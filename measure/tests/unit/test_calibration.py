"""自动校准(S9.4,用户定「校准不靠人工」):写法生成、塞入、三件套计数、存档与装回、确认页只读实测计数。零网络零数据库。"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from engine.calibration import (
    CalibrationReport,
    Sample,
    calibrate,
    inject,
    load_calibration,
    render_markdown,
    save_calibration,
    variants_of,
)
from engine.models import Answer, BrandSpec, Outcome, SearchMode, SearchTrace, SurfaceRun
from report.app import create_app
from tests.unit.test_report_page import REGISTRY, DummyAdapter

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM", "Aurora CRM"], domains=["aurora-crm.com"])


def sample(text: str, run_id: str, brand: BrandSpec = BRAND, competitors: tuple[str, ...] = ()) -> Sample:
    run = SurfaceRun(run_id=run_id, prompt_id="p", run_index=1, provider="test_provider", surface="s", model="m",
                     search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
                     answer=Answer(answer_text=text, citations_available=True), search_trace=SearchTrace(),
                     timestamp=datetime(2026, 10, 21, tzinfo=UTC), latency_ms=1, cost_usd=0.0)
    return Sample(run=run, brand=brand, competitors=list(competitors))


def test_variants_cover_the_decided_spellings_and_near_misses():
    variants = variants_of(BRAND, ["竞品甲", "AuroraCRM", " "])
    kinds = {(v.kind, v.text, v.expected) for v in variants}
    assert ("原样", "极光CRM", True) in kinds and ("竞品名", "竞品甲", False) in kinds  # 中文只做原样;负例 = 竞品名
    assert not any(v.expected is False and v.kind == "竞品名" and v.text != "竞品甲" for v in variants)  # 撞名 / 空名
    assert ("小写", "auroracrm", True) in kinds and ("复数", "AuroraCRMs", True) in kinds
    assert ("所有格", "AuroraCRM's", True) in kinds
    # 黄 9 定稿(2026-10-27):中间换一个字母是别的词,规则 / 模糊都不该认;裁判不判这条(按上下文会当拼错的品牌名)
    other = next(v for v in variants if v.kind == "中间换一个字母(别的词)")
    assert (other.text, other.expected, other.judge) == ("AuroxaCRM", False, False)
    assert all(v.judge for v in variants if v.kind != "中间换一个字母(别的词)")
    assert ("去空格", "AuroraCRM", True) not in kinds  # 与别名 AuroraCRM 重复:去重
    accented = {(v.kind, v.text, v.expected) for v in variants_of(BrandSpec(name="Citroën"))}
    assert ("去重音", "Citroen", True) in accented and ("原样", "Citroën", True) in accented
    assert ("连字符", "Aurora-CRM", True) in kinds and ("全角", "ＡｕｒｏｒａＣＲＭ", True) in kinds
    assert len({v.text for v in variants}) == len(variants)  # 按原样不重复(大小写、全角是要分开测的写法)


def test_inject_puts_the_spelling_after_the_first_sentence_with_boundaries():
    assert inject("第一句。第二句。", "AuroraCRM") == "第一句。 AuroraCRM 第二句。"
    assert inject("No punctuation at all", "X") == "X No punctuation at all"
    assert inject("First one. Second one.", "Y") == "First one. Y Second one."


def test_calibrate_counts_hits_false_alarms_disagreement_and_double_judge():
    rivals = ("竞品甲",)
    samples = [sample("常见选择包括 Alpha 和 Beta。", "r1", competitors=rivals),
               sample("Many pick auroracrms for pricing.", "r2", competitors=rivals),
               sample("我们推荐 极光CRM。", "r3", competitors=rivals),
               sample("Nothing here. Really.", "r4", competitors=rivals)]
    # 裁判甲:看到 Aurora 开头的拉丁写法或中文名就说提到;裁判乙:只认中文名;r4 裁判甲看不到
    judge_a = lambda run, brand: None if run.run_id == "r4" else (  # noqa: E731
        "极光" in run.answer.answer_text or "auro" in run.answer.answer_text.lower())
    judge_b = lambda run, brand: "极光" in run.answer.answer_text  # noqa: E731
    report = calibrate(samples, source="测试", judge_a=judge_a, judge_b=judge_b, judge_sample=4, seed=1,
                       now=datetime(2026, 10, 21, tzinfo=UTC))
    assert (report.n_answers, report.n_bases, report.judge_sample) == (4, 2, 4)  # 底本 = r1、r4(规则 / 模糊都没提到)
    rules, fuzzy, judge = report.totals("rules"), report.totals("fuzzy"), report.totals("judge")
    assert rules["pos_n"] == fuzzy["pos_n"] > 0 and rules["pos_hit"] < fuzzy["pos_hit"]  # 模糊比规则多认出变体
    assert rules["neg_n"] == fuzzy["neg_n"] == 6  # 两个底本各塞一个竞品名 + 两个别名各一个「中间换一个字母」
    assert rules["neg_false"] == 0 and fuzzy["neg_false"] == 0  # 竞品名、中间换字的别的词都没被误认(黄 9 v2)
    assert judge["neg_n"] == 2  # 裁判只判竞品名负例,不判「中间换一个字母」(按上下文会当拼错的品牌名)
    assert judge["pos_n"] > 0 and judge["pos_n"] < fuzzy["pos_n"]  # 裁判只在抽样且说没提到的底本上跑(r4 看不到)
    rf = report.pair("rules", "fuzzy")
    assert rf is not None and (rf.n, rf.disagree, rf.examples) == (4, 1, ["r2"])  # auroracrms:规则没认、模糊认了
    rj = report.pair("rules", "judge")
    assert rj is not None and rj.n == 3  # r4 裁判看不到,不比
    assert report.double_judge is not None and (report.double_judge.n, report.double_judge.agree) == (3, 2)
    assert report.double_judge.disagree_ids == ["r2"]
    md = render_markdown(report)
    assert "自动校准,非人工核对" in md and "| fuzzy | 复数 | 是 |" in md and "rules vs fuzzy:1/4(25%)" in md


def test_without_judges_only_rules_and_fuzzy_are_compared():
    report = calibrate([sample("x", "r1")], source="测试")
    assert report.judge_sample == 0 and report.double_judge is None
    assert [p.a + p.b for p in report.disagreement] == ["rulesfuzzy"] and report.totals("judge")["pos_n"] == 0


def test_calibration_round_trips_through_yaml(tmp_path: Path):
    report = calibrate([sample("常见选择包括 Alpha。", "r1")], source="测试", now=datetime(2026, 10, 21, tzinfo=UTC))
    path = tmp_path / "calibration.yaml"
    save_calibration(report, path)
    assert load_calibration(path) == report
    assert load_calibration(tmp_path / "missing.yaml") is None  # 没跑过就是没有,不猜


def test_confirm_page_shows_measured_counts_only_after_calibration():
    report = CalibrationReport.model_validate({
        "calibrated_at": "2026-10-21T00:00:00Z", "source": "旧库", "n_answers": 900, "n_bases": 600,
        "judge_sample": 100,
        "synthetic": [
            {"method": "rules", "kind": "原样", "expected": True, "n": 600, "correct": 600},
            {"method": "rules", "kind": "复数", "expected": True, "n": 600, "correct": 0},
            {"method": "rules", "kind": "竞品名", "expected": False, "n": 600, "correct": 600},
            {"method": "fuzzy", "kind": "原样", "expected": True, "n": 600, "correct": 600},
            {"method": "fuzzy", "kind": "复数", "expected": True, "n": 600, "correct": 598},
            {"method": "fuzzy", "kind": "竞品名", "expected": False, "n": 600, "correct": 597},
            {"method": "judge", "kind": "原样", "expected": True, "n": 70, "correct": 69},
            {"method": "judge", "kind": "竞品名", "expected": False, "n": 70, "correct": 70},
        ],
        "disagreement": [{"a": "rules", "b": "fuzzy", "n": 900, "disagree": 31, "examples": ["a"]}],
        "double_judge": {"n": 95, "agree": 91, "disagree_ids": ["b"]},
    })

    def page(calibration):
        app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                         evidence=REGISTRY, job_runner=lambda **kw: None, calibration=calibration)
        client = TestClient(app)
        resp = client.post("/jobs?lang=zh", data={"brand_name": "X", "aliases": "", "website": "x.com", "category": "C",
                                                  "questions": "q?", "generate": ""}, follow_redirects=False)
        return client.get(resp.headers["location"]).text

    before = page(None)
    assert "自动校准还没做" in before
    after = page(report)
    assert "自动校准还没做" not in after
    assert "自动校准(2026-10-21,非人工核对;900 条真实回答)" in after
    assert "规则认出 600/1200,模糊认出 1198/1200,裁判认出 69/70" in after
    assert "规则误认 0/600,模糊误认 3/600,裁判误认 0/70" in after
    assert "规则与模糊判法不同 31/900 条" in after and "两家裁判一致 91/95 条" in after
    assert "低档用规则,中档用模糊,高档用裁判" in after


def test_prefetch_hook_gets_the_sampled_answers_then_their_synthetic_runs():
    """脚本靠这个钩子并行预判:先抽样的真实回答(两家都判),再这些底本的合成样本(只请主裁判)。"""
    samples = [sample("常见选择包括 Alpha。", "r1"), sample("推荐 极光CRM。", "r2")]
    calls = []
    calibrate(samples, source="测试", judge_a=lambda run, brand: False, judge_b=lambda run, brand: False,
              judge_sample=2, prefetch=lambda batch, both: calls.append((sorted(s.run.run_id for s in batch), both)))
    assert calls[0] == (["r1", "r2"], True)
    synthetic_ids, both = calls[1]
    assert both is False and synthetic_ids and all(i.startswith("r1#") for i in synthetic_ids)  # 只有 r1 是底本
