"""L3 共享类型:阶段、状态、阈值与阶段结论(第 5 章诊断层词汇)。

v1.1 结构化输出(16.5):L3 只产出**结论码 + 参数**(Msg),不产出任何
成品语句;组句发生在渲染层(diagnosis/messages.py 的目录,zh/en 双语)。
阈值是产品口径的一部分:每个 StageFinding 在 evidence_lines 里自报
所用阈值,报告渲染时随指标一并展示(铁律 5)。
LOW_CONFIDENCE 的指标不允许给出确定性 PASS/FAIL 结论,一律降权 WEAK(7.3)。
判定直接看比例(v0.4.26 S2,用户定 T1):不再算区间——同一道题的几次回答是一簇,区间是虚假精度。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from diagnosis.messages import Msg, msg
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate, SampleAccounting
from engine.models import FrozenModel


class Stage(StrEnum):
    ACCESS = "ACCESS"
    RETRIEVAL = "RETRIEVAL"
    CITATION = "CITATION"
    REPRESENTATION = "REPRESENTATION"


class Line(StrEnum):
    """哪条线(架构书附录 v0.4.24 D2 / D3):官网线与第三方线各自亮灯、各自定位,永不合并。"""

    OWN = "own"
    THIRD_PARTY = "third_party"


class StageStatus(StrEnum):
    PASS = "PASS"
    WEAK = "WEAK"
    FAIL = "FAIL"
    NOT_OBSERVABLE = "NOT_OBSERVABLE"


class DiagnosisThresholds(FrozenModel):
    """阶段判定阈值(可配;默认值为 v1 操作阈值,随结论自报)。

    普世线(v0.4.17 用户拍板;v0.4.26 S2 改为直接看比例,线不变):比例 < 20% 为 FAIL,≥ 50% 为 PASS,
    中间 WEAK。红绿灯在所有项目上含义一致,行业差异靠竞品对比体现而非靠线。

    rate_gap:两个比例相差 ≥ 20 个百分点才说「不一样」——竞品对比的领先 / 落后、同仪器漏斗的落差、
    检索依赖的开闭卷差别都用它。S2 初始值,待用户过目(架构书附录 v0.4.26 拍板结果 T1)。
    """

    retrieval_fail_below: float = 0.20
    retrieval_pass_from: float = 0.50
    citation_fail_below: float = 0.20
    citation_pass_from: float = 0.50
    mention_fail_below: float = 0.20
    mention_pass_from: float = 0.50
    negative_share_weak: float = 0.30  # 情感负面占比超此值,状态封顶 WEAK
    negative_share_fail: float = 0.50  # 品牌事实题下负面过半 → FAIL
    rate_gap: float = 0.20
    dependency_high_ratio: float = 0.5  # 检索依赖(第 9 章):闭卷比例 < 开卷比例 × 此值 → HIGH


class StageFinding(FrozenModel):
    stage: Stage
    status: StageStatus
    line: Line = Line.OWN
    headline: Msg
    evidence_lines: list[Msg] = []
    low_confidence: bool = False
    rate: Rate | None = None  # 这一步的主指标 k/n(S8.7:主视图的格子直接用;看不到 → None)
    failed_checks: list[str] = []  # ACCESS:官网审计里 FAIL 的检查项名,建议按它过滤(审核 2026-10-27 黄 14)


class SentimentTally(FrozenModel):
    """提到你的句子按好坏计数(S8.1,用户定:「提到你的 18 句话里:正面 1、中性 9、负面 8」+ 百分比饼图)。
    四类互斥、一句只算一类,所以可以画成一张饼;一句都没有 → 调用方给 None,不画。"""

    positive: int
    neutral: int
    negative: int
    mixed: int

    @property
    def total(self) -> int:
        return self.positive + self.neutral + self.negative + self.mixed


class MentionShare(FrozenModel):
    """一个名字在本次有效回答里被提到的次数(S7 竞品提及对比)。
    relation 是相对于你:more / less / close;你自己这一行 None。"""

    name: str
    rate: Rate
    relation: str | None = None


class MentionCompare(FrozenModel):
    """竞品提及对比(S7,用户定:措辞 A、放主视图 P1、带百分比和每名一环):提到你 k/N、提到各竞品几次;
    差距 ≥ gap(比例)才说多 / 少,否则差不多。一次回答可同时提到几家,各家各算各的比例,不是一张饼。"""

    brand: MentionShare
    competitors: list[MentionShare]
    gap: float


def rate_params(r: Rate) -> dict[str, Any]:
    return {"k": r.k, "n": r.n, "share": r.share}  # 用户视图用原始计数「N 次里 k 次」


def accounting_params(accounting: SampleAccounting) -> dict[str, Any]:
    """口径里的分母与三种无效的数量(产品文档 15.2:无效回答不进分母,单独显示数量)。"""
    return {
        "n_valid": accounting.n_valid,
        "n_total": accounting.n_total,
        "n_refused": accounting.n_refused,
        "n_empty": accounting.n_empty,
        "n_failed": accounting.n_transient_failure + accounting.n_permanent_failure,
    }


def caliber_msg(metric: ClassSurfaceMetric) -> Msg:
    return msg(
        "common.caliber",
        surface=metric.surface,
        model=metric.model,
        n_prompts=len(metric.per_prompt),
        stop_reasons=", ".join(r.value for r in metric.stop_reasons),
        **accounting_params(metric.accounting),
    )


def prompt_msgs(metric: ClassSurfaceMetric) -> list[Msg]:
    """逐题并列 + 题目覆盖率(产品文档 15.2):72 次里的 31 次命中可能全集中在少数几道题上。"""
    items = [
        msg("common.prompt_item", idx=i, **rate_params(r))
        if r is not None else msg("common.prompt_item_none", idx=i)
        for i, r in enumerate(metric.per_prompt, start=1)
    ]
    lines: list[Msg] = []
    if len(items) >= 2:
        lines.append(msg("common.per_prompt", items=items))
    if metric.prompt_coverage is not None:
        lines.append(msg("common.prompt_coverage", **rate_params(metric.prompt_coverage)))
    return lines


def rate_status(
    r: Rate,
    *,
    low_confidence: bool,
    fail_below: float,
    pass_from: float,
) -> tuple[StageStatus, list[Msg]]:
    """比例三态判定 + LOW_CONFIDENCE 降权。返回 (状态, 附注)。"""
    if r.share < fail_below:
        status = StageStatus.FAIL
    elif r.share >= pass_from:
        status = StageStatus.PASS
    else:
        status = StageStatus.WEAK
    notes: list[Msg] = []
    if low_confidence and status is not StageStatus.WEAK:
        notes.append(msg("common.low_confidence_downgrade"))
        status = StageStatus.WEAK
    return status, notes


def compare_rates(a: Rate, b: Rate, gap: float) -> int:
    """两个比例的差别:a 比 b 高 ≥ gap → 1;低 ≥ gap → −1;差距不足 gap → 0(分不出)。
    差值先四舍五入到小数点后 9 位:3/5 − 2/5 在浮点里是 0.19999…,不能因此把「正好差 20 个百分点」判成分不出(S7)。"""
    diff = round(a.share - b.share, 9)
    if diff >= gap:
        return 1
    if -diff >= gap:
        return -1
    return 0
