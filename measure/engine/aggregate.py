"""L2 聚合:逐条观测 → 各被测对象在一个题型上的指标(v0.4.26 S2;架构书附录 v0.4.24 第 5 节 ③)。

只算数,不下结论:阶段状态、定位、建议都是 L3 的事。指标一律「计数 + 比例」,不算区间(产品文档 15.2)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from engine.detectors.citation import DETECTOR_NAME as CITATION_LABEL
from engine.detectors.matching import mention_sentence, normalize_domain, url_host
from engine.detectors.retrieval_rank import DETECTOR_NAME as PRESENCE_LABEL
from engine.metrics import Rate, SampleAccounting, account_runs, rate_of
from engine.models import (
    UNKNOWN,
    AnswerQuote,
    BrandSpec,
    FrozenModel,
    PromptIntent,
    Sentiment,
    SourceAttribution,
    SourceClass,
    StopReason,
    Target,
)
from engine.observe import RunObservations
from engine.scheduler import is_account_error
from engine.validity import is_valid_answer

MENTION_LABEL = "brand_mention"  # 提及指标的标签;用的是哪个判定器随档位(S9.2),记在观测行与采样说明里
SPELLINGS_LIMIT = 8  # 报告末尾最多列几种写法(S9.2b)


class NameSpelling(FrozenModel):
    """AI 回答里写你的名字的一种写法(S9.2b,用户定):原样文字、出现次数、是否已登记为名字 / 别名。
    没登记的下次测可以加进别名——这是从本单回答里数出来的真数据。"""

    text: str
    count: int
    registered: bool
    suggested: bool = False  # 裁判觉得像你、但没登记,没算进提及率(BR5,用户 2026-10-30 定)


def tally_spellings(
    observations: Iterable[RunObservations], brand: BrandSpec, limit: int = SPELLINGS_LIMIT,
) -> list[NameSpelling]:
    """按主判定器的命中片段数写法(规则判定器没有命中片段,用它命中的登记名);大小写合并、显示第一次见到的写法。
    裁判看到的新写法也数进来,标 suggested;同一个写法也被算作提到过的,按算过的显示。"""
    known = {brand.name.strip().lower(), *(alias.strip().lower() for alias in brand.aliases)}
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    counted: set[str] = set()

    def add(text: str) -> str:
        key = text.lower()
        counts[key] += 1
        display.setdefault(key, text)
        return key

    for o in observations:
        m = o.mention
        if m is None:
            continue
        for written in (s.strip() for s in m.new_spellings):
            if written and written.lower() not in known:
                add(written)
        text = (m.matched_text or m.matched_alias or "").strip()
        if m.mentioned and text:
            counted.add(add(text))
    return [NameSpelling(text=display[key], count=n, registered=key in known, suggested=key not in counted | known)
            for key, n in counts.most_common(limit)]

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
class ArchiveTally:
    """只能从公开存档看到的第三方网页(S6.4):不算实时观测,不进比例,单独给数量与存档日期范围。"""

    pages: int
    mentioning: int
    oldest: str | None  # YYYY-MM-DD
    newest: str | None


CITED_ELSEWHERE_LIMIT: Final = 20  # 报告里「AI 引用了、但没写到你」的第三方网页最多列几个(按引用它的回答数取前几)


class CitedPage(FrozenModel):
    """一个被 AI 引用、正文可读、但没写到你的第三方网页(第三方线 C1 名单的一行)。"""

    url: str  # 判定用的链接(跳转仪器为还原后的终点)
    domain: str
    title: str | None  # AI 返回的引用标题;平台没给 → None,不猜
    answers: int  # 引用了它的回答数(一条回答里引两次只算一次)


class CitedElsewhere(FrozenModel):
    """AI 回答这类问题时愿意拿来当参考、但里面没有你的第三方网页(用户 2026-10-28 定补:实验卡
    EVIDENCE_THIRDPARTY_CITED_PAGE_GAP_V1 写着「证据区有名单」,名单就是它)。只算实时看到、正文可读的
    (SourceClass.OTHER);存档行、看不到的、竞品官网都不算。"""

    pages: list[CitedPage]  # 按回答数降序、同数按网址,最多 CITED_ELSEWHERE_LIMIT 个
    total: int  # 去重后一共几个网页


def cited_elsewhere(observations: Iterable[RunObservations]) -> CitedElsewhere | None:
    """数出「AI 引用了、但没写到你」的第三方网页;一个都没有 → None(实验卡 C1 据此不出)。"""
    answers: Counter[str] = Counter()
    titles: dict[str, str | None] = {}
    for obs in observations:
        if obs.attribution is None or obs.attribution.cited is None or obs.run.answer is None:
            continue
        platform_titles = {c.url: c.title for c in obs.run.answer.citations}
        seen: set[str] = set()
        for entry in obs.attribution.cited:
            if entry.source_class is not SourceClass.OTHER or not _live(entry):
                continue
            if entry.text_source in SUMMARY_SOURCES:  # 只读到标题 + 简介:正文深处可能写了你,看不到就不算(铁律 7;第二轮)
                continue
            key = (entry.final_url or entry.url).split("#", 1)[0]  # 同一篇文章带不同 # 片段算一篇(第二轮)
            if key in seen:
                continue
            seen.add(key)
            answers[key] += 1
            if titles.get(key) is None:
                titles[key] = platform_titles.get(entry.url)
    if not answers:
        return None
    ranked = sorted(answers, key=lambda url: (-answers[url], url))
    return CitedElsewhere(
        pages=[CitedPage(url=url, domain=normalize_domain(url_host(url)), title=titles.get(url),
                         answers=answers[url]) for url in ranked[:CITED_ELSEWHERE_LIMIT]],
        total=len(ranked),
    )


@dataclass(frozen=True)
class ThirdPartyMeasurement:
    """第三方线(S6.1):搜到 / 引用「正文写到你的第三方网页」,与官网线分开、不相加。
    一次回答的状态:至少一条写到你 → 有;没有但至少一条可归类 → 没有;全部看不到 → 不进分母(v0.4.24 第 2 节)。"""

    cited: ClassSurfaceMetric | None  # 引用线;仪器不暴露引用集 → None
    cited_lead: Rate | None  # 引用了写到你的回答里,有几次是标题或首段就出现(技术栏)
    retrieved: ClassSurfaceMetric | None  # 搜到线;仪器不暴露搜索结果 → None
    retrieved_lead: Rate | None
    unobservable_cited: dict[str, int]  # 看不到的引用网页按原因计数(条)
    unobservable_retrieved: dict[str, int]
    summary_only: dict[str, int]  # 只拿到网页摘要 / 嵌入接口标题、按摘要判的第三方网页,按文字来源计数(引用 + 搜到;S6.3)
    archived: ArchiveTally | None  # 只能从存档看到的(引用 + 搜到;S6.4);没有 → None
    cited_elsewhere: CitedElsewhere | None = None  # AI 引用了、但没写到你的第三方网页(C1 名单);没有 → None


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
    third_party: ThirdPartyMeasurement | None  # 第三方线;没有来源归属观测 → None(S6.1)
    mention: ClassSurfaceMetric
    sentiment_counts: dict[Sentiment, int]
    competitor_citations: list[tuple[str, Rate]]  # 有观测的竞品才列
    competitor_mentions: list[tuple[str, Rate]]  # 回答里提到各竞品的次数(S7):有观测的竞品才列
    quotes: list[AnswerQuote]  # AI 原话(S7):每道题按采样顺序前 QUOTES_PER_PROMPT 次回答,只进证据区
    generative_retrieval: GenerativeRetrieval | None  # 本仪器不暴露检索链路 → None
    brand_cited_urls: list[str]  # 本对象全部运行里被引的品牌官网 URL(未去重)


QUOTES_PER_PROMPT: Final = 3  # AI 原话每道题取几次回答(用户定:按提问顺序前 3 条,不挑)


def _mentioned(obs: RunObservations) -> bool | None:
    return None if obs.mention is None else obs.mention.mentioned


def _quotes(observations: list[RunObservations], prompt_ids: list[str]) -> list[AnswerQuote]:
    """每道题按采样顺序取前 QUOTES_PER_PROMPT 次回答,不挑:提到你 → 第一次提到的那一句;
    没提到 → 只记没提到;无效回答和说不准 → None,说不准另带 unsure。"""
    quotes: list[AnswerQuote] = []
    for no, prompt_id in enumerate(prompt_ids, start=1):
        runs = sorted((o for o in observations if o.run.prompt_id == prompt_id), key=lambda o: o.run.run_index)
        for o in runs[:QUOTES_PER_PROMPT]:
            mentioned = None if o.mention is None else o.mention.mentioned
            sentence = None
            mention = o.mention
            if mentioned and mention is not None and mention.first_position is not None and o.run.answer is not None:
                sentence = mention_sentence(o.run.answer.answer_text, mention.first_position).strip()
            quotes.append(AnswerQuote(prompt_id=prompt_id, prompt_no=no, run_index=o.run.run_index,
                                      run_id=o.run.run_id, mentioned=mentioned, sentence=sentence,
                                      unsure=mention is not None and mentioned is None))
    return quotes


def _cited(obs: RunObservations) -> bool | None:
    return None if obs.citation is None else obs.citation.cited


def _present(obs: RunObservations) -> bool | None:
    return None if obs.presence is None else obs.presence.present


def _live(e: SourceAttribution) -> bool:
    """实时看到的才进比例;存档行(S6.4)在实时口径里算看不到。"""
    return e.archived_at is None


def _tp_flag(entries: list[SourceAttribution]) -> bool | None:
    """一次回答的第三方线状态:有 / 没有 / 全部看不到(None,不进分母)。存档行按看不到算。"""
    if any(e.source_class is SourceClass.THIRD_PARTY_MENTIONS and _live(e) for e in entries):
        return True
    if entries and all(e.source_class is SourceClass.NOT_OBSERVABLE or not _live(e) for e in entries):
        return None
    return False


def _tp_lead(entries: list[SourceAttribution]) -> bool:
    return any(e.source_class is SourceClass.THIRD_PARTY_MENTIONS and _live(e) and e.position in ("title", "lead")
               for e in entries)


def _archive_tally(entry_lists: list[list[SourceAttribution]]) -> ArchiveTally | None:
    archived = [e for entries in entry_lists for e in entries if e.archived_at is not None]
    if not archived:
        return None
    stamps = sorted(e.archived_at for e in archived if e.archived_at)
    fmt = lambda ts: f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ts  # noqa: E731
    return ArchiveTally(
        pages=len(archived),
        mentioning=sum(e.source_class is SourceClass.THIRD_PARTY_MENTIONS for e in archived),
        oldest=fmt(stamps[0]) if stamps else None, newest=fmt(stamps[-1]) if stamps else None,
    )


SUMMARY_SOURCES = frozenset({"meta", "oembed", "rendered_meta"})  # 只有标题 + 简介,正文深处提到会漏(不会多算)


def _summary_only(entry_lists: list[list[SourceAttribution]]) -> dict[str, int]:
    counts = Counter(
        e.text_source for entries in entry_lists for e in entries
        if e.source_class in (SourceClass.THIRD_PARTY_MENTIONS, SourceClass.OTHER) and _live(e)
        and e.text_source in SUMMARY_SOURCES
    )
    return {str(k): v for k, v in counts.items()}


def _unobservable(entry_lists: list[list[SourceAttribution]]) -> dict[str, int]:
    counts = Counter(
        e.reason or "UNKNOWN" for entries in entry_lists for e in entries
        if e.source_class is SourceClass.NOT_OBSERVABLE or not _live(e)  # 存档行按实时抓取的原因计看不到
    )
    return dict(counts)


def _measure_third_party(
    target: Target, provider: str, intent: PromptIntent, observations: list[RunObservations],
    prompt_ids: list[str], stop_reason: StopReason,
) -> ThirdPartyMeasurement:
    def metric(side: str, label: str) -> ClassSurfaceMetric | None:
        entry_lists = [getattr(o.attribution, side) for o in observations if o.attribution is not None]
        if all(entries is None for entries in entry_lists):
            return None  # 该仪器不暴露这一项
        return rate_metric(
            target, provider, intent, observations, prompt_ids,
            lambda o: (None if o.attribution is None or getattr(o.attribution, side) is None
                       else _tp_flag(getattr(o.attribution, side))),
            metric_label=label, stop_reason=stop_reason,
        )

    def lead(side: str) -> Rate | None:
        hits = [getattr(o.attribution, side) for o in observations if o.attribution is not None]
        return rate_of([_tp_lead(entries) for entries in hits if entries is not None and _tp_flag(entries)])

    def unobservable(side: str) -> dict[str, int]:
        return _unobservable([
            entries for o in observations if o.attribution is not None
            if (entries := getattr(o.attribution, side)) is not None
        ])

    def entries_of(side: str) -> list[list[SourceAttribution]]:
        return [entries for o in observations if o.attribution is not None
                if (entries := getattr(o.attribution, side)) is not None]

    return ThirdPartyMeasurement(
        cited=metric("cited", "third_party_cited"), cited_lead=lead("cited"),
        retrieved=metric("retrieved", "third_party_retrieved"), retrieved_lead=lead("retrieved"),
        unobservable_cited=unobservable("cited"), unobservable_retrieved=unobservable("retrieved"),
        summary_only=_summary_only(entries_of("cited") + entries_of("retrieved")),
        archived=_archive_tally(entries_of("cited") + entries_of("retrieved")),
        cited_elsewhere=cited_elsewhere(observations),
    )


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
    competitor_mentions: list[tuple[str, Rate]] = []
    for name in competitor_names:
        hits = [m.mentioned for o in observations
                if (m := o.competitor_mentions.get(name)) is not None and m.mentioned is not None]
        if hits:
            competitor_mentions.append((name, Rate(k=sum(hits), n=len(hits))))
    return TargetMeasurement(
        target=target,
        provider=provider,
        intent=intent,
        citation=metric(_cited, CITATION_LABEL),
        primary_citation=metric(primary_cited, PRIMARY_CITATION_LABEL) if primary_domain else None,
        citations_by_domain=by_domain,
        unresolved_citations=sum(c.unresolved for c in cited_obs),
        official_channel_citation=metric(channel_cited, CHANNEL_CITATION_LABEL) if channels_known else None,
        third_party=(
            _measure_third_party(target, provider, intent, observations, prompt_ids, stop_reason)
            if any(o.attribution is not None for o in observations) else None
        ),
        mention=metric(_mentioned, MENTION_LABEL),
        sentiment_counts=dict(Counter(o.sentiment.sentiment for o in observations if o.sentiment is not None)),
        competitor_citations=competitor_citations,
        competitor_mentions=competitor_mentions,
        quotes=_quotes(observations, prompt_ids),
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
