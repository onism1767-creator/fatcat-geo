"""L2 聚合:采样结果 + 逐条观测 → 各被测对象在一道题上的指标(架构书附录 v0.4.24 第 5 节 ③)。

只算数,不下结论:阶段状态、定位、建议都是 L3 的事。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from engine.detectors.brand_mention import DETECTOR_NAME as MENTION_LABEL
from engine.detectors.citation import DETECTOR_NAME as CITATION_LABEL
from engine.detectors.retrieval_rank import DETECTOR_NAME as PRESENCE_LABEL
from engine.metrics import ProportionEstimate, SampleAccounting, estimate_proportion, merge_accounting
from engine.models import UNKNOWN, FrozenModel, Outcome, Sentiment, StopReason, Target
from engine.observe import RunObservations
from engine.scheduler import SamplingResult


class NeedSurfaceMetric(FrozenModel):
    """需求层指标(第 3 章步骤 7):同一 InformationNeed 在一个 surface 上
    跨全部问法的合并观测。合并是各问法命中的混合比例,正式名称是
    「冻结问法集上的观测率」(v0.4.13 条目 2):问法是语义层的独立单位,运行只是
    问法内的重复,合并估计不外推到需求总体;问法间的差异靠 variant_estimates
    并列展示,per_variant 保留完整明细。"""

    information_need_id: str
    provider: str
    surface: str
    model: str
    metric_label: str  # 命中口径(通常为判定器名),如 citation_domain_match
    k_hits: int
    estimate: ProportionEstimate | None
    accounting: SampleAccounting
    stop_reasons: list[StopReason]  # 逐问法,随口径进报告
    per_variant: list[SamplingResult]
    variant_estimates: list[ProportionEstimate | None]  # 逐问法,与问法同序;分母为空则 None


def aggregate_need_surface(
    results: list[SamplingResult], *, metric_label: str
) -> NeedSurfaceMetric:
    """按采样时的收敛判据合并(命中数取自采样结果本身)。"""
    if not results:
        raise ValueError("没有可聚合的采样结果")
    heads = {(r.information_need_id, r.provider, r.surface, r.model) for r in results}
    if len(heads) != 1:
        raise ValueError(f"只能聚合同一 (需求, surface, model) 的结果,收到 {heads}")
    k_hits = sum(r.k_hits for r in results)
    n_pooled = sum(r.estimate.n for r in results if r.estimate)
    need_id, provider, surface, model = next(iter(heads))
    return NeedSurfaceMetric(
        information_need_id=need_id,
        provider=provider,
        surface=surface,
        model=model,
        metric_label=metric_label,
        k_hits=k_hits,
        estimate=estimate_proportion(k_hits, n_pooled) if n_pooled else None,
        accounting=merge_accounting(r.accounting for r in results),
        stop_reasons=[r.stop_reason for r in results],
        per_variant=results,
        variant_estimates=[r.estimate for r in results],
    )


def estimate_of(flags: list[bool]) -> ProportionEstimate | None:
    """一组是/否观测的比例估计;没有观测 → None(不进分母,不是 0)。"""
    return estimate_proportion(sum(flags), len(flags)) if flags else None


def _variant_estimates(
    observations: list[RunObservations],
    variant_ids: list[str],
    hit: Callable[[RunObservations], bool | None],
) -> list[ProportionEstimate | None]:
    """按问法分组的估计(与问法同序),供需求层指标并列展示问法间分歧(v0.4.13 条目 2)。"""
    hits: dict[str, list[bool]] = {vid: [] for vid in variant_ids}
    for obs in observations:
        value = hit(obs)
        if value is not None and obs.run.prompt_variant_id in hits:
            hits[obs.run.prompt_variant_id].append(value)
    return [estimate_of(v) for v in hits.values()]


def rate_metric(
    results: list[SamplingResult],
    observations: list[RunObservations],
    variant_ids: list[str],
    hit: Callable[[RunObservations], bool | None],
    *,
    metric_label: str,
) -> NeedSurfaceMetric:
    """按另一个判定重新数命中的指标(采样的收敛判据是引用,提及率 / 在场率要按各自判定器重数)。
    口径(问法数、停止原因、样本账)沿用这批采样结果;逐问法明细不带(它记的是收敛判据的命中)。"""
    flags = [value for obs in observations if (value := hit(obs)) is not None]
    return aggregate_need_surface(results, metric_label=metric_label).model_copy(update={
        "k_hits": sum(flags),
        "estimate": estimate_of(flags),
        "per_variant": [],
        "variant_estimates": _variant_estimates(observations, variant_ids, hit),
    })


@dataclass(frozen=True)
class GenerativeRetrieval:
    """生成端检索链路的原料(v0.4.8):本仪器自己暴露检索词与候选集时才有。"""

    presence: NeedSurfaceMetric | None  # 品牌官网进候选集的比例(候选集全不可观测时 None)
    best_ranks: list[int]
    query: ProportionEstimate | None  # 品牌名进自拟检索词的比例
    cited: ProportionEstimate | None  # 同仪器的被引比例(同仪器漏斗)
    triggered: ProportionEstimate | None  # 触发检索的比例


@dataclass(frozen=True)
class BareRetrieval:
    """裸检索通道(某家自己的搜索接口,如双通道架构 v0.4.8)的检索指标。"""

    target: Target
    provider: str
    metric: NeedSurfaceMetric
    best_ranks: list[int]


@dataclass(frozen=True)
class TargetMeasurement:
    """一个能回答的被测对象在一道题上的全部指标(开卷腿)。"""

    target: Target
    provider: str
    citation: NeedSurfaceMetric
    mention: NeedSurfaceMetric
    sentiment_counts: dict[Sentiment, int]
    competitor_citations: list[tuple[str, ProportionEstimate]]  # 有观测的竞品才列
    generative_retrieval: GenerativeRetrieval | None  # 本仪器不暴露检索链路 → None
    brand_cited_urls: list[str]  # 本对象全部运行里被引的品牌官网 URL(未去重)


def _mentioned(obs: RunObservations) -> bool | None:
    return None if obs.mention is None else obs.mention.mentioned


def _present(obs: RunObservations) -> bool | None:
    return None if obs.presence is None else obs.presence.present


def _best_ranks(observations: list[RunObservations]) -> list[int]:
    return [o.presence.best_rank for o in observations
            if o.presence is not None and o.presence.best_rank is not None]


def measure_target(
    target: Target,
    provider: str,
    results: list[SamplingResult],
    observations: list[RunObservations],
    variant_ids: list[str],
    *,
    competitor_names: list[str],
    exposes_retrieval: bool,
) -> TargetMeasurement:
    generative = None
    if exposes_retrieval:
        presence_obs = [o for o in observations if o.presence is not None]
        generative = GenerativeRetrieval(
            presence=rate_metric(results, observations, variant_ids, _present, metric_label=PRESENCE_LABEL)
            if presence_obs else None,
            best_ranks=_best_ranks(observations),
            query=estimate_of([o.query.present for o in observations if o.query is not None]),
            cited=estimate_of([o.citation.cited for o in observations if o.citation is not None]),
            triggered=estimate_of([
                bool(o.run.search_trace.search_triggered) for o in observations
                if o.run.outcome is Outcome.SUCCESS and o.run.search_trace.search_triggered != UNKNOWN
            ]),
        )
    competitor_citations = []
    for name in competitor_names:
        flags = [c.cited for o in observations if (c := o.competitor_citations[name]) is not None]
        if flags:
            competitor_citations.append((name, estimate_proportion(sum(flags), len(flags))))
    return TargetMeasurement(
        target=target,
        provider=provider,
        citation=aggregate_need_surface(results, metric_label=CITATION_LABEL),
        mention=rate_metric(results, observations, variant_ids, _mentioned, metric_label=MENTION_LABEL),
        sentiment_counts=dict(Counter(o.sentiment.sentiment for o in observations if o.sentiment is not None)),
        competitor_citations=competitor_citations,
        generative_retrieval=generative,
        brand_cited_urls=[url for o in observations for url in o.brand_cited_urls],
    )


def measure_bare(
    target: Target, provider: str, results: list[SamplingResult], observations: list[RunObservations],
) -> BareRetrieval:
    """裸检索通道的收敛判据本身就是在场,命中数直接取采样结果。"""
    return BareRetrieval(
        target=target,
        provider=provider,
        metric=aggregate_need_surface(results, metric_label=PRESENCE_LABEL),
        best_ranks=_best_ranks(observations),
    )


def measure_closed_book(results: list[SamplingResult]) -> NeedSurfaceMetric:
    """闭卷腿(search_disabled)的收敛判据就是提及,命中数直接取采样结果。"""
    return aggregate_need_surface(results, metric_label=MENTION_LABEL)
