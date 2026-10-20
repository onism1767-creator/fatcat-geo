"""L2 观测:对每条 SurfaceRun 跑一遍判定器,结果打包,供聚合使用(架构书附录 v0.4.24 第 5 节 ②)。

本版全部是规则判定(免费、确定),出报告时现算;结果照旧逐条存档(persist_observations)。
判不了的项记 None——缺席就是缺席,不补默认值。
引用判定带「链接还原」上下文(S4);来源归属判定还带「网页核实」结果(S6.1):跳转仪器的引用按终点判,
第三方网页按抓回的正文判,看不到的照记原因。
提及判定随档位(S9.2):规则 / 模糊两个判定器都逐条存档(供分歧率与校准),提及率、AI 原话、情感句按档位的主判定器算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.query_trace import QueryTraceDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import Detector, ObservationStore, observe_runs
from engine.detectors.sentiment import SentimentDetector
from engine.detectors.source_class import SourceClassifier
from engine.models import (
    BrandMentionObservation,
    BrandSpec,
    CitationObservation,
    QueryTraceObservation,
    RetrievalRankObservation,
    SentimentObservation,
    SourceAttributionObservation,
    SurfaceRun,
)
from engine.page_check import PageIndex
from engine.resolve import ResolutionContext
from engine.tiers import MentionMethod

_MENTION = BrandMentionDetector()
_FUZZY = FuzzyBrandMentionDetector()
_PRESENCE = RetrievalRankDetector()
_QUERY = QueryTraceDetector()
_MENTION_DETECTORS: dict[MentionMethod, Detector] = {
    MentionMethod.RULES: _MENTION,
    MentionMethod.RULES_FUZZY: _FUZZY,
}


def mention_detector(method: MentionMethod) -> Detector:
    """档位的主提及判定器(S9.2):提及率、AI 原话、情感句都按它算;
    裁判(S9.3)落地前请求它就明确报错,不静默退回别的方法(硬边界 5 同款)。"""
    try:
        return _MENTION_DETECTORS[method]
    except KeyError:
        raise ValueError(f"提及判定方法 {method.value} 还没有判定器(S9.3 落地后才有)") from None


@dataclass(frozen=True)
class ObserveContext:
    """一单的判定上下文:竞品、跳转还原、网页核实。"""

    competitors: list[BrandSpec]
    resolution: ResolutionContext | None = None
    pages: PageIndex | None = None
    mention_method: MentionMethod = MentionMethod.RULES  # 档位定的「提到你」怎么认(S9.2)


def rule_detectors(context: ObserveContext) -> list[Detector]:
    """逐条存档的规则判定器(竞品被引 / 被提到随观测现算、不存档:派生表没有「判的是哪个品牌」这一列)。
    两个提及判定器都存(并排,供校准);情感句按档位的主判定器定位。"""
    return [
        _MENTION, _FUZZY, CitationDetector(context.resolution), _PRESENCE,
        SentimentDetector(mention_detector(context.mention_method)), _QUERY,
        SourceClassifier(context.competitors, context.resolution, context.pages),
    ]


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
    brand_cited_urls: list[str]  # 命中品牌官网的引用用于判定的链接(跳转仪器为终点),供独立核验
    attribution: SourceAttributionObservation | None = None  # 来源归属(第三方线的原料,S6.1)
    # 竞品名 → 回答里提到它吗(S7;与竞品被引一样随观测现算、不存档)
    competitor_mentions: dict[str, BrandMentionObservation | None] = field(default_factory=dict)


def observe(run: SurfaceRun, brand: BrandSpec, context: ObserveContext) -> RunObservations:
    citation_detector = CitationDetector(context.resolution)
    citation = citation_detector.detect(run, brand)
    classifier = SourceClassifier(context.competitors, context.resolution, context.pages)
    main = mention_detector(context.mention_method)
    mention = main.detect(run, brand)
    return RunObservations(
        run=run,
        mention=mention if isinstance(mention, BrandMentionObservation) else None,
        citation=citation,
        presence=_PRESENCE.detect(run, brand),
        sentiment=SentimentDetector(main).detect(run, brand),
        query=_QUERY.detect(run, brand),
        competitor_citations={c.name: citation_detector.detect(run, c) for c in context.competitors},
        brand_cited_urls=list(citation.matched_urls) if citation is not None else [],
        attribution=classifier.detect(run, brand),
        competitor_mentions={c.name: _competitor_mention(main, run, c) for c in context.competitors},
    )


def _competitor_mention(main: Detector, run: SurfaceRun, competitor: BrandSpec) -> BrandMentionObservation | None:
    found = main.detect(run, competitor)
    return found if isinstance(found, BrandMentionObservation) else None


def persist_observations(
    storage: ObservationStore, runs: list[SurfaceRun], brand: BrandSpec, context: ObserveContext,
) -> None:
    """规则判定逐条存档(幂等:同 run × 判定器 × 版本至多一条)。"""
    observe_runs(storage, runs, rule_detectors(context), brand)
