"""L2 观测:对每条 SurfaceRun 跑一遍判定器,结果打包,供聚合使用(架构书附录 v0.4.24 第 5 节 ②)。

本版全部是规则判定(免费、确定),出报告时现算;结果照旧逐条存档(persist_observations)。
判不了的项记 None——缺席就是缺席,不补默认值。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from adapters.base import CapabilityRegistry
from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.matching import matches_any_brand_domain
from engine.detectors.query_trace import QueryTraceDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import Detector, ObservationStore, observe_runs
from engine.detectors.sentiment import SentimentDetector
from engine.models import (
    BrandMentionObservation,
    BrandSpec,
    CitationObservation,
    QueryTraceObservation,
    RetrievalRankObservation,
    SearchMode,
    SentimentObservation,
    SurfaceRun,
)

_MENTION = BrandMentionDetector()
_CITATION = CitationDetector()
_PRESENCE = RetrievalRankDetector()
_SENTIMENT = SentimentDetector()
_QUERY = QueryTraceDetector()
# 逐条存档的规则判定器(竞品被引随观测现算、不存档:派生表没有「判的是哪个品牌」这一列)
RULE_DETECTORS: tuple[Detector, ...] = (_MENTION, _CITATION, _PRESENCE, _SENTIMENT, _QUERY)


@dataclass(frozen=True)
class RunObservations:
    """一条运行的全部规则判定结果。"""

    run: SurfaceRun
    mention: BrandMentionObservation | None
    citation: CitationObservation | None
    presence: RetrievalRankObservation | None  # 品牌官网在检索候选里(候选集不可观测时 None)
    sentiment: SentimentObservation | None
    query: QueryTraceObservation | None
    competitor_citations: dict[str, CitationObservation | None]  # 竞品名 → 该竞品官网是否被引
    brand_cited_urls: list[str]  # 被引来源里属于品牌官网的 URL(供独立核验)


def _brand_cited_urls(run: SurfaceRun, brand: BrandSpec) -> list[str]:
    if run.answer is None or not run.answer.citations_available:
        return []
    return [c.url for c in run.answer.citations if matches_any_brand_domain(c.domain, brand)]


def observe(run: SurfaceRun, brand: BrandSpec, competitors: list[BrandSpec]) -> RunObservations:
    return RunObservations(
        run=run,
        mention=_MENTION.detect(run, brand),
        citation=_CITATION.detect(run, brand),
        presence=_PRESENCE.detect(run, brand),
        sentiment=_SENTIMENT.detect(run, brand),
        query=_QUERY.detect(run, brand),
        competitor_citations={c.name: _CITATION.detect(run, c) for c in competitors},
        brand_cited_urls=_brand_cited_urls(run, brand),
    )


def persist_observations(storage: ObservationStore, runs: list[SurfaceRun], brand: BrandSpec) -> None:
    """规则判定逐条存档(幂等:同 run × 判定器 × 版本至多一条)。"""
    observe_runs(storage, runs, list(RULE_DETECTORS), brand)


def convergence_hit(
    capabilities: CapabilityRegistry, mode: SearchMode, brand: BrandSpec
) -> Callable[[SurfaceRun], bool | None]:
    """采样的收敛判据(按能力声明泛型分派,不认 surface 名):闭卷腿看提及(该模式没有引用机制);
    开卷腿看被引。"""
    if mode is SearchMode.SEARCH_DISABLED:
        return lambda run: None if (o := _MENTION.detect(run, brand)) is None else o.mentioned
    if capabilities.supports_citations:
        return lambda run: None if (o := _CITATION.detect(run, brand)) is None else o.cited
    raise ValueError("该仪器未声明可作收敛判据的能力(引用)")
