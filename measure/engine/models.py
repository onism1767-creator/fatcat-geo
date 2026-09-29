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


class NeedIntent(StrEnum):
    """需求意图分型(17.2)。指标解释按意图分层,不得混算:
    中立三型测自然可见度;brand_probe 点名焦点品牌,专测 REPRESENTATION。"""

    DISCOVERY = "discovery"
    COMPARISON = "comparison"
    SCENARIO = "scenario"
    BRAND_PROBE = "brand_probe"


class StopReason(StrEnum):
    """采样停止原因(7.2)。必须随指标一起进报告——N=6 因收敛而停与
    N=6 因预算截断,是完全不同强度的结论(铁律 5)。"""

    CI_CONVERGED = "CI_CONVERGED"
    MAX_N_REACHED = "MAX_N_REACHED"
    FAILURE_RATE_EXCEEDED = "FAILURE_RATE_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SURFACE_DISABLED = "SURFACE_DISABLED"


# ── 诊断对象与需求(1.3 / 6.5)─────────────────────────────────────────


class BrandSpec(FrozenModel):
    """诊断对象:品牌名、别名/常见错写、品牌域名(用户输入的建模)。
    是判定器(L2 detectors)的业务输入,不进入 SurfaceRun 原始层。"""

    name: str
    aliases: list[str] = []  # 别名与常见错写;匹配时与 name 合并使用
    domains: list[str] = []  # 品牌域名,如 example.com,不带协议





class InformationNeed(FrozenModel):
    """信息需求:分析与报告的基本单位,指标聚合到这一层。
    intent 为必填:意图是口径的一部分(17.2),不允许无标签的需求。"""

    need_id: str = Field(default_factory=lambda: str(uuid4()))
    statement: str
    intent: NeedIntent
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


class PromptVariant(FrozenModel):
    """同一需求的一种问法。用户原话 ≠ 实际执行的查询,故以需求为聚合单位。"""

    variant_id: str = Field(default_factory=lambda: str(uuid4()))
    need_id: str
    prompt_text: str
    origin: PromptOrigin
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── AskRequest(引擎 → 适配器,6.2)─────────────────────────────────────


class AskRequest(FrozenModel):
    """引擎下发给适配器的统一请求。

    run_id / information_need_id / prompt_variant_id 为关联键:
    由引擎生成,适配器原样写入 SurfaceRun,与结构化日志(13.4)同源。
    重试由引擎统一持有,适配器内禁止自行重试(13.1)。
    """

    run_id: str
    information_need_id: str
    prompt_variant_id: str
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
    information_need_id: str
    prompt_variant_id: str
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


class CitationObservation(DerivedObservation):
    cited: bool
    positions: list[int] = []  # 命中引用在引用集中的位次


class CitationEvidence(FrozenModel):
    """一条被引 URL 的独立核验记录(三档证据「已核验」层的原始观测)。

    这是对「核验时刻的网页」的观测,只追加、可按时效复用;
    「某次运行算不算已核验被引」是判断,由诊断层基于本表 + SurfaceRun 计算。
    checked_brands 记录本次查找过哪些品牌——没查过的品牌缺席不可解读。
    """

    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    normalized_url: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reachable: bool
    http_status: int | None = None
    final_url: str | None = None
    checked_brands: list[str] = []
    brands_found: list[str] = []  # 可见正文中确实出现清晰名的品牌
    visible_chars: int | None = None
    error: str | None = None


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
