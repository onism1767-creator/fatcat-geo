"""L3 共享类型:阶段、状态、阈值与阶段结论(第 5 章诊断层词汇)。

v1.1 结构化输出(16.5):L3 只产出**结论码 + 参数**(Msg),不产出任何
成品语句;组句发生在渲染层(diagnosis/messages.py 的目录,zh/en 双语)。
阈值是产品口径的一部分:每个 StageFinding 在 evidence_lines 里自报
所用阈值,报告渲染时随指标一并展示(铁律 5)。
LOW_CONFIDENCE 的指标不允许给出确定性 PASS/FAIL 结论,一律降权 WEAK(7.3)。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from diagnosis.messages import Msg, msg
from engine.aggregate import NeedSurfaceMetric
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
    """阶段判定阈值(可配;默认值为 v1 操作阈值,随结论自报)。

    判定一律基于 95% 区间端点而非点估计:FAIL 要求区间上界都低于阈值
    (确信缺席),PASS 要求区间下界都高于阈值(确信在场),两者之间 WEAK。

    v0.4.17(用户拍板):三项统一为「普世线」——整个区间 ≤ 20% 才 FAIL,整个区间
    ≥ 50% 才 PASS,中间黄区留宽;红绿灯在所有项目上含义一致,行业差异靠竞品对比
    体现而非靠线。样本少时 k=0 的上界收不进 20%(0/8 ≈ 0.26)只判 WEAK——
    样本越少越不敢下定论,这是特性不是缺陷。
    """

    retrieval_fail_ci_high: float = 0.20
    retrieval_pass_ci_low: float = 0.50
    citation_fail_ci_high: float = 0.20
    citation_pass_ci_low: float = 0.50
    mention_fail_ci_high: float = 0.20
    mention_pass_ci_low: float = 0.50
    negative_share_weak: float = 0.30  # 情感负面占比超此值,状态封顶 WEAK
    negative_share_fail: float = 0.50  # 探针需求下负面过半 → FAIL
    # 检索依赖(第 9 章):闭卷区间上界 < 开卷区间下界 × 此值 → HIGH(2026-09 用户拍板)
    dependency_high_ratio: float = 0.5


class StageFinding(FrozenModel):
    stage: Stage
    status: StageStatus
    headline: Msg
    evidence_lines: list[Msg] = []
    low_confidence: bool = False


def estimate_params(estimate: ProportionEstimate) -> dict[str, Any]:
    return {
        "point": estimate.point,
        "low": estimate.ci_low,
        "high": estimate.ci_high,
        "n": estimate.n,
        "k": estimate.k,  # 用户视图用原始计数「N 次里 k 次」,不出单点概率
    }


def caliber_msg(metric: NeedSurfaceMetric) -> Msg:
    accounting = metric.accounting
    return msg(
        "common.caliber",
        surface=metric.surface,
        model=metric.model,
        n_valid=accounting.n_valid,
        n_total=accounting.n_total,
        stop_reasons=", ".join(r.value for r in metric.stop_reasons),
        refusal=accounting.refusal_rate,
        failure=accounting.failure_rate,
        n_variants=len(metric.variant_estimates),
    )


def variant_msgs(metric: NeedSurfaceMetric) -> list[Msg]:
    """问法并列 + 措辞敏感警示(v0.4.13 条目 2)。问法是语义层的独立单位,需求层
    合并估计只是冻结问法集上的观测率;任意两问法可信区间不重叠即措辞敏感。
    单问法无并列可言,返回空。"""
    items = [
        msg("common.variant_item", idx=i, **estimate_params(e))
        if e is not None else msg("common.variant_item_none", idx=i)
        for i, e in enumerate(metric.variant_estimates, start=1)
    ]
    if len(items) < 2:
        return []
    lines = [msg("common.per_variant", items=items)]
    estimates = [e for e in metric.variant_estimates if e is not None]
    if any(
        a.ci_high < b.ci_low or b.ci_high < a.ci_low
        for i, a in enumerate(estimates)
        for b in estimates[i + 1:]
    ):
        lines.append(msg("common.variant_sensitive"))
    return lines


def interval_status(
    estimate: ProportionEstimate,
    *,
    low_confidence: bool,
    fail_ci_high: float,
    pass_ci_low: float,
) -> tuple[StageStatus, list[Msg]]:
    """区间三态判定 + LOW_CONFIDENCE 降权。返回 (状态, 附注)。"""
    if estimate.ci_high <= fail_ci_high:
        status = StageStatus.FAIL
    elif estimate.ci_low >= pass_ci_low:
        status = StageStatus.PASS
    else:
        status = StageStatus.WEAK
    notes: list[Msg] = []
    if low_confidence and status is not StageStatus.WEAK:
        notes.append(msg("common.low_confidence_downgrade"))
        status = StageStatus.WEAK
    return status, notes
