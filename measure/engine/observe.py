"""L2 观测:对每条 SurfaceRun 跑一遍判定器,结果打包,供聚合使用(架构书附录 v0.4.24 第 5 节 ②)。

判定只算一次;结果照旧逐条存档——persist_observations 存的是 observe() 算出的判定行,不重跑(高档的裁判是花钱的)。
判不了的项记 None——缺席就是缺席,不补默认值。
引用判定带「链接还原」上下文(S4);来源归属判定还带「网页核实」结果(S6.1):跳转仪器的引用按终点判,
第三方网页按抓回的正文判,看不到的照记原因。
提及判定随档位(S9.2 / S9.3):规则 / 模糊两个判定器每条都存档(供分歧率与校准),高档再加 AI 裁判;
提及率、AI 原话、情感句按档位的主判定器算;竞品提及走免费判定器(规则档用规则,其余用模糊),不请裁判。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector
from engine.detectors.brand_mention_judge import JudgeBrandMentionDetector, JudgeSentimentDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.query_trace import QueryTraceDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import Detector, ObservationStore
from engine.detectors.sentiment import SentimentDetector
from engine.detectors.source_class import SourceClassifier
from engine.judge import JudgeUnavailable, MentionJudge
from engine.models import (
    BrandMentionObservation,
    BrandSpec,
    CitationObservation,
    DerivedObservation,
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


def mention_detector(method: MentionMethod, judge: MentionJudge | None = None) -> Detector:
    """档位的主提及判定器(S9.2 / S9.3):提及率、AI 原话、情感句都按它算;
    高档要 AI 裁判,没装配就明确报错,不静默退回别的方法(硬边界 5 同款)。"""
    if method is MentionMethod.RULES:
        return _MENTION
    if method is MentionMethod.RULES_FUZZY:
        return _FUZZY
    if judge is None:
        raise JudgeUnavailable("高档的提及判定要 AI 裁判,但没有装配裁判(config/judges.yaml 的型号没加载)")
    return JudgeBrandMentionDetector(judge)


def _free_detector(method: MentionMethod) -> Detector:
    """不花钱的那个:竞品提及用(规则档用规则,其余用模糊),不请裁判。"""
    return _MENTION if method is MentionMethod.RULES else _FUZZY


@dataclass(frozen=True)
class ObserveContext:
    """一单的判定上下文:竞品、跳转还原、网页核实、档位的认法、高档的裁判。"""

    competitors: list[BrandSpec]
    resolution: ResolutionContext | None = None
    pages: PageIndex | None = None
    mention_method: MentionMethod = MentionMethod.RULES  # 档位定的「提到你」怎么认(S9.2)
    judge: MentionJudge | None = None  # 高档的裁判(S9.3),执行器装配并预判


@dataclass(frozen=True)
class RunObservations:
    """一条运行的全部判定结果。"""

    run: SurfaceRun
    mention: BrandMentionObservation | None  # 档位主判定器的提及判定
    citation: CitationObservation | None
    presence: RetrievalRankObservation | None  # 品牌官网在检索候选里(候选集不可观测时 None)
    sentiment: SentimentObservation | None
    query: QueryTraceObservation | None
    competitor_citations: dict[str, CitationObservation | None]  # 竞品名 → 该竞品官网是否被引
    brand_cited_urls: list[str]  # 命中品牌官网的引用用于判定的链接(跳转仪器为终点),供独立核验
    attribution: SourceAttributionObservation | None = None  # 来源归属(第三方线的原料,S6.1)
    # 竞品名 → 回答里提到它吗(S7;与竞品被引一样随观测现算、不存档)
    competitor_mentions: dict[str, BrandMentionObservation | None] = field(default_factory=dict)
    # 要存档的全部判定行(规则 / 模糊 / 裁判提及、引用、在场、情感、检索词、来源归属),S9.3 起由这里统一存
    archived: list[DerivedObservation] = field(default_factory=list)


def _as_mention(observation: DerivedObservation | None) -> BrandMentionObservation | None:
    return observation if isinstance(observation, BrandMentionObservation) else None


def observe(run: SurfaceRun, brand: BrandSpec, context: ObserveContext) -> RunObservations:
    method = context.mention_method
    rules = _MENTION.detect(run, brand)
    fuzzy = _FUZZY.detect(run, brand)
    judged: BrandMentionObservation | None = None
    sentiment: SentimentObservation | None
    if method is MentionMethod.RULES_FUZZY_JUDGE:
        judged = _as_mention(mention_detector(method, context.judge).detect(run, brand))
        assert context.judge is not None  # mention_detector 已把守
        sentiment = JudgeSentimentDetector(context.judge).detect(run, brand)
    else:
        sentiment = SentimentDetector(mention_detector(method)).detect(run, brand)
    mention = {MentionMethod.RULES: rules, MentionMethod.RULES_FUZZY: fuzzy, MentionMethod.RULES_FUZZY_JUDGE: judged}
    citation_detector = CitationDetector(context.resolution)
    citation = citation_detector.detect(run, brand)
    presence = _PRESENCE.detect(run, brand)
    query = _QUERY.detect(run, brand)
    attribution = SourceClassifier(context.competitors, context.resolution, context.pages).detect(run, brand)
    free = _free_detector(method)
    return RunObservations(
        run=run,
        mention=mention[method],
        citation=citation,
        presence=presence,
        sentiment=sentiment,
        query=query,
        competitor_citations={c.name: citation_detector.detect(run, c) for c in context.competitors},
        brand_cited_urls=list(citation.matched_urls) if citation is not None else [],
        attribution=attribution,
        competitor_mentions={c.name: _as_mention(free.detect(run, c)) for c in context.competitors},
        archived=[o for o in (rules, fuzzy, judged, citation, presence, sentiment, query, attribution)
                  if o is not None],
    )


def persist_observations(storage: ObservationStore, observations: Iterable[RunObservations]) -> int:
    """把 observe() 算出的判定行逐条存档(幂等:同 run × 判定器 × 版本至多一条)。判定只算一次——裁判是花钱的。
    返回写入尝试的条数。"""
    written = 0
    for item in observations:
        for observation in item.archived:
            storage.insert_observation(observation)
            written += 1
    return written
