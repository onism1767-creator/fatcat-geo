"""CITATION 诊断:AI 会选择你当参考来源吗?(5.2)

输出被引比例 + 竞品差距。竞品对比只认「相差 ≥ rate_gap 个百分点」,差距不足就说分不出
——禁止在差不多的比例上硬排序,与"无总分"同源。
输出为结论码 + 参数(v1.1),组句在渲染层。
"""

from __future__ import annotations

from diagnosis.findings import (
    DiagnosisThresholds,
    Msg,
    Stage,
    StageFinding,
    StageStatus,
    caliber_msg,
    compare_rates,
    msg,
    prompt_msgs,
    rate_params,
    rate_status,
)
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate
from engine.models import FrozenModel

_HEADLINES = {
    StageStatus.FAIL: "citation.headline.fail",
    StageStatus.WEAK: "citation.headline.weak",
    StageStatus.PASS: "citation.headline.pass",
}


def _compare_line(focal: Rate, name: str, competitor: Rate, gap: float) -> Msg:
    code = {
        -1: "citation.compare.behind",
        1: "citation.compare.ahead",
        0: "citation.compare.close",
    }[compare_rates(focal, competitor, gap)]
    return msg(code, name=name, gap=gap, **rate_params(competitor))


class VerificationSummary(FrozenModel):
    """三档证据「已核验」层的题型级汇总:被引 URL 的四态计数(v0.4.13 条目 5)。

    URL 级而非运行级——「已核验被引概率」已撤销,它把「平台有没有引用」与
    「本工具能不能再抓到」混成一个数。只有 content_mismatch 是负面证据。
    """

    attempted: int  # 尝试核验的唯一 URL 数
    verified_match: int
    content_mismatch: int
    fetch_blocked: int
    unparsed: int
    unreachable: int

    @property
    def decidable(self) -> int:
        return self.verified_match + self.content_mismatch

    @property
    def coverage(self) -> float | None:
        """核验覆盖率 = 可判定 / 尝试;没尝试过则 None。"""
        return self.decidable / self.attempted if self.attempted else None


def diagnose_citation(
    metric: ClassSurfaceMetric | None,
    competitors: list[tuple[str, Rate]] | None = None,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
    verification: VerificationSummary | None = None,
) -> StageFinding:
    if metric is None or metric.rate is None:
        return StageFinding(
            stage=Stage.CITATION,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("citation.headline.not_observable"),
        )
    r = metric.rate
    accounting = metric.accounting
    status, notes = rate_status(
        r,
        low_confidence=accounting.low_confidence,
        fail_below=thresholds.citation_fail_below,
        pass_from=thresholds.citation_pass_from,
    )
    evidence = [
        msg("citation.rate", **rate_params(r)),
        caliber_msg(metric),
        *prompt_msgs(metric),
        msg("citation.thresholds",
            fail=thresholds.citation_fail_below, pass_=thresholds.citation_pass_from),
    ]
    if verification is not None:
        evidence.append(msg("citation.tier_note"))
        evidence.append(msg(
            "citation.verify_tally",
            attempted=verification.attempted,
            match=verification.verified_match,
            mismatch=verification.content_mismatch,
            blocked=verification.fetch_blocked,
            unparsed=verification.unparsed,
            unreachable=verification.unreachable,
        ))
        if verification.coverage is not None:
            evidence.append(msg(
                "citation.verify_coverage",
                decidable=verification.decidable,
                attempted=verification.attempted,
                coverage=verification.coverage,
            ))
    evidence.extend(_compare_line(r, name, comp, thresholds.rate_gap) for name, comp in (competitors or []))
    evidence.extend(notes)
    return StageFinding(
        stage=Stage.CITATION,
        status=status,
        headline=msg(_HEADLINES[status]),
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
