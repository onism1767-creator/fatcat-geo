"""自动校准(S9.4;架构书附录 v0.4.27 第 5 节 + 用户 2026-10-14 拍板「校准不靠人工」):
三种「提到你」判法准不准,自动数出来。

三件套(全部是计数,标明「自动校准,非人工核对」;硬边界 8:计数 + 比例 + 口径,不出分数):
① 合成测试集:规则和模糊都说「没提到」的真实回答里,程序塞入各种写法的品牌名(答案已知:提到了),再塞入近似的错名
   (答案已知:没提到),每种判法认出几条、误认几条自动数;
② 真实回答上两两分歧:规则 vs 模糊全量比,裁判只比抽样的那批(裁判花钱);
③ 两家不同的裁判判同一批回答,一致几条;不一致的列出来供可选抽看。
结果存 config/calibration.yaml(确认页「准确率对比」只读它)与 docs/校准/ 的 markdown。
本模块无厂商名,不连库;读库在 scripts/ 里。
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector
from engine.detectors.matching import is_spaced_script_char
from engine.models import BrandSpec, FrozenModel, SurfaceRun

DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parents[1] / "config" / "calibration.yaml"
MAX_EXAMPLES = 20  # 分歧清单最多列几条 run_id

Judge = Callable[[SurfaceRun, BrandSpec], bool | None]  # 裁判:提到了 / 没提到 / 看不到(None)
METHODS = ("rules", "fuzzy", "judge")


class Sample(FrozenModel):
    run: SurfaceRun
    brand: BrandSpec
    competitors: list[str] = []  # 这单的竞品名:塞进去当负例(肯定不是你;S9.4b,10-21 换掉首字母错名)


class Variant(FrozenModel):
    kind: str  # 写法种类(原样 / 小写 / 去空格 / 全角 / 复数 / 所有格 / 去重音 / 中间换一个字母(别的词) / 竞品名……)
    text: str
    expected: bool  # 塞进去之后「提到了」的正确答案
    # 裁判要不要判这条:中间换一个字母的「别的词」裁判按上下文会当拼错的品牌名(S9.4b),只对规则 / 模糊计分
    judge: bool = True


class KindScore(FrozenModel):
    method: str
    kind: str
    expected: bool
    n: int
    correct: int  # expected=True 时 = 认出来的条数;expected=False 时 = 没误认的条数


class PairDisagreement(FrozenModel):
    a: str
    b: str
    n: int  # 两边都有判定的条数
    disagree: int
    examples: list[str]  # 分歧的 run_id(最多 MAX_EXAMPLES 条,供抽看)


class DoubleJudge(FrozenModel):
    n: int  # 两家裁判都判了的条数
    agree: int
    disagree_ids: list[str]


class CalibrationReport(FrozenModel):
    calibrated_at: datetime
    source: str  # 数据来源口径(哪个库、哪几单、几条回答)
    n_answers: int
    n_bases: int  # 作合成底本的回答数(规则 / 模糊都说没提到)
    judge_sample: int  # 抽了几条真实回答请裁判
    synthetic: list[KindScore]
    disagreement: list[PairDisagreement]
    double_judge: DoubleJudge | None

    def totals(self, method: str) -> dict[str, int]:
        """一种判法的合计:塞入写法 pos_n 条认出 pos_hit;塞入错名 neg_n 条误认 neg_false。"""
        pos = [s for s in self.synthetic if s.method == method and s.expected]
        neg = [s for s in self.synthetic if s.method == method and not s.expected]
        return {
            "pos_n": sum(s.n for s in pos), "pos_hit": sum(s.correct for s in pos),
            "neg_n": sum(s.n for s in neg), "neg_false": sum(s.n - s.correct for s in neg),
        }

    def pair(self, a: str, b: str) -> PairDisagreement | None:
        return next((p for p in self.disagreement if {p.a, p.b} == {a, b}), None)


# ── ① 写法生成与塞入 ──────────────────────────────────────────────────────────────────


def _is_latin(name: str) -> bool:
    return any(c.isalpha() for c in name) and all(is_spaced_script_char(c) or c in " -'’." for c in name)


def _fullwidth(ch: str) -> str:
    code = ord(ch)
    return chr(code + 0xFEE0) if 0x21 <= code <= 0x7E else ch


def _other_word(name: str) -> str:
    """中间换一个字母:按 2026-10-27 定稿(黄 9)这是别的词(Colombia 不是 Columbia),规则 / 模糊都不该认。"""
    middle = [i for i, c in enumerate(name) if c.isalpha()][len(name) // 2]
    swap = "x" if name[middle].lower() != "x" else "q"
    return name[:middle] + swap + name[middle + 1:]


def _strip_accents(name: str) -> str:
    """去掉重音符号(Citroën → Citroen):回答里常这么写,规整那道关该认(v2)。"""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))


def variants_of(brand: BrandSpec, competitors: Sequence[str] = ()) -> list[Variant]:
    """一张品牌卡能生成的写法:拉丁名字的大小写 / 空格 / 连字符 / 全角 / 复数 / 所有格 / 去重音(都该认出来);
    中间换一个字母是别的词(黄 9 定稿 2026-10-27:只对规则 / 模糊计分,裁判按上下文会当拼错的品牌名,
    S9.4b 已定不算干净负例);
    中文名字只做原样(中文不模糊,用户定);负例还有竞品名(肯定不是你)。"""
    out: list[Variant] = []
    seen: set[tuple[str, bool]] = set()

    def add(kind: str, text: str, expected: bool, *, judge: bool = True) -> None:
        key = (text, expected)  # 按原样去重:大小写、全角都是要分开测的写法
        if text.strip() and key not in seen:
            seen.add(key)
            out.append(Variant(kind=kind, text=text, expected=expected, judge=judge))

    for raw in dict.fromkeys([brand.name, *brand.aliases]):
        name = raw.strip()
        if not name:
            continue
        add("原样", name, True)
        if _is_latin(name):
            add("小写", name.lower(), True)
            if " " in name:
                add("去空格", name.replace(" ", ""), True)
                add("连字符", name.replace(" ", "-"), True)
            add("全角", "".join(_fullwidth(c) for c in name), True)
            if len(name) >= 4 and name[-1].isalpha():
                add("复数", name + "s", True)
                add("所有格", name + "'s", True)
            if _strip_accents(name) != name:
                add("去重音", _strip_accents(name), True)
            if len(name) >= 6:
                add("中间换一个字母(别的词)", _other_word(name), False, judge=False)
    known = {n.strip().lower() for n in [brand.name, *brand.aliases]}
    for competitor in competitors:
        if competitor.strip() and competitor.strip().lower() not in known:
            add("竞品名", competitor.strip(), False)
    return out


_SENTENCE_END = re.compile(r"[。!?\n]|[.!?](?=\s)")


def inject(answer: str, text: str) -> str:
    """把一种写法塞进回答第一句之后(没有句读就放开头),前后留空格,拉丁写法才有词边界。"""
    match = _SENTENCE_END.search(answer)
    if match is None:
        return f"{text} {answer}"
    cut = match.end()
    return f"{answer[:cut]} {text} {answer[cut:].lstrip(' ')}"


def synthetic_run(sample: Sample, variant: Variant, index: int) -> SurfaceRun:
    assert sample.run.answer is not None
    answer = sample.run.answer.model_copy(update={"answer_text": inject(sample.run.answer.answer_text, variant.text)})
    return sample.run.model_copy(update={"run_id": f"{sample.run.run_id}#{index}", "answer": answer})


# ── 跑三种判法 ────────────────────────────────────────────────────────────────────────


def _rules_judge() -> Judge:
    detector = BrandMentionDetector()

    def says(run: SurfaceRun, brand: BrandSpec) -> bool | None:
        obs = detector.detect(run, brand)
        return None if obs is None else obs.mentioned

    return says


def _fuzzy_judge() -> Judge:
    detector = FuzzyBrandMentionDetector()

    def says(run: SurfaceRun, brand: BrandSpec) -> bool | None:
        obs = detector.detect(run, brand)
        return None if obs is None else obs.mentioned

    return says


def calibrate(
    samples: Sequence[Sample],
    *,
    source: str,
    judge_a: Judge | None = None,
    judge_b: Judge | None = None,
    judge_sample: int = 0,
    seed: int = 0,
    now: datetime | None = None,
    prefetch: Callable[[Sequence[Sample], bool], None] | None = None,
) -> CalibrationReport:
    """三件套一次跑完。裁判只在抽样的 judge_sample 条真实回答及其合成样本上跑(花钱有界);没给裁判就只比规则 / 模糊。
    prefetch(批, 两家都判吗):给脚本并行预判的钩子——先把一批裁判结论并行取回缓存,这里再顺序读。"""
    rng = random.Random(seed)
    rules, fuzzy = _rules_judge(), _fuzzy_judge()
    verdicts: dict[str, dict[str, bool | None]] = {m: {} for m in METHODS}  # 判法 → run_id → 结论
    for s in samples:
        verdicts["rules"][s.run.run_id] = rules(s.run, s.brand)
        verdicts["fuzzy"][s.run.run_id] = fuzzy(s.run, s.brand)
    judged: list[Sample] = []
    double: DoubleJudge | None = None
    if judge_a is not None and judge_sample > 0:
        judged = rng.sample(list(samples), min(judge_sample, len(samples)))
        if prefetch is not None:
            prefetch(judged, True)
        for s in judged:
            verdicts["judge"][s.run.run_id] = judge_a(s.run, s.brand)
        if judge_b is not None:
            agree, disagree_ids, n = 0, [], 0
            for s in judged:
                a, b = verdicts["judge"][s.run.run_id], judge_b(s.run, s.brand)
                if a is None or b is None:
                    continue
                n += 1
                if a == b:
                    agree += 1
                else:
                    disagree_ids.append(s.run.run_id)
            double = DoubleJudge(n=n, agree=agree, disagree_ids=disagree_ids[:MAX_EXAMPLES])
    # ② 两两分歧(都有判定的才比)
    disagreement = [_pair(a, b, verdicts) for a, b in (("rules", "fuzzy"), ("rules", "judge"), ("fuzzy", "judge"))]
    # ① 合成:底本 = 规则和模糊都说没提到的回答;裁判只在抽样底本上跑
    bases = [s for s in samples if verdicts["rules"].get(s.run.run_id) is False
             and verdicts["fuzzy"].get(s.run.run_id) is False]
    judged_ids = {s.run.run_id for s in judged if verdicts["judge"].get(s.run.run_id) is False}
    synthetic_runs = [(base, variant, synthetic_run(base, variant, i))
                      for base in bases for i, variant in enumerate(variants_of(base.brand, base.competitors))]
    if prefetch is not None and judge_a is not None:
        prefetch([Sample(run=run, brand=base.brand) for base, _, run in synthetic_runs
                  if base.run.run_id in judged_ids], False)  # 合成样本只请主裁判
    scores: dict[tuple[str, str, bool], list[int]] = {}
    for base, variant, run in synthetic_runs:
        methods: list[tuple[str, Judge]] = [("rules", rules), ("fuzzy", fuzzy)]
        if judge_a is not None and base.run.run_id in judged_ids and variant.judge:
            methods.append(("judge", judge_a))
        for method, says in methods:
            said = says(run, base.brand)
            if said is None:
                continue  # 看不到的不算
            cell = scores.setdefault((method, variant.kind, variant.expected), [0, 0])
            cell[0] += 1
            cell[1] += int(said == variant.expected)
    synthetic = [KindScore(method=m, kind=k, expected=e, n=n, correct=c)
                 for (m, k, e), (n, c) in sorted(scores.items(), key=lambda kv: (METHODS.index(kv[0][0]), kv[0][1]))]
    return CalibrationReport(
        calibrated_at=now or datetime.now(UTC), source=source, n_answers=len(samples), n_bases=len(bases),
        judge_sample=len(judged), synthetic=synthetic, disagreement=[d for d in disagreement if d.n],
        double_judge=double,
    )


def _pair(a: str, b: str, verdicts: dict[str, dict[str, bool | None]]) -> PairDisagreement:
    both = [rid for rid in verdicts[a] if verdicts[a][rid] is not None and verdicts[b].get(rid) is not None]
    differ = [rid for rid in both if verdicts[a][rid] != verdicts[b][rid]]
    return PairDisagreement(a=a, b=b, n=len(both), disagree=len(differ), examples=differ[:MAX_EXAMPLES])


# ── 存档与装回 ────────────────────────────────────────────────────────────────────────


def save_calibration(report: CalibrationReport, path: Path = DEFAULT_CALIBRATION_PATH) -> None:
    path.write_text(
        "# 自动校准结果(S9.4,scripts/calibrate_mention_detectors.py 生成;只记计数,非人工核对)。\n"
        "# 确认页「准确率对比」只读本文件。\n"
        + yaml.safe_dump(report.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_calibration(path: Path = DEFAULT_CALIBRATION_PATH) -> CalibrationReport | None:
    """没跑过校准就 None(确认页写「自动校准还没做」),不猜。"""
    if not path.exists():
        return None
    return CalibrationReport.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def render_markdown(report: CalibrationReport) -> str:
    """docs/校准/ 的存档:计数 + 比例 + 口径。"""
    def pct(k: int, n: int) -> str:
        return f"{k}/{n}({round(100 * k / n)}%)" if n else "—"

    lines = [f"# 提及判法自动校准 {report.calibrated_at.astimezone(UTC):%Y-%m-%d}", "",
             f"口径:{report.source};真实回答 {report.n_answers} 条,合成底本 {report.n_bases} 条,"
             f"请裁判抽样 {report.judge_sample} 条。",
             "自动校准,非人工核对。", "", "## ① 合成测试集(程序塞入写法,答案已知)", "",
             "| 判法 | 写法 | 应认出 | 条数 | 判对 |", "|---|---|---|---|---|"]
    for s in report.synthetic:
        expected = "是" if s.expected else "否(错名)"
        lines.append(f"| {s.method} | {s.kind} | {expected} | {s.n} | {pct(s.correct, s.n)} |")
    lines += ["", "合计:"]
    for m in METHODS:
        t = report.totals(m)
        if t["pos_n"] or t["neg_n"]:
            lines.append(f"- {m}:塞入写法认出 {pct(t['pos_hit'], t['pos_n'])};"
                         f"塞入错名误认 {pct(t['neg_false'], t['neg_n'])}")
    lines += ["", "## ② 真实回答两两分歧", ""]
    for p in report.disagreement:
        lines.append(f"- {p.a} vs {p.b}:{pct(p.disagree, p.n)} 条判法不同;抽看:{', '.join(p.examples) or '—'}")
    lines += ["", "## ③ 两家裁判一致", ""]
    if report.double_judge is None:
        lines.append("- 没跑(没给第二个裁判或没抽样)")
    else:
        d = report.double_judge
        lines.append(f"- 一致 {pct(d.agree, d.n)};不一致抽看:{', '.join(d.disagree_ids) or '—'}")
    return "\n".join(lines) + "\n"
