"""L2 观测:对每条 SurfaceRun 跑一遍判定器,结果打包,供聚合使用(架构书附录 v0.4.24 第 5 节 ②)。

判定只算一次;结果照旧逐条存档——persist_observations 存的是 observe() 算出的判定行,不重跑(高档的裁判是花钱的)。
判不了的项记 None——缺席就是缺席,不补默认值。
引用判定带「链接还原」上下文(S4);来源归属判定还带「网页核实」结果(S6.1):跳转仪器的引用按终点判,
第三方网页按抓回的正文判,看不到的照记原因。
提及判定随档位(S9.2 / S9.3):规则 / 模糊两个判定器每条都存档(供分歧率与校准),高档再加 AI 裁判;
中档只把模糊判定器说不准的那几条请裁判,裁判判不了仍记说不准(BR4,用户定 Q3);低档说不准就是说不准。
提及率、AI 原话、情感句按档位的主判定器算;竞品提及走免费判定器(规则档用规则,其余用模糊),不请裁判。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector
from engine.detectors.brand_mention_judge import DETECTOR_NAME as JUDGE_MENTION_NAME
from engine.detectors.brand_mention_judge import SENTIMENT_DETECTOR_NAME as JUDGE_SENTIMENT_NAME
from engine.detectors.brand_mention_judge import JudgeBrandMentionDetector, JudgeSentimentDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.query_trace import QueryTraceDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import ObservationStore
from engine.detectors.sentiment import DETECTOR_NAME as LEXICON_SENTIMENT_NAME
from engine.detectors.sentiment import SentimentDetector
from engine.detectors.source_class import SourceClassifier
from engine.judge import JudgeUnavailable, MentionJudge
from engine.models import (
    BrandMentionObservation,
    BrandSpec,
    CitationObservation,
    DerivedObservation,
    MentionContext,
    QueryTraceObservation,
    RetrievalRankObservation,
    SentimentObservation,
    SourceAttributionObservation,
    SurfaceRun,
)
from engine.page_check import PageIndex
from engine.resolve import ResolutionContext
from engine.tiers import MentionMethod

_PRESENCE = RetrievalRankDetector()
_QUERY = QueryTraceDetector()


def mention_detector_name(method: MentionMethod) -> str:
    """档位主提及判定器的名字——只读库里已存的判定行时用(单条回答页、结果文件),不需要装配裁判。
    原来这两处直接调 mention_detector(),高档没装裁判就抛错,页面 500(第二轮红 3)。"""
    if method is MentionMethod.RULES:
        return BrandMentionDetector.name
    return FuzzyBrandMentionDetector.name if method is MentionMethod.RULES_FUZZY else JUDGE_MENTION_NAME


def sentiment_detector_name(method: MentionMethod) -> str:
    """档位用的情感判定器名字:高档存的是裁判给的情感,其余档是词表情感(读库里的判定行时用)。"""
    return JUDGE_SENTIMENT_NAME if method is MentionMethod.RULES_FUZZY_JUDGE else LEXICON_SENTIMENT_NAME


def _names_of(brand: BrandSpec) -> list[str]:
    return [name for name in dict.fromkeys([brand.name, *brand.aliases]) if name.strip()]


def mention_context(category: str, others: Iterable[BrandSpec]) -> MentionContext:
    """判定一家时的线索:本单品类 + 同一张名单里的别家(判你时别家是竞品,判竞品时别家含你)。"""
    return MentionContext(category=category, peers=[name for other in others for name in _names_of(other)])


@dataclass(frozen=True)
class ObserveContext:
    """一单的判定上下文:竞品、跳转还原、网页核实、档位的认法、裁判(高档每条判,中档只判说不准的)。"""

    competitors: list[BrandSpec]
    resolution: ResolutionContext | None = None
    pages: PageIndex | None = None
    mention_method: MentionMethod = MentionMethod.RULES  # 档位定的「提到你」怎么认(S9.2)
    judge: MentionJudge | None = None  # 执行器装配并预判
    category: str = ""  # 本单品类,有风险的名字靠它和竞品名当线索(BR4)


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


class _Settled:
    """已经判完的提及当情感判定器的来源:情感句按主判定器定位,不重判。"""

    name = "settled_mention"
    version = 1

    def __init__(self, mention: BrandMentionObservation | None) -> None:
        self._mention = mention

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        return self._mention


def _unsure(observation: BrandMentionObservation | None) -> bool:
    return observation is not None and observation.mentioned is None


def is_unsure_for_fuzzy(run: SurfaceRun, brand: BrandSpec, context: MentionContext) -> bool:
    """中档要不要为这条回答请裁判。"""
    return _unsure(_as_mention(FuzzyBrandMentionDetector(context).detect(run, brand)))


def observe(run: SurfaceRun, brand: BrandSpec, context: ObserveContext) -> RunObservations:
    method = context.mention_method
    clues = mention_context(context.category, context.competitors)
    rules = BrandMentionDetector(clues).detect(run, brand)
    fuzzy = FuzzyBrandMentionDetector(clues).detect(run, brand)
    judged: BrandMentionObservation | None = None
    sentiment: SentimentObservation | None
    if method is MentionMethod.RULES_FUZZY_JUDGE:
        if context.judge is None:  # 不静默退回别的方法(硬边界 5 同款)
            raise JudgeUnavailable("高档的提及判定要 AI 裁判,但没有装配裁判(config/judges.yaml 的型号没加载)")
        judged = _as_mention(JudgeBrandMentionDetector(context.judge).detect(run, brand))
        mention = judged
        sentiment = JudgeSentimentDetector(context.judge).detect(run, brand)
    else:
        mention = rules if method is MentionMethod.RULES else fuzzy
        if method is MentionMethod.RULES_FUZZY and context.judge is not None and _unsure(fuzzy):
            judged = _as_mention(JudgeBrandMentionDetector(context.judge).detect(run, brand))
            mention = judged or fuzzy  # 裁判判不了仍记说不准
        sentiment = SentimentDetector(_Settled(mention)).detect(run, brand)
    citation_detector = CitationDetector(context.resolution)
    citation = citation_detector.detect(run, brand)
    presence = _PRESENCE.detect(run, brand)
    query = _QUERY.detect(run, brand)
    attribution = SourceClassifier(context.competitors, context.resolution, context.pages).detect(run, brand)
    def competitor_mention(competitor: BrandSpec) -> BrandMentionObservation | None:
        """不花钱的那个判定器:规则档用规则,其余用模糊,不请裁判;说不准的不进这家的分母。"""
        others = [brand, *(c for c in context.competitors if c.name != competitor.name)]
        detector_class = BrandMentionDetector if method is MentionMethod.RULES else FuzzyBrandMentionDetector
        return _as_mention(detector_class(mention_context(context.category, others)).detect(run, competitor))

    return RunObservations(
        run=run,
        mention=mention,
        citation=citation,
        presence=presence,
        sentiment=sentiment,
        query=query,
        competitor_citations={c.name: citation_detector.detect(run, c) for c in context.competitors},
        brand_cited_urls=list(citation.matched_urls) if citation is not None else [],
        attribution=attribution,
        competitor_mentions={c.name: competitor_mention(c) for c in context.competitors},
        archived=[o for o in (rules, fuzzy, judged, citation, presence, sentiment, query, attribution)
                  if o is not None],
    )


def persist_observations(
    storage: ObservationStore, observations: Iterable[RunObservations], *,
    trigger_kind: str = "job", note: str | None = None,
) -> int:
    """把 observe() 算出的判定行逐条存档(同样的判定不重复写;判据改了的追加,见 Storage.insert_observation)。
    判定只算一次——裁判是花钱的。某个判定器的版本第一次出现在库里(新库,或判定器升级后的第一单)记一条
    RecomputeEvent 审计(13.5;审核 2026-10-27 黄 8):指标变了,得分得清是世界变了还是判定器变了。
    记没记过以审计表本身为准(Storage.record_version_first_seen):两单同时存档只记一条,上次存档中途断库漏记的这次补上
    (第二轮审核 B6)。返回实际写入的条数。"""
    written: Counter[tuple[str, int]] = Counter()
    for item in observations:
        for observation in item.archived:
            key = (observation.detector_name, observation.detector_version)
            written[key] += 0  # 这批里出现过的版本都要核一遍审计,哪怕这次一行都没新写(补记漏掉的)
            if storage.insert_observation(observation):
                written[key] += 1
    for (name, version), count in written.items():
        storage.record_version_first_seen(detector_name=name, version=version, rows_affected=count,
                                          trigger_kind=trigger_kind, note=note)
    return sum(written.values())
