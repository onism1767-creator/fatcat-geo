"""L3 阶段诊断表驱动测试(12.1):阶段指标输入 → 期望状态。

用例刻意选在远离阈值边界的清晰区,避免数值临界抖动。v0.4.26 S2:判定看比例,线 20% / 50% 不变。
"""

from diagnosis.citation_diag import diagnose_citation
from diagnosis.findings import Stage, StageStatus, prompt_msgs
from diagnosis.messages import render_msg
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import retrieval_not_observable
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate, account_outcomes, rate
from engine.models import Outcome, PromptIntent, Sentiment, StopReason


def zh(message) -> str:
    return render_msg(message, "zh")


def zh_lines(finding) -> list[str]:
    return [zh(line) for line in finding.evidence_lines]


def make_metric(k: int, n: int, *, refused: int = 0, failed: int = 0,
                per_prompt: list[Rate | None] | None = None) -> ClassSurfaceMetric:
    outcomes = (
        [Outcome.SUCCESS] * n
        + [Outcome.REFUSED] * refused
        + [Outcome.TRANSIENT_FAILURE] * failed
    )
    r = rate(k, n) if n else None
    per_prompt = per_prompt if per_prompt is not None else ([r] if r else [])
    observed = [p for p in per_prompt if p is not None]
    return ClassSurfaceMetric(
        intent=PromptIntent.CATEGORY_DISCOVERY,
        provider="test_provider",
        surface="test_surface",
        model="m1",
        metric_label="test_metric",
        k_hits=k,
        rate=r,
        accounting=account_outcomes(outcomes),
        stop_reasons=[StopReason.COMPLETED],
        per_prompt=per_prompt,
        prompt_coverage=Rate(k=sum(p.k > 0 for p in observed), n=len(observed)) if observed else None,
    )


def test_prompt_lines_and_coverage():
    """逐题并列 + 题目覆盖率(产品文档 15.2);单题无并列可言,但覆盖率照给。"""
    spread = make_metric(6, 12, per_prompt=[rate(3, 3), rate(3, 3), rate(0, 3), rate(0, 3)])
    lines = [zh(m) for m in prompt_msgs(spread)]
    assert len(lines) == 2 and "题1 3/3" in lines[0] and "题3 0/3" in lines[0]
    assert "题目覆盖率:4 道题里 2 道至少命中一次(50%)" in lines[1]

    with_gap = make_metric(3, 6, per_prompt=[rate(3, 6), None])
    assert "题2 无有效样本" in zh(prompt_msgs(with_gap)[0])

    single = make_metric(3, 8)
    assert len(prompt_msgs(single)) == 1 and "1 道题里 1 道至少命中一次" in zh(prompt_msgs(single)[0])
    caliber = "\n".join(zh_lines(diagnose_citation(single)))
    assert "冻结题目集上的观测率" in caliber and "题数=1" in caliber


# ── RETRIEVAL ────────────────────────────────────────────────────────


def test_retrieval_not_observable_when_surface_exposes_no_retrieval():
    """仪器不暴露检索链路 → 不可观测,证据行写明不借数据、不反推(铁律 7;v0.4.26 S1)。"""
    finding = retrieval_not_observable()
    assert finding.status is StageStatus.NOT_OBSERVABLE
    assert finding.stage is Stage.RETRIEVAL
    assert any("不从别的接口借数据" in line for line in zh_lines(finding))


# ── CITATION ─────────────────────────────────────────────────────────


def test_citation_absent_fails_regardless_of_sample_size():
    """看比例(T1):0/30 和 0/8 都是 0% < 20% → FAIL;样本量写在口径里,不再靠区间收窄。"""
    assert diagnose_citation(make_metric(0, 30)).status is StageStatus.FAIL
    assert diagnose_citation(make_metric(0, 8)).status is StageStatus.FAIL
    assert any("操作阈值(v1 初始值,未经校准)" in line for line in zh_lines(diagnose_citation(make_metric(0, 8))))


def test_citation_thresholds_on_the_rate():
    """普世线(v0.4.17):比例 ≥ 50% 才 PASS——15/30 过线,14/30 只能 WEAK;< 20% 才 FAIL——6/30 是 WEAK,5/30 是 FAIL。"""
    assert diagnose_citation(make_metric(25, 30)).status is StageStatus.PASS
    assert diagnose_citation(make_metric(15, 30)).status is StageStatus.PASS
    assert diagnose_citation(make_metric(14, 30)).status is StageStatus.WEAK
    assert diagnose_citation(make_metric(6, 30)).status is StageStatus.WEAK
    assert diagnose_citation(make_metric(5, 30)).status is StageStatus.FAIL


def test_citation_low_confidence_caps_definitive_to_weak():
    metric = make_metric(0, 30, failed=15)  # 无效样本占 1/3 > 20%
    finding = diagnose_citation(metric)
    assert finding.status is StageStatus.WEAK
    assert finding.low_confidence is True
    assert any("LOW_CONFIDENCE" in line for line in zh_lines(finding))


def test_citation_verification_lines():
    from diagnosis.citation_diag import VerificationSummary

    verification = VerificationSummary(
        attempted=5, verified_match=1, content_mismatch=1, fetch_blocked=2, unparsed=1, unreachable=0,
    )
    assert verification.decidable == 2 and verification.coverage == 0.4
    finding = diagnose_citation(make_metric(6, 30), verification=verification)
    lines = "\n".join(zh_lines(finding))
    assert "三档证据口径" in lines and "不轻信平台引用" in lines
    assert "已核验被引概率" not in lines  # v0.4.13:运行级核验概率已撤销
    assert "正文见品牌 1" in lines and "被拦截(401/403/429/503)2" in lines and "非 HTML 未解析 1" in lines
    assert "核验覆盖率:2/5 = 40%" in lines
    assert "不说明平台引用有问题" in lines  # 抓不到 ≠ 引用无效


def test_citation_verification_coverage_none_when_nothing_attempted():
    from diagnosis.citation_diag import VerificationSummary

    verification = VerificationSummary(
        attempted=0, verified_match=0, content_mismatch=0, fetch_blocked=0, unparsed=0, unreachable=0,
    )
    assert verification.coverage is None
    lines = "\n".join(zh_lines(diagnose_citation(make_metric(0, 30), verification=verification)))
    assert "核验覆盖率" not in lines and "0 条唯一被引 URL" in lines


def test_citation_competitor_comparison_lines():
    """竞品对比只认相差 ≥ 20 个百分点;差距不足就说分不出,不硬排序。"""
    focal = make_metric(3, 30)  # 10%
    finding = diagnose_citation(focal, [("竞品甲", rate(20, 30)), ("竞品乙", rate(6, 30)), ("竞品丙", rate(0, 30))])
    lines = "\n".join(zh_lines(finding))
    assert "落后竞品 竞品甲 20% 个百分点以上(其被引 20/30 = 67%)" in lines
    assert "与竞品 竞品乙 相差不足 20%,分不出高低" in lines
    assert "与竞品 竞品丙 相差不足 20%,分不出高低" in lines  # 10% vs 0%:差 10,不够
    ahead = "\n".join(zh_lines(diagnose_citation(make_metric(20, 30), [("竞品丁", rate(0, 30))])))
    assert "领先竞品 竞品丁 20% 个百分点以上" in ahead


# ── REPRESENTATION ───────────────────────────────────────────────────


def test_representation_neutral_low_mention_fails_with_na_wording():
    finding = diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(0, 30), {})
    assert finding.status is StageStatus.FAIL
    assert "未稳定进入生成回答" in zh(finding.headline)
    assert any("提及样本不足" in line for line in zh_lines(finding))


def test_representation_neutral_pass_and_negative_cap():
    counts_ok = {Sentiment.POSITIVE: 5, Sentiment.NEUTRAL: 3}
    assert (
        diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(27, 30), counts_ok).status
        is StageStatus.PASS
    )
    counts_negative = {Sentiment.POSITIVE: 3, Sentiment.NEGATIVE: 3}
    capped = diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(27, 30), counts_negative)
    assert capped.status is StageStatus.WEAK
    assert any("封顶 WEAK" in line for line in zh_lines(capped))


def test_representation_brand_fact_judged_by_sentiment_only():
    metric = make_metric(30, 30)  # 点名品牌的题提及率天然高,不参与判定
    negative = {Sentiment.NEGATIVE: 3, Sentiment.POSITIVE: 2}
    assert diagnose_representation(PromptIntent.BRAND_FACT, metric, negative).status is StageStatus.FAIL
    mild = {Sentiment.NEGATIVE: 2, Sentiment.POSITIVE: 4}
    assert diagnose_representation(PromptIntent.BRAND_FACT, metric, mild).status is StageStatus.WEAK
    healthy = {Sentiment.POSITIVE: 4, Sentiment.NEUTRAL: 2}
    assert diagnose_representation(PromptIntent.BRAND_FACT, metric, healthy).status is StageStatus.PASS


def test_representation_claims_note_always_present():
    finding = diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(27, 30), {})
    assert any("品牌事实清单" in line for line in zh_lines(finding))
