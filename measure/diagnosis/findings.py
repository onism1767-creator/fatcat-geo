"""L3 共享类型:阶段、状态、阈值与阶段结论(第 5 章诊断层词汇)。

阈值是产品口径的一部分:每个 StageFinding 在 evidence_lines 里自报
所用阈值,报告渲染时随指标一并展示(铁律 5)。
LOW_CONFIDENCE 的指标不允许给出确定性 PASS/FAIL 结论,一律降权 WEAK(7.3)。
"""

from __future__ import annotations

from enum import StrEnum

from engine.metrics import ProportionEstimate
from engine.models import FrozenModel


class Stage(StrEnum):
    ACCESS = "ACCESS"
    RETRIEVAL = "RETRIEVAL"
    CITATION = "CITATION"
    REPRESENTATION = "REPRESENTATION"


class StageStatus(StrEnum):
    PASS = "PASS"
    WEAK = "WEAK"
    FAIL = "FAIL"
    NOT_OBSERVABLE = "NOT_OBSERVABLE"


class DiagnosisThresholds(FrozenModel):
    """阶段判定阈值(可配;默认值为 v1 产品口径,随结论自报)。

    判定一律基于 95% 区间端点而非点估计:FAIL 要求区间上界都低于阈值
    (确信缺席),PASS 要求区间下界都高于阈值(确信在场),两者之间 WEAK。
    """

    retrieval_fail_ci_high: float = 0.20
    retrieval_pass_ci_low: float = 0.40
    # 0.15:k=0 时上界随 n 收窄(0/20≈0.13 → FAIL;0/14≈0.20 → 仅 WEAK)
    # ——样本越少越不敢下定论,这是特性不是缺陷
    citation_fail_ci_high: float = 0.15
    citation_pass_ci_low: float = 0.25
    mention_fail_ci_high: float = 0.20
    mention_pass_ci_low: float = 0.50
    negative_share_weak: float = 0.30  # 情感负面占比超此值,状态封顶 WEAK
    negative_share_fail: float = 0.50  # 探针需求下负面过半 → FAIL


class StageFinding(FrozenModel):
    stage: Stage
    status: StageStatus
    headline: str
    evidence_lines: list[str] = []
    low_confidence: bool = False


def fmt_estimate(estimate: ProportionEstimate) -> str:
    return (
        f"{estimate.point:.0%} [95% 区间(Jeffreys): "
        f"{estimate.ci_low:.0%}–{estimate.ci_high:.0%}](N={estimate.n})"
    )


def interval_status(
    estimate: ProportionEstimate,
    *,
    low_confidence: bool,
    fail_ci_high: float,
    pass_ci_low: float,
) -> tuple[StageStatus, list[str]]:
    """区间三态判定 + LOW_CONFIDENCE 降权。返回 (状态, 附注行)。"""
    if estimate.ci_high <= fail_ci_high:
        status = StageStatus.FAIL
    elif estimate.ci_low >= pass_ci_low:
        status = StageStatus.PASS
    else:
        status = StageStatus.WEAK
    notes: list[str] = []
    if low_confidence and status is not StageStatus.WEAK:
        notes.append("无效样本占比超阈值(LOW_CONFIDENCE),确定性结论降权为 WEAK(7.3)")
        status = StageStatus.WEAK
    return status, notes
