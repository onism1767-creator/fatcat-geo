"""L3 阶段诊断表驱动测试(12.1):阶段指标输入 → 期望状态。

用例刻意选在远离阈值边界的清晰区,避免数值临界抖动。
"""

from diagnosis.citation_diag import diagnose_citation
from diagnosis.findings import Stage, StageStatus
from diagnosis.messages import render_msg


def zh(message) -> str:
    return render_msg(message, "zh")


def zh_lines(finding) -> list[str]:
    return [zh(line) for line in finding.evidence_lines]
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import diagnose_retrieval
from engine.metrics import account_outcomes, estimate_proportion
from engine.models import NeedIntent, Outcome, Sentiment, StopReason
from engine.scheduler import NeedSurfaceMetric


def make_metric(k: int, n: int, *, refused: int = 0, failed: int = 0) -> NeedSurfaceMetric:
    outcomes = (
        [Outcome.SUCCESS] * n
        + [Outcome.REFUSED] * refused
        + [Outcome.TRANSIENT_FAILURE] * failed
    )
    return NeedSurfaceMetric(
        information_need_id="need-1",
        provider="test_provider",
        surface="test_surface",
        model="m1",
        metric_label="test_metric",
        k_hits=k,
        estimate=estimate_proportion(k, n) if n else None,
        accounting=account_outcomes(outcomes),
        stop_reasons=[StopReason.CI_CONVERGED],
        per_variant=[],
    )


# ── RETRIEVAL ────────────────────────────────────────────────────────


def test_retrieval_not_observable_without_metric():
    finding = diagnose_retrieval(None, [])
    assert finding.status is StageStatus.NOT_OBSERVABLE
    assert finding.stage is Stage.RETRIEVAL


def test_retrieval_absent_fails_with_rank_evidence():
    finding = diagnose_retrieval(make_metric(0, 30), [])
    assert finding.status is StageStatus.FAIL
    assert any("从未进入检索候选" in line for line in zh_lines(finding))
    assert any("判定阈值" in line for line in zh_lines(finding))  # 阈值自报进口径


def test_retrieval_stable_presence_passes_with_rank_summary():
    finding = diagnose_retrieval(make_metric(27, 30), [3, 7, 5])
    assert finding.status is StageStatus.PASS
    assert any("中位数第 5 位" in line for line in zh_lines(finding))


def test_retrieval_middling_is_weak():
    assert diagnose_retrieval(make_metric(3, 10), [8]).status is StageStatus.WEAK


def test_retrieval_low_confidence_caps_fail_to_weak():
    metric = make_metric(0, 30, failed=15)  # 无效样本占 1/3 > 20%
    finding = diagnose_retrieval(metric, [])
    assert finding.status is StageStatus.WEAK
    assert finding.low_confidence is True
    assert any("LOW_CONFIDENCE" in line for line in zh_lines(finding))


# ── CITATION ─────────────────────────────────────────────────────────


def test_citation_absent_fails():
    assert diagnose_citation(make_metric(0, 30)).status is StageStatus.FAIL


def test_citation_small_sample_zero_is_only_weak():
    """0/8 与 0/30 不是同一强度的结论:样本少只敢说 WEAK。"""
    assert diagnose_citation(make_metric(0, 8)).status is StageStatus.WEAK


def test_citation_stable_passes():
    assert diagnose_citation(make_metric(15, 30)).status is StageStatus.PASS


def test_citation_verification_lines():
    from diagnosis.citation_diag import VerificationSummary

    verification = VerificationSummary(
        estimate=estimate_proportion(3, 30), checked_urls=5, unreachable=1, soft_misses=2,
    )
    finding = diagnose_citation(make_metric(6, 30), verification=verification)
    lines = "\n".join(zh_lines(finding))
    assert "三档证据口径" in lines and "不轻信平台引用" in lines
    assert "已核验被引概率" in lines
    assert "不可达 1 条" in lines and "疑似软 404" in lines


def test_citation_competitor_comparison_lines():
    focal = make_metric(0, 30)
    strong_comp = estimate_proportion(20, 30)
    overlap_comp = estimate_proportion(1, 30)
    finding = diagnose_citation(focal, [("竞品甲", strong_comp), ("竞品乙", overlap_comp)])
    lines = "\n".join(zh_lines(finding))
    assert "显著落后于竞品 竞品甲" in lines
    assert "与竞品 竞品乙 区间重叠,无法区分" in lines  # 重叠禁止硬排序


# ── REPRESENTATION ───────────────────────────────────────────────────


def test_representation_neutral_low_mention_fails_with_na_wording():
    finding = diagnose_representation(NeedIntent.DISCOVERY, make_metric(0, 30), {})
    assert finding.status is StageStatus.FAIL
    assert "未稳定进入生成回答" in zh(finding.headline)
    assert any("提及样本不足" in line for line in zh_lines(finding))


def test_representation_neutral_pass_and_negative_cap():
    counts_ok = {Sentiment.POSITIVE: 5, Sentiment.NEUTRAL: 3}
    assert (
        diagnose_representation(NeedIntent.DISCOVERY, make_metric(27, 30), counts_ok).status
        is StageStatus.PASS
    )
    counts_negative = {Sentiment.POSITIVE: 3, Sentiment.NEGATIVE: 3}
    capped = diagnose_representation(NeedIntent.DISCOVERY, make_metric(27, 30), counts_negative)
    assert capped.status is StageStatus.WEAK
    assert any("封顶 WEAK" in line for line in zh_lines(capped))


def test_representation_probe_judged_by_sentiment_only():
    metric = make_metric(30, 30)  # 探针下提及率天然高,不参与判定
    negative = {Sentiment.NEGATIVE: 3, Sentiment.POSITIVE: 2}
    assert (
        diagnose_representation(NeedIntent.BRAND_PROBE, metric, negative).status
        is StageStatus.FAIL
    )
    mild = {Sentiment.NEGATIVE: 2, Sentiment.POSITIVE: 4}
    assert (
        diagnose_representation(NeedIntent.BRAND_PROBE, metric, mild).status
        is StageStatus.WEAK
    )
    healthy = {Sentiment.POSITIVE: 4, Sentiment.NEUTRAL: 2}
    assert (
        diagnose_representation(NeedIntent.BRAND_PROBE, metric, healthy).status
        is StageStatus.PASS
    )


def test_representation_claims_note_always_present():
    finding = diagnose_representation(NeedIntent.DISCOVERY, make_metric(27, 30), {})
    assert any("品牌事实清单" in line for line in zh_lines(finding))
