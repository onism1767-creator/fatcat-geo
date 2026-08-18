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


class PromptOrigin(StrEnum):
    USER = "user"
    ASSISTED = "assisted"


# ── 需求与问法(6.5)────────────────────────────────────────────────────


class InformationNeed(FrozenModel):
    """信息需求:分析与报告的基本单位,指标聚合到这一层。"""

    need_id: str = Field(default_factory=lambda: str(uuid4()))
    statement: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    timeout_s: float = 60.0


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
    def _outcome_error_consistency(self) -> "SurfaceRun":
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
