"""L2 聚合:逐条观测 → 各被测对象在一个题型上的指标(v0.4.26 S2;架构书附录 v0.4.24 第 5 节 ③)。

只算数,不下结论:阶段状态、定位、建议都是 L3 的事。指标一律「计数 + 比例」,不算区间(产品文档 15.2)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from engine.detectors.brand_mention import DETECTOR_NAME as MENTION_LABEL
from engine.detectors.citation import DETECTOR_NAME as CITATION_LABEL
from engine.detectors.retrieval_rank import DETECTOR_NAME as PRESENCE_LABEL
from engine.metrics import Rate, SampleAccounting, account_runs, rate_of
from engine.models import UNKNOWN, FrozenModel, PromptIntent, Sentiment, StopReason, Target
from engine.observe import RunObservations
from engine.scheduler import is_account_error
from engine.validity import is_valid_answer

PRIMARY_CITATION_LABEL = f"{CITATION_LABEL}:primary"  # 主站引用率的命中口径(v0.4.26 S4)
CHANNEL_CITATION_LABEL = f"{CITATION_LABEL}:official_channel"  # 官方渠道引用的命中口径(S5,单列)


class ClassSurfaceMetric(FrozenModel):
    """题型层指标:同一题型下全部题目 × 每题几次在一个被测对象上的合并观测。
    合并是各题命中的混合比例,正式名称是「冻结题目集上的观测率」,不外推到「该品牌在该类需求总体中
    的可见度」;题间差异靠 per_prompt 并列展示;题目覆盖率防止少数几道题撑起总数(产品文档 15.2)。"""

    intent: PromptIntent
    provider: str
    surface: str
    model: str
    metric_label: str  # 命中口径(通常为判定器名),如 citation_domain_match
    k_hits: int
    rate: Rate | None  # 分母为空 → None
    accounting: SampleAccounting
    stop_reasons: list[StopReason]  # 该对象采样计划的停止原因(一个计划跨全部题型),随口径进报告
    per_prompt: list[Rate | None]  # 逐题,与题同序;某题无有效样本 → None
    prompt_coverage: Rate | None  # 至少命中一次的题数 ÷ 题目总数(产品文档 15.2;没拿到有效回答的题按没命中算)


def _accounting(observations: list[RunObservations]) -> SampleAccounting:
    real = [o.run for o in observations if not is_account_error(o.run)]
    return account_runs(real, n_account_error=len(observations) - len(real))


def rate_metric(
    target: Target,
    provider: str,
    intent: PromptIntent,
    observations: list[RunObservations],
    prompt_ids: list[str],
    hit: Callable[[RunObservations], bool | None],
    *,
    metric_label: str,
    stop_reason: StopReason,
) -> ClassSurfaceMetric:
    """按一个判定数命中:逐题分组(与题同序),再合并。"""
    flags_by_prompt: dict[str, list[bool]] = {pid: [] for pid in prompt_ids}
    for obs in observations:
        value = hit(obs)
        if value is not None and obs.run.prompt_id in flags_by_prompt:
            flags_by_prompt[obs.run.prompt_id].append(value)
    per_prompt = [rate_of(flags) for flags in flags_by_prompt.values()]
    observed = [r for r in per_prompt if r is not None]
    all_flags = [flag for flags in flags_by_prompt.values() for flag in flags]
    return ClassSurfaceMetric(
        intent=intent,
        provider=provider,
        surface=target.surface,
        model=target.model,
        metric_label=metric_label,
        k_hits=sum(all_flags),
        rate=rate_of(all_flags),
        accounting=_accounting(observations),
        stop_reasons=[stop_reason],
        per_prompt=per_prompt,
        # 题目覆盖率(产品文档 15.2):至少命中一次的题数 ÷ 题目总数,没拿到有效回答的题按没命中算;
        # 一道题都没观测到时为 None(整块不可观测,不是 0)
        prompt_coverage=(
            Rate(k=sum(r is not None and r.k > 0 for r in per_prompt), n=len(prompt_ids)) if observed else None
        ),
    )


@dataclass(frozen=True)
class GenerativeRetrieval:
    """生成端检索链路的原料:本仪器自己暴露检索词与候选集时才有。"""

    presence: ClassSurfaceMetric | None  # 品牌官网进候选集的比例(候选集全不可观测时 None)
    best_ranks: list[int]
    query: Rate | None  # 品牌名进自拟检索词的比例
    cited: Rate | None  # 同仪器的被引比例(同仪器漏斗)
    triggered: Rate | None  # 触发检索的比例


@dataclass(frozen=True)
class TargetMeasurement:
    """一个被测对象在一个题型上的全部指标(开卷腿)。"""

    target: Target
    provider: str
    intent: PromptIntent
    citation: ClassSurfaceMetric  # 官网引用率:任一官网域名,一条回答只算一次
    primary_citation: ClassSurfaceMetric | None  # 主站引用率:主站注册域名;没有主站 → None(v0.4.26 S4)
    citations_by_domain: list[tuple[str, Rate]]  # 按域名拆分:各官网注册域名各算一次,按次数降序
    unresolved_citations: int  # 跳转链还原失败、记「看不到」的引用条数(不进任何分子分母)
    official_channel_citation: ClassSurfaceMetric | None  # 官方渠道引用:单列、不并入官网;名单里没有官方渠道 → None(S5)
    mention: ClassSurfaceMetric
    sentiment_counts: dict[Sentiment, int]
    competitor_citations: list[tuple[str, Rate]]  # 有观测的竞品才列
    generative_retrieval: GenerativeRetrieval | None  # 本仪器不暴露检索链路 → None
    brand_cited_urls: list[str]  # 本对象全部运行里被引的品牌官网 URL(未去重)


def _mentioned(obs: RunObservations) -> bool | None:
    return None if obs.mention is None else obs.mention.mentioned


def _cited(obs: RunObservations) -> bool | None:
    return None if obs.citation is None else obs.citation.cited


def _present(obs: RunObservations) -> bool | None:
    return None if obs.presence is None else obs.presence.present


def _best_ranks(observations: list[RunObservations]) -> list[int]:
    return [o.presence.best_rank for o in observations
            if o.presence is not None and o.presence.best_rank is not None]


def measure_target(
    target: Target,
    provider: str,
    intent: PromptIntent,
    observations: list[RunObservations],
    prompt_ids: list[str],
    *,
    stop_reason: StopReason,
    competitor_names: list[str],
    exposes_retrieval: bool,
    primary_domain: str | None = None,
    channels_known: bool = False,
) -> TargetMeasurement:
    """primary_domain:主站的注册域名(没有就不出主站引用率);channels_known:官网名单里有官方渠道才出官方渠道引用。"""
    def metric(hit: Callable[[RunObservations], bool | None], label: str) -> ClassSurfaceMetric:
        return rate_metric(target, provider, intent, observations, prompt_ids, hit,
                           metric_label=label, stop_reason=stop_reason)

    def primary_cited(obs: RunObservations) -> bool | None:
        return None if obs.citation is None else primary_domain in obs.citation.matched_domains

    def channel_cited(obs: RunObservations) -> bool | None:
        return None if obs.citation is None else bool(obs.citation.matched_channels)

    cited_obs = [o.citation for o in observations if o.citation is not None]
    by_domain: list[tuple[str, Rate]] = []
    for domain in sorted({d for c in cited_obs for d in c.matched_domains}):
        r = rate_of([domain in c.matched_domains for c in cited_obs])
        if r is not None:
            by_domain.append((domain, r))
    by_domain.sort(key=lambda item: (-item[1].k, item[0]))

    generative = None
    if exposes_retrieval:
        presence_obs = [o for o in observations if o.presence is not None]
        generative = GenerativeRetrieval(
            presence=metric(_present, PRESENCE_LABEL) if presence_obs else None,
            best_ranks=_best_ranks(observations),
            query=rate_of([o.query.present for o in observations if o.query is not None]),
            cited=rate_of([o.citation.cited for o in observations if o.citation is not None]),
            triggered=rate_of([
                bool(o.run.search_trace.search_triggered) for o in observations
                if is_valid_answer(o.run) and o.run.search_trace.search_triggered != UNKNOWN
            ]),
        )
    competitor_citations: list[tuple[str, Rate]] = []
    for name in competitor_names:
        flags = [c.cited for o in observations if (c := o.competitor_citations[name]) is not None]
        if flags:
            competitor_citations.append((name, Rate(k=sum(flags), n=len(flags))))
    return TargetMeasurement(
        target=target,
        provider=provider,
        intent=intent,
        citation=metric(_cited, CITATION_LABEL),
        primary_citation=metric(primary_cited, PRIMARY_CITATION_LABEL) if primary_domain else None,
        citations_by_domain=by_domain,
        unresolved_citations=sum(c.unresolved for c in cited_obs),
        official_channel_citation=metric(channel_cited, CHANNEL_CITATION_LABEL) if channels_known else None,
        mention=metric(_mentioned, MENTION_LABEL),
        sentiment_counts=dict(Counter(o.sentiment.sentiment for o in observations if o.sentiment is not None)),
        competitor_citations=competitor_citations,
        generative_retrieval=generative,
        brand_cited_urls=[url for o in observations for url in o.brand_cited_urls],
    )


def measure_closed_book(
    target: Target,
    provider: str,
    intent: PromptIntent,
    observations: list[RunObservations],
    prompt_ids: list[str],
    *,
    stop_reason: StopReason,
) -> ClassSurfaceMetric:
    """闭卷腿(search_disabled)只看提及。"""
    return rate_metric(target, provider, intent, observations, prompt_ids, _mentioned,
                       metric_label=MENTION_LABEL, stop_reason=stop_reason)
