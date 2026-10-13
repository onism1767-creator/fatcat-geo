"""第三方线诊断(架构书附录 v0.4.24 第 2 节 D2 / D3;v0.4.26 S6.1):搜到写到你的第三方网页 → 引用写到你的第三方网页。

与官网线各自亮灯、各自给「N 次里 k 次」、各自定位,永不合并成一个灯或一个数(用户定)。
判定线沿用普世 20 / 50。「写到你」= 打开网页后正文里出现品牌名或别名(engine/detectors/page_mention.py);
看不到的网页按原因单独给数量,不进分母。输出为结论码 + 参数,组句在渲染层。
"""

from __future__ import annotations

from collections.abc import Mapping

from diagnosis.findings import (
    DiagnosisThresholds,
    Line,
    Msg,
    Stage,
    StageFinding,
    StageStatus,
    caliber_msg,
    msg,
    prompt_msgs,
    rate_params,
    rate_status,
)
from engine.aggregate import ArchiveTally, ClassSurfaceMetric
from engine.metrics import Rate

_STEP = {Stage.RETRIEVAL: "retrieved", Stage.CITATION: "cited"}
_REASON_ORDER = ("ROBOTS_DISALLOWED", "BLOCKED", "LOGIN_REQUIRED", "EMPTY_TEXT", "NOT_HTML", "FETCH_FAILED",
                 "NOT_ATTEMPTED", "UNRESOLVED")


def diagnose_third_party(
    stage: Stage,
    metric: ClassSurfaceMetric | None,
    lead: Rate | None,
    unobservable: Mapping[str, int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
    summary_only: Mapping[str, int] | None = None,
    archived: ArchiveTally | None = None,
) -> StageFinding:
    """第三方线的一步(stage = RETRIEVAL 搜到 / CITATION 引用)。无有效样本或该仪器不暴露这一步 → 看不到。"""
    step = _STEP[stage]
    if metric is None or metric.rate is None:
        return StageFinding(
            stage=stage, line=Line.THIRD_PARTY, status=StageStatus.NOT_OBSERVABLE,
            headline=msg(f"thirdparty.{step}.headline.not_observable"),
            evidence_lines=[_unobservable_line(unobservable)] if unobservable else [],
        )
    r = metric.rate
    fail_below = thresholds.citation_fail_below if stage is Stage.CITATION else thresholds.retrieval_fail_below
    pass_from = thresholds.citation_pass_from if stage is Stage.CITATION else thresholds.retrieval_pass_from
    status, notes = rate_status(r, low_confidence=metric.accounting.low_confidence,
                                fail_below=fail_below, pass_from=pass_from)
    evidence: list[Msg] = [msg(f"thirdparty.{step}.rate", **rate_params(r))]
    if lead is not None:
        evidence.append(msg("thirdparty.lead", **rate_params(lead)))  # 技术栏:标题或首段就出现
    if unobservable:
        evidence.append(_unobservable_line(unobservable))
    if summary_only:
        evidence.append(msg("thirdparty.summary_only", n=sum(summary_only.values()), items=[
            msg("thirdparty.reason_item", reason=msg(f"textsource.{source}"), n=count)
            for source, count in sorted(summary_only.items())
        ]))
    if archived is not None:
        evidence.append(msg("thirdparty.archived", n=archived.pages, k=archived.mentioning,
                            oldest=archived.oldest or "?", newest=archived.newest or "?"))
    evidence.append(caliber_msg(metric))
    evidence.extend(prompt_msgs(metric))
    evidence.append(msg("citation.thresholds" if stage is Stage.CITATION else "retrieval.thresholds",
                        fail=fail_below, pass_=pass_from))
    evidence.extend(notes)
    return StageFinding(
        stage=stage, line=Line.THIRD_PARTY, status=status,
        headline=msg(f"thirdparty.{step}.headline.{status.value.lower()}"),
        evidence_lines=evidence, low_confidence=metric.accounting.low_confidence,
    )


def _unobservable_line(unobservable: Mapping[str, int]) -> Msg:
    items = [
        msg("thirdparty.reason_item", reason=msg(f"pagereason.{reason}"), n=unobservable[reason])
        for reason in sorted(unobservable, key=lambda k: _REASON_ORDER.index(k) if k in _REASON_ORDER else 99)
    ]
    return msg("thirdparty.unobservable", n=sum(unobservable.values()), items=items)


def localize_third_party(findings: list[StageFinding], *, skip: frozenset[Stage] = frozenset()) -> Stage | None:
    """第三方线自己的「最主要卡在哪一步」:第一个 FAIL,否则第一个 WEAK;看不到的跳过。"""
    usable = [f for f in findings if f.status is not StageStatus.NOT_OBSERVABLE and f.stage not in skip]
    for wanted in (StageStatus.FAIL, StageStatus.WEAK):
        for finding in usable:
            if finding.status is wanted:
                return finding.stage
    return None
