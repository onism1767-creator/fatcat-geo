"""L2 数据模型:架构书第 6 章「接口与数据契约」的代码形态。

本模块是系统宪法的一部分,任何字段增删都需评审(架构书第 6 章)。

关键不变式:
- SurfaceRun 是不可变原始层(frozen),只追加,永不修改;
- SurfaceRun 及其嵌套模型中不得出现任何提及/情感/判定类字段,
  一切判定属派生层 DerivedObservation,带 detector_version 可全量重算;
- 不可观测的字段记 NOT_OBSERVABLE,禁止猜测填充;
- 本模块(以及整个 engine/、diagnosis/)源码中不得出现任何
  provider/surface 名字符串,tests/unit/test_no_provider_strings.py 机械检查。

AbsorptionProxyObservation 按架构书 6.4 于 v1 后置,暂不定义。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

# ── 观测缺失语义(铁律 7:不可观测就标注,禁止猜测填充)──────────────────

NOT_OBSERVABLE: Final = "NOT_OBSERVABLE"
UNKNOWN: Final = "UNKNOWN"

NotObservable = Literal["NOT_OBSERVABLE"]
Unknown = Literal["UNKNOWN"]


class FrozenModel(BaseModel):
    """所有契约模型的基类:不可变 + 未知字段拒收。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ── 枚举 ──────────────────────────────────────────────────────────────


class SearchMode(StrEnum):
    """采样时的检索模式,受适配器能力声明约束(6.2)。"""

    DEFAULT = "default"
    FORCED_SEARCH = "forced_search"
    SEARCH_DISABLED = "search_disabled"


class Outcome(StrEnum):
    """运行终态(6.3)。每次采样尝试必落一条 SurfaceRun,失败不蒸发。

    引用集为空的正常返回也是 SUCCESS——真实负观测,必须进指标分母(7.3)。
    """

    SUCCESS = "SUCCESS"
    REFUSED = "REFUSED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class RunErrorType(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    REFUSAL = "REFUSAL"
    # v0.4.21:我方账户问题——不是对 surface 的观测,是观测没发出去。两种,处理不同:
    ACCOUNT_AUTH = "ACCOUNT_AUTH"  # key 无效 / 没权限(HTTP 401 / 403):换 key
    ACCOUNT_CREDIT = "ACCOUNT_CREDIT"  # 额度不足 / 欠费(HTTP 402,或 400 / 429 带欠费原话):充值


ACCOUNT_ERROR_TYPES = frozenset({RunErrorType.ACCOUNT_AUTH, RunErrorType.ACCOUNT_CREDIT})


class PromptOrigin(StrEnum):
    USER = "user"
    ASSISTED = "assisted"


class PromptIntent(StrEnum):
    """题型(产品文档 15.2 的五类 prompt;v0.4.26 S2)。指标按题型分开看,不得混算:
    前四类不点名焦点品牌,测自然可见度;品牌事实点名焦点品牌,专测 REPRESENTATION。"""

    CATEGORY_DISCOVERY = "category_discovery"  # 品类发现:「XX 类最好的有哪些」
    PROBLEM_SOLUTION = "problem_solution"  # 问题与方案:「怎么解决 XX」
    COMPARISON_ALTERNATIVE = "comparison_alternative"  # 对比与替代:「X 的替代品」「X 和 Y 怎么选」(可点名竞品)
    RECOMMENDATION = "recommendation"  # 推荐:「你会推荐什么」
    BRAND_FACT = "brand_fact"  # 品牌事实:「[品牌] 是做什么的、怎么样」(点名焦点品牌)


class StopReason(StrEnum):
    """采样计划的停止原因(v0.4.26 S2:次数固定,不按结果提前停)。必须随指标一起进报告——
    72 次全部问完与预算截断在 40 次,是完全不同强度的结论(铁律 5)。"""

    COMPLETED = "COMPLETED"  # 全部题目 × 次数都问完了
    FAILURE_RATE_EXCEEDED = "FAILURE_RATE_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SURFACE_DISABLED = "SURFACE_DISABLED"
    # 重新生成报告时只用已有样本重算、不补采(判据变了而已有样本尚未满足停止条件,如官网名单后改):
    # 「重新生成报告不向 AI 提问、不花钱」由结构保证,不靠已有样本恰好收敛(2026-09-29)
    NOT_RESAMPLED = "NOT_RESAMPLED"


# ── 诊断对象与题目(1.3 / 6.5)─────────────────────────────────────────


class BrandSpec(FrozenModel):
    """诊断对象:品牌名、别名/常见错写、品牌域名(用户输入的建模)。
    是判定器(L2 detectors)的业务输入,不进入 SurfaceRun 原始层。"""

    name: str
    aliases: list[str] = []  # 别名与常见错写;匹配时与 name 合并使用
    domains: list[str] = []  # 计入的官网域名(主站在前),来自官网名单 BrandDomainList(v0.4.26 S5)
    official_channels: list[str] = []  # 官方渠道匹配键「主机名 + 路径前缀」,如 brand.tmall.com/(单列,不算官网)





class Prompt(FrozenModel):
    """一道题(产品文档 15.2):原样问 runs_per_prompt 次,不改措辞——改了就不是严格的重复观察。
    题型是口径的一部分,必填;指标按题型聚合。"""

    prompt_id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    intent: PromptIntent
    origin: PromptOrigin
    locale: str  # 提问语言(测量语境,16.5)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CompetitorSuggestion(FrozenModel):
    """竞品建议条目(17.3 第②步产物),用户勾选后转为竞品 BrandSpec。"""

    name: str
    domain: str


class Target(FrozenModel):
    """一个被测对象 = 一台仪器(surface)上的一个型号(v0.4.19,用户拍板:每家 AI 的全部型号一起测)。
    型号是口径的一部分:同一家 AI 的不同型号回答不同,各测各的、各报各的,样本与指标不合并。"""

    surface: str
    model: str

    @property
    def key(self) -> str:
        """表单值 / 模板字典键(surface 名与型号名都不含 "::")。"""
        return f"{self.surface}::{self.model}"

    @classmethod
    def parse(cls, key: str) -> Target:
        surface, _, model = key.partition("::")
        return cls(surface=surface, model=model)


# ── AskRequest(引擎 → 适配器,6.2)─────────────────────────────────────


class AskRequest(FrozenModel):
    """引擎下发给适配器的统一请求。

    run_id / prompt_id / run_index 为关联键:
    由引擎生成,适配器原样写入 SurfaceRun,与结构化日志(13.4)同源。
    重试由引擎统一持有,适配器内禁止自行重试(13.1)。
    """

    run_id: str
    prompt_id: str
    run_index: int  # 第几次原样提问(1 起);同一题同一对象同一模式下的槽位号
    prompt_text: str
    search_mode: SearchMode = SearchMode.DEFAULT
    model: str | None = None  # None 时取适配器 meta 的默认模型
    locale: str
    region: str | None = None  # None = 不钉地区(surface 不支持或未配置)
    timeout_s: float = 180.0  # v0.4.21:带思考的旗舰型号单发约 30 s,并发下 60 s 会误判超时


# ── SurfaceRun(适配器 → 引擎,原始层,永不可变,6.3)────────────────────


class RetrievedSource(FrozenModel):
    """检索候选中的一个来源,按平台返回原样翻译。

    rank 是平台明确给出的位次(1 起)或有序结果中的位置;
    平台未暴露排名信息时记 NOT_OBSERVABLE。
    「目标品牌排到第几」是派生判定,不在此层。
    """

    url: str
    domain: str
    title: str | None = None
    rank: int | NotObservable = NOT_OBSERVABLE


class SearchTrace(FrozenModel):
    search_triggered: bool | Unknown = UNKNOWN
    generated_queries: list[str] | NotObservable = NOT_OBSERVABLE
    retrieved_sources: list[RetrievedSource] | NotObservable = NOT_OBSERVABLE
    opened_sources: list[str] | NotObservable = NOT_OBSERVABLE


class Citation(FrozenModel):
    url: str
    domain: str
    title: str | None = None
    position: int  # 在引用集中的位次,1 起


class Answer(FrozenModel):
    """生成的回答。citations_available=False 表示该 surface 不暴露引用,
    此时 citations 为空不代表「没引用你」,指标层不得混淆两者。"""

    answer_text: str
    citations_available: bool
    citations: list[Citation] = []


class RunError(FrozenModel):
    """outcome 非 SUCCESS 时的错误信息。attempts = 引擎重试耗尽前的总尝试次数。"""

    error_type: RunErrorType
    message: str
    http_status: int | None = None
    attempts: int = 1


class SurfaceRun(FrozenModel):
    """一次采样尝试的原始记录。只追加,永不 UPDATE/DELETE。

    provider/surface 的取值来自配置与适配器 meta 的运行时数据,
    不属于源码硬编码,不违反禁字符串铁律(13.4 说明)。
    """

    run_id: str
    prompt_id: str
    run_index: int
    provider: str
    surface: str
    model: str
    search_mode: SearchMode
    locale: str
    region: str | None = None
    outcome: Outcome
    error: RunError | None = None
    search_trace: SearchTrace = SearchTrace()
    answer: Answer | None = None
    raw_payload: JsonValue = None  # 平台原始返回全量存档,供判定器升级后重解析
    timestamp: datetime
    latency_ms: int
    cost_usd: float

    @model_validator(mode="after")
    def _outcome_error_consistency(self) -> SurfaceRun:
        if self.outcome is Outcome.SUCCESS and self.error is not None:
            raise ValueError("outcome=SUCCESS 时不得携带 error")
        if self.outcome is not Outcome.SUCCESS and self.error is None:
            raise ValueError(f"outcome={self.outcome} 时必须携带 error(失败不蒸发,且拒答信号须留档)")
        return self


# ── DerivedObservation(派生层,可重算,6.4)───────────────────────────


class DerivedObservation(FrozenModel):
    """派生判定基类。每条带 detector_version;判定器升级后对全部历史重算,
    重算须落 RecomputeEvent 审计(13.5,随存储层交付)。"""

    observation_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    detector_name: str
    detector_version: int
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrandMentionObservation(DerivedObservation):
    mentioned: bool
    matched_alias: str | None = None  # 命中的品牌名/别名/错写
    first_position: int | None = None  # 回答文本中的首次出现字符偏移
    brand_rank: int | None = None  # 回答列举多个品牌时的名次,1 起;不适用为 None
    # S9.2(模糊判定器起):怎么认出来的,供单条回答页与校准;规则判定器(v2)不填
    matched_text: str | None = None  # 原文里的命中片段
    match_kind: str | None = None  # exact / normalized / fuzzy
    edit_distance: int | None = None  # 模糊匹配时差几个字母


class CitationObservation(DerivedObservation):
    cited: bool
    positions: list[int] = []  # 命中引用在引用集中的位次
    matched_domains: list[str] = []  # 命中官网的注册域名(去重),供主站引用率与按域名拆分(v0.4.26 S4)
    matched_urls: list[str] = []  # 命中引用用于判定的链接(跳转仪器为终点链接),供独立核验
    matched_channels: list[str] = []  # 命中的官方渠道匹配键(S5):单列,不算官网被引
    unresolved: int = 0  # 跳转链还原失败、记「看不到」的引用条数(不进分子分母)


class PageFetchStatus(StrEnum):
    """打开一个网页的结果(v0.4.26 S6.1):只有 OK 抽到了正文;其余都是「看不到」,原因照记。"""

    OK = "OK"  # HTML 可达,抽到了正文(text_source 记是正文抽取还是全文回退)
    EMPTY_TEXT = "EMPTY_TEXT"  # 可达 HTML 但一个字也抽不出(脚本渲染的空壳 / 空页)
    NOT_HTML = "NOT_HTML"  # 可达但不是网页(PDF、图片……)
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"  # 站点 robots.txt 不允许抓,没抓(第三方站尊重,D23)
    BLOCKED = "BLOCKED"  # 403 / 429 / 503:疑似机器人防护拦截本工具客户端
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 401,或终点落在登录页
    FETCH_FAILED = "FETCH_FAILED"  # 其余非成功状态(404…)、超时、网络错误


class PageCheck(FrozenModel):
    """一次打开网页的原始记录(6.4 派生层的原始观测,只追加、按网址复用):存的是抓到的文字,
    「正文里有没有写到你」是判定(engine/detectors/page_mention.py),可随版本重算,不用重抓。"""

    check_id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    normalized_url: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: PageFetchStatus
    http_status: int | None = None
    final_url: str | None = None
    title: str | None = None
    text: str | None = None  # 抽出的正文(或回退的全文可见文字),有上限
    text_source: str | None = None  # main = 正文抽取;visible = 抽不出正文、回退到全文可见文字
    text_chars: int | None = None
    truncated: bool = False  # 网页超过体积上限,只读了前面一段
    error: str | None = None
    fetcher_version: int
    # S6.4:这条记录来自公开存档(网页现在打不开时的退路,实现见 crawler/):快照地址、存档时间戳、当时实时抓取的状态。
    # 存档不算实时观测,判定与报告都单独计。
    archive_url: str | None = None
    archive_timestamp: str | None = None  # 存档方的时间戳 YYYYMMDDhhmmss
    live_status: PageFetchStatus | None = None  # 实时抓取时的结果(存档行才有),看不到的原因照旧按它计


class SourceClass(StrEnum):
    """一次回答接触的一个网页归哪一类(架构书附录 v0.4.24 D1;S6.1)。"""

    OWN_SITE = "OWN_SITE"
    OFFICIAL_CHANNEL = "OFFICIAL_CHANNEL"
    COMPETITOR_SITE = "COMPETITOR_SITE"
    THIRD_PARTY_MENTIONS = "THIRD_PARTY_MENTIONS"  # 第三方网页,正文里写到你
    OTHER = "OTHER"  # 第三方网页,正文可读,没写到你
    NOT_OBSERVABLE = "NOT_OBSERVABLE"  # 看不到:reason 记原因,不进分母


class SourceAttribution(FrozenModel):
    url: str
    final_url: str | None = None
    source_class: SourceClass
    competitor: str | None = None  # COMPETITOR_SITE:是哪个竞品
    position: str | None = None  # THIRD_PARTY_MENTIONS:title / lead / body
    reason: str | None = None  # NOT_OBSERVABLE:PageFetchStatus 值或 UNRESOLVED / NOT_ATTEMPTED
    text_source: str | None = None  # 文字来源:main / visible / meta / oembed / api:<平台> / rendered_* / archive_*
    archived_at: str | None = None  # 判的是公开存档时记存档时间戳(S6.4):不进实时比例,单独计


class SourceAttributionObservation(DerivedObservation):
    """一次回答的引用与搜索结果逐条归类。None = 该仪器不暴露这一项(看不到引用集 / 候选集)。"""

    cited: list[SourceAttribution] | None
    retrieved: list[SourceAttribution] | None


class AnswerQuote(FrozenModel):
    """AI 原话(S7,用户定:只进证据区、不挑不改):一道题的一次回答里第一次提到你的那一句。
    mentioned None = 这次没有有效回答(拒答 / 失败 / 空白);False = 有效回答但没提到你(不放原话,用户定 Q1)。"""

    prompt_id: str
    prompt_no: int  # 题目在本题型里的序号(1 起,与报告里的题目清单同序)
    run_index: int  # 第几次回答(采样顺序)
    run_id: str
    mentioned: bool | None
    sentence: str | None = None


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"  # 跟到了终点(≥ 1 跳,终点非 3xx;终点是 404 也是终点,可达性是另一回事)
    NOT_REDIRECTED = "NOT_REDIRECTED"  # 跳转服务没跳:看不到真正去向
    TOO_MANY_HOPS = "TOO_MANY_HOPS"
    FETCH_FAILED = "FETCH_FAILED"  # 网络错误 / 超时


class CitationResolution(FrozenModel):
    """一条跳转链的还原记录(v0.4.26 S4,C2):只追加、按链接复用。这是对「此刻这条链接指向哪」的观测,
    不含任何品牌判定。"""

    resolution_id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    final_url: str | None = None
    hops: int
    status: ResolutionStatus
    http_status: int | None = None
    error: str | None = None
    resolver_version: int
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── 官网名单(6.5;v0.4.26 S5,产品文档 15.2 D12)────────────────────────


class DomainTier(StrEnum):
    """一个域名凭什么进名单:A 类主站自己承认、自动计入;B 类只提示。"""

    A1 = "A1"  # 主站 hreflang 声明(HTML link / HTTP Link 头 / sitemap)
    A2 = "A2"  # 主站 Organization / WebSite 结构化数据的 url / sameAs
    A3 = "A3"  # 主站首页直接链接且网址名相同
    B1 = "B1"  # 回答里出现的同名域名,主站没有任何依据指向它
    B2 = "B2"  # 只有对方单方面声明与主站的关系


class DomainStatus(StrEnum):
    CONFIRMED = "confirmed"  # 计入官网(A 类自动;B 类经用户确认)
    UNCONFIRMED = "unconfirmed"  # 只提示,不计入
    REMOVED = "removed"  # 用户去掉的 A 类:不算官网,留痕


class DomainEvidence(FrozenModel):
    """一条依据(机械事实,人话渲染在 L4):kind 定义义,source 是出处页面,detail 是 hreflang 值 / 字段名 / 次数等。"""

    kind: str
    source: str | None = None
    detail: str | None = None


class BrandDomain(FrozenModel):
    domain: str  # 注册域名
    tier: DomainTier
    status: DomainStatus
    evidence: list[DomainEvidence] = []
    bidirectional: bool | None = None  # 对方是否指回主站;None = 没查到(对方首页没打开)
    seen_count: int = 0  # B 类:在回答与搜索结果里出现的次数


class OfficialChannel(FrozenModel):
    """品牌在第三方平台上的官方页面:按「主机名 + 路径前缀」精确匹配,单列、不并入官网。"""

    match: str  # 如 brand.tmall.com/
    platform: str
    kind: str
    evidence: DomainEvidence


class BrandDomainList(FrozenModel):
    """官网名单:随任务快照冻结,再测默认沿用;用户可去掉 A 类、确认 B 类。"""

    primary: str  # 主站注册域名(用户填的)
    discovered_at: datetime
    public_suffix_list: str  # 注册域名用的公共后缀表快照版本
    domains: list[BrandDomain] = []  # 不含主站本身
    official_channels: list[OfficialChannel] = []
    notes: list[DomainEvidence] = []  # 查找过程的说明(主站打不开、超过上限……)
    candidate_fetches: int = 0  # 已打开过的候选首页数(上限贯穿采样前后)

    @property
    def confirmed_domains(self) -> list[str]:
        return [self.primary, *(d.domain for d in self.domains if d.status is DomainStatus.CONFIRMED)]

    @property
    def unconfirmed_domains(self) -> list[str]:
        return [d.domain for d in self.domains if d.status is DomainStatus.UNCONFIRMED]

    @property
    def channel_prefixes(self) -> list[str]:
        return [c.match for c in self.official_channels]

    @property
    def user_changed(self) -> bool:
        return any(e.kind in ("user_confirmed", "user_removed") for d in self.domains for e in d.evidence)


class RetrievalRankObservation(DerivedObservation):
    """品牌域名在检索候选集中的位次判定(检索观测通道供数)。"""

    present: bool
    best_rank: int | None = None  # 命中来源中最靠前的位次(1 起);未命中或位次不可观测为 None
    matched_domains: list[str] = []  # 命中的候选域名原样记录


class QueryTraceObservation(DerivedObservation):
    """品牌名/别名是否进入模型自拟的检索词(检索词可观测的通道供数,v0.4.8)。

    「你在不在模型的候选名单里」——生成端检索通道的上游环节判定。"""

    present: bool
    matched_alias: str | None = None  # 首个命中的品牌名/别名
    matched_queries: list[str] = []  # 命中的检索词原样记录


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class SentimentObservation(DerivedObservation):
    sentiment: Sentiment


class ClaimVerdict(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNVERIFIABLE = "unverifiable"


class ClaimCheck(FrozenModel):
    claim_text: str
    verdict: ClaimVerdict


class RepresentationObservation(DerivedObservation):
    claims: list[ClaimCheck] = []
