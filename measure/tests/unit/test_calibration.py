"""自动校准(S9.4,用户定「校准不靠人工」):写法生成、塞入、三件套计数、存档与装回、确认页只读实测计数。零网络零数据库。"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from engine.calibration import (
    CalibrationReport,
    RealSample,
    Sample,
    calibrate,
    current_versions,
    inject,
    load_calibration,
    render_markdown,
    report_path,
    save_calibration,
    variants_of,
)
from engine.hard_cases import NAMING, HardBrand
from engine.models import Answer, BrandSpec, MentionContext, Outcome, SearchMode, SearchTrace, SurfaceRun
from report.app import create_app
from report.narrative import calibration_text
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
    assert judge["neg_n"] == 1  # 裁判只在它判过的底本(r1;r4 看不到)上判竞品名负例,不判「中间换一个字母」
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


def test_a_rerun_on_the_same_day_keeps_the_earlier_report(tmp_path: Path):
    """改判定器前后同日各跑一次要比前后,不能盖掉(BR2 盖掉过 BR1 的基准)。"""
    first = report_path(tmp_path, "2026-10-30")
    assert first.name == "校准-2026-10-30.md"
    first.write_text("基准", encoding="utf-8")
    second = report_path(tmp_path, "2026-10-30")
    assert second.name == "校准-2026-10-30-2.md"
    second.write_text("修后", encoding="utf-8")
    assert report_path(tmp_path, "2026-10-30").name == "校准-2026-10-30-3.md"
    assert first.read_text(encoding="utf-8") == "基准"


def test_confirm_page_shows_measured_counts_only_after_calibration():
    report = CalibrationReport.model_validate({
        "calibrated_at": "2026-10-21T00:00:00Z", "source": "旧库", "n_answers": 900, "n_bases": 600,
        "judge_sample": 100, "detector_versions": current_versions(), "hard": [], "cross": [], "real": [],
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
    assert "裁判这次没测" not in after and "判法改过了" not in after and "程序造的难题" not in after


def test_prefetch_hook_gets_the_sampled_answers_then_their_synthetic_runs():
    """脚本靠这个钩子并行预判:先抽样的真实回答(两家都判),再这些底本的合成样本(只请主裁判)。"""
    samples = [sample("常见选择包括 Alpha。", "r1"), sample("推荐 极光CRM。", "r2")]
    calls = []
    calibrate(samples, source="测试", judge_a=lambda run, brand: False, judge_b=lambda run, brand: False,
              judge_sample=2, prefetch=lambda batch, both: calls.append((sorted(s.run.run_id for s in batch), both)))
    assert calls[0] == (["r1", "r2"], True)
    synthetic_ids, both = calls[1]
    assert both is False and synthetic_ids and all(i.startswith("r1#") for i in synthetic_ids)  # 只有 r1 是底本


def test_prefetch_skips_spellings_the_judge_is_not_scored_on():
    """「中间换一个字母」只对规则 / 模糊计分,裁判不判:原来预判时也先请裁判判一遍,花钱不计分(第二轮审核 B10)。"""
    samples = [sample("常见选择包括 Alpha。", "r1"), sample("推荐 极光CRM。", "r2")]
    calls = []
    calibrate(samples, source="测试", judge_a=lambda run, brand: False, judge_b=lambda run, brand: False,
              judge_sample=2, prefetch=lambda batch, both: calls.append(sorted(s.run.run_id for s in batch)))
    variants = variants_of(BRAND)
    assert any(not v.judge for v in variants)  # 有不归裁判计分的写法,这条测试才有意义
    assert calls[1] == sorted(f"r1#{i}" for i, v in enumerate(variants) if v.judge)


def test_names_with_few_letters_do_not_crash_the_variant_maker():
    """「Web 3.0」7 个字只有 3 个字母:原来找中间字母时下标越界,整个校准脚本崩(第二轮审核 B10)。"""
    swapped = [v.text for v in variants_of(BrandSpec(name="Web 3.0")) if not v.expected]
    assert len(swapped) == 1 and swapped[0] != "Web 3.0"
    # 没崩过的名字换的还是原来那个字母(校准数字不因此变)
    assert [v.text for v in variants_of(BRAND) if v.kind.startswith("中间")] == ["AuroxaCRM", "Aurorx CRM"]


def test_a_swapped_name_that_still_contains_a_registered_name_is_not_a_negative():
    """第 8 条:On Running 换字母成 On Runxing,里面的 On 本来就是登记名,认出来是对的,不能算误认。"""
    brand = BrandSpec(name="On", aliases=["On Running"])
    assert not any(v.kind.startswith("中间") for v in variants_of(brand))
    report = calibrate([sample("常见选择包括 Alpha。", "r1", brand=brand)], source="测试")
    assert report.totals("rules")["neg_n"] == 0 and report.totals("fuzzy")["neg_false"] == 0


def _hard_report() -> CalibrationReport:
    answers = [sample("常见选择包括 Alpha 和 Beta。", "r1"), sample("美的空调卖得好。第二句。", "r2")]
    hard = [
        HardBrand(name="기아", positives={"带助词": ["기아는 자동차 회사입니다."]}),
        HardBrand(name="美的", category="空调", negatives={"中文词中词": ["甜美的笑容。"]},
                  positives={"语境里的品牌": ["美的空调。"]}),
    ]
    return calibrate(answers, source="测试", hard=hard, now=datetime(2026, 10, 30, tzinfo=UTC))


def test_hard_cases_are_counted_by_kind_and_kept_apart_from_the_old_totals():
    """BR1:难题按种类分开数、不混进原来的合计;量的是现在判法的毛病。
    韩文近形三条(기압、기안、기암)BR2 之前中档全误认,模糊 v3 全不认了。"""
    report = _hard_report()
    plain = calibrate([sample("常见选择包括 Alpha 和 Beta。", "r1")], source="测试")
    assert report.totals("rules")["neg_n"] > 0 and plain.hard == [] and plain.cross == []
    cells = {(s.method, s.kind, s.expected): (s.n, s.correct) for s in report.hard}
    assert cells[("rules", "韩文近形", False)] == (6, 6)  # 3 条 × 2 个底本,规则不认
    assert cells[("fuzzy", "韩文近形", False)] == (6, 6)
    assert cells[("rules", "带助词", True)] == (2, 2) and cells[("fuzzy", "带助词", True)] == (2, 2)
    # 美的的底本只剩 r1:r2 里本来就有美的,不能拿来塞难题,否则分不清是谁惹的;「甜美的」BR4 起不算你了
    assert cells[("rules", "中文词中词", False)] == (1, 1) and cells[("rules", "语境里的品牌", True)] == (1, 1)
    totals = report.hard_totals("fuzzy")
    assert (totals["neg_n"], totals["neg_false"]) == (7, 0) and (totals["pos_n"], totals["pos_hit"]) == (3, 3)


def test_real_answers_of_hard_brands_are_counted_apart_by_brand_kind_and_method():
    """BR3:点名题列没认出的,反例题列认出的;不混进前面几样的合计。"""
    brand = BrandSpec(name="美的")
    context = MentionContext(category="空调")

    def real_sample(text: str, run_id: str, kind: str) -> RealSample:
        return RealSample(run=sample(text, run_id).run, brand=brand, kind=kind, context=context)

    real = [real_sample("美的空调很省电。", "n1", NAMING), real_sample("这款空调很省电。", "n2", NAMING),
            real_sample("这是一次完美的旅行。", "c1", "反例"), real_sample("美的空调很常见。", "c2", "反例"),
            real_sample("美的很常见。", "u1", "不点名")]

    report = calibrate([sample("常见选择包括 Alpha。", "r1")], source="测试", real=real)
    plain = calibrate([sample("常见选择包括 Alpha。", "r1")], source="测试")

    cells = {(s.kind, s.method): s for s in report.real}
    assert (cells[(NAMING, "rules")].n, cells[(NAMING, "rules")].hits) == (2, 1)
    assert [e.split(":")[0] for e in cells[(NAMING, "rules")].examples] == ["n2"]
    assert (cells[("反例", "fuzzy")].n, cells[("反例", "fuzzy")].hits) == (2, 1)
    assert [e.split(":")[0] for e in cells[("反例", "fuzzy")].examples] == ["c2"]
    assert (cells[("不点名", "fuzzy")].hits, cells[("不点名", "fuzzy")].unsure) == (0, 1)  # BR4:说不准单独数
    assert report.synthetic == plain.synthetic and plain.real == []
    assert "| 美的 | 反例 | fuzzy | 2 | 1/2(50%) | 0 |" in render_markdown(report)


def test_other_brands_answers_as_bases_are_listed_for_checking():
    """BR1:别家回答当底本,认出来的列出来可选抽看;答案不是构造的,不进合计。"""
    report = _hard_report()
    cross = {(c.brand, c.method): c for c in report.cross}
    assert (cross[("美的", "rules")].n, cross[("美的", "rules")].hits) == (2, 1)
    assert cross[("美的", "rules")].examples == ["r2:美的空调卖得好。第二句。"]
    assert (cross[("기아", "fuzzy")].n, cross[("기아", "fuzzy")].hits) == (2, 0)
    md = render_markdown(report)
    assert "## ④ 难题" in md and "| fuzzy | 韩文近形 | 否 | 6 | 6/6(100%) |" in md
    assert "## ⑤ 别家回答当底本" in md and "美的 · rules:1/2(50%)" in md


def test_a_real_brand_can_be_left_out_of_the_bases():
    brand = HardBrand(name="Alpha", skip_bases=[BRAND.name], negatives={"常用词": ["alpha"]})
    report = calibrate([sample("常见选择包括 Alpha。", "r1")], source="测试", hard=[brand])
    assert report.hard == [] and {c.n for c in report.cross} == {0}


def test_versions_are_recorded_and_a_changed_detector_is_flagged(tmp_path: Path, monkeypatch):
    """第 22 条:校准文件记测的是哪版判定器;之后升了版本,确认页要说这是旧版的数字(中英)。没请裁判就不记裁判。"""
    report = _hard_report()
    assert report.detector_versions == {k: v for k, v in current_versions().items() if k != "judge"}
    path = tmp_path / "calibration.yaml"
    save_calibration(report, path)
    assert load_calibration(path) == report
    assert report.stale_methods() == []
    zh, en = calibration_text(report, "zh"), calibration_text(report, "en")
    assert "判法改过了" not in zh and "has changed since" not in en
    assert "裁判这次没测" in zh and "the judge was not tested this time" in en
    assert "规则误认 0/7,模糊误认 0/7" in zh and "规则认出 3/3,模糊认出 3/3" in zh
    assert "rules false alarms 0/7, fuzzy 0/7" in en and "rules recognised 3/3, fuzzy 3/3" in en

    newer = {**current_versions(), "fuzzy": report.detector_versions["fuzzy"] + 1}
    monkeypatch.setattr("engine.calibration.current_versions", lambda: newer)
    assert report.stale_methods() == ["fuzzy"]
    assert "这份校准之后,模糊的判法改过了,上面是旧版的数字" in calibration_text(report, "zh")
    assert "the fuzzy method has changed since this calibration" in calibration_text(report, "en")


def test_judge_version_counts_once_the_judge_was_used():
    report = calibrate([sample("x", "r1")], source="测试", judge_a=lambda run, brand: False, judge_sample=1)
    assert report.detector_versions == current_versions()


def test_the_calibration_file_in_the_repo_is_readable_and_records_versions():
    """仓库里的校准结果要能读、要带版本;升判定器版本却没重跑时这里不拦(确认页会提示),只拦文件坏了。"""
    report = load_calibration()
    assert report is not None and {"rules", "fuzzy"} <= set(report.detector_versions) and report.hard
    assert "程序造的难题" in calibration_text(report, "zh") and "hard cases" in calibration_text(report, "en")
