"""L1 适配器契约:SurfaceAdapter 接口 + CapabilityRegistry + 三类契约异常。

架构书第 6 章(接口)与第 13.1 节(异常契约)的代码形态,改动需评审。

适配器只做两件事:把 AskRequest 翻译成该平台的调用;把平台返回翻译成
SurfaceRun。不判定品牌提及、不算指标、不猜缺失字段、不自行重试。

依赖说明:本模块只允许引用 engine.models 中的契约类型(第 6 章数据契约),
不得引用 engine 的任何逻辑模块(scheduler / storage / detectors / metrics)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from engine.models import AskRequest, FrozenModel, SearchMode, SurfaceRun

# ── 异常契约(13.1):适配器对外只允许抛以下三类 ─────────────────────────


class AdapterError(Exception):
    """Transient/Permanent 的公共基类,便于引擎统一捕获。

    response_body:平台错误响应体(截断),随失败 SurfaceRun 进 raw_payload
    存档——失败不蒸发也包括失败的证据(13.1 增补)。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_body = response_body


class AdapterTransientError(AdapterError):
    """超时、限流、5xx。引擎按 13.2 重试;耗尽后落 outcome=TRANSIENT_FAILURE。"""


class AdapterPermanentError(AdapterError):
    """认证失败、非法请求、配额永久不足。不重试,落 outcome=PERMANENT_FAILURE 并告警。"""


class CapabilityNotSupportedError(Exception):
    """请求了未声明的能力。这是编程错误而非运行时波动:直接上抛,
    任务失败,不落 SurfaceRun。故意不继承 AdapterError,
    避免被引擎的重试/落库路径捕获吞掉(铁律:显式报错,不静默降级)。"""


# ── 能力声明(6.1)────────────────────────────────────────────────────


class CapabilityRegistry(FrozenModel):
    """每个适配器必须声明的能力集合。引擎按能力调度;
    声明不支持的能力被请求时显式报错,禁止静默降级。"""

    supports_query_trace: bool = False
    supports_retrieval_rank: bool = False
    supports_citations: bool = False
    supports_region: bool = False
    supports_forced_search: bool = False
    supports_search_disabled: bool = False
    supports_offline: bool = False

    def require(self, capability: str) -> None:
        if capability not in type(self).model_fields:
            raise CapabilityNotSupportedError(f"未知能力名: {capability!r}")
        if not getattr(self, capability):
            raise CapabilityNotSupportedError(f"该 surface 未声明能力: {capability}")

    def require_search_mode(self, mode: SearchMode) -> None:
        """适配器在 ask() 入口调用,校验请求的检索模式是否在声明能力内。"""
        if mode is SearchMode.FORCED_SEARCH:
            self.require("supports_forced_search")
        elif mode is SearchMode.SEARCH_DISABLED:
            self.require("supports_search_disabled")


# ── SurfaceMeta 与接口(6.1)──────────────────────────────────────────


class SurfaceMeta(FrozenModel):
    provider: str
    surface: str
    display_name: str  # 用户报告里的产品名(v0.4.14);厂商名只许出现在适配器与配置
    models: list[str]
    default_model: str
    capabilities: CapabilityRegistry
    unit_prices: dict[str, float] = {}  # 计费单价,键为该 surface 的计费口径(如每千输入 token)
    # 单次调用期望成本,确认页预估用(v0.4.6;v0.4.19 起按型号报价):键 = models 里的每个型号
    # (加载器强制全覆盖——型号是口径的一部分,各测各的,漏报一个预估就静默漏计)。
    # 由适配器自行声明,单一真相源;口径二选一且须在适配器内注明——实测均值 或 推导上界。
    # 真实计费仍以 SurfaceRun.cost_usd 为准。
    est_cost_per_call_usd: dict[str, float]
    # search_disabled 模式(检索依赖诊断闭卷腿)的单次期望成本(v0.4.7),同样按型号:
    # 声明 supports_search_disabled 的适配器必须报价(加载器强制),否则为 None。
    est_cost_per_call_search_disabled_usd: dict[str, float] | None = None


class SurfaceAdapter(ABC):
    """统一适配器接口。实现者自查清单:
    - ask() 入口先 require_search_mode,不支持即抛 CapabilityNotSupportedError;
    - 平台原生错误只翻译为 AdapterTransientError / AdapterPermanentError;
    - 机器可读的显式拒答(如内容过滤终止信号)翻译为 outcome=REFUSED 的
      SurfaceRun 返回,不抛错;从回答文本猜测"算不算拒答"属 L2 判定器,不做;
    - raw_payload 全量存档;不可观测字段记 NOT_OBSERVABLE;
    - 禁止 sleep/重试/退避(归引擎调度器)。
    """

    @abstractmethod
    def meta(self) -> SurfaceMeta: ...

    @abstractmethod
    def ask(self, request: AskRequest) -> SurfaceRun: ...

    @abstractmethod
    def healthcheck(self) -> bool:
        """key 有效性、配额可用性。"""

    def close(self) -> None:  # noqa: B027 —— 刻意的可选钩子,不设为抽象方法
        """释放底层连接。持有 httpx.Client 的适配器必须覆写;
        默认无资源可释放(测试替身/模拟适配器无需实现)。"""
