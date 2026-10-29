"""L1 适配器契约:SurfaceAdapter 接口 + CapabilityRegistry + 三类契约异常。

架构书第 6 章(接口)与第 13.1 节(异常契约)的代码形态,改动需评审。

适配器只做两件事:把 AskRequest 翻译成该平台的调用;把平台返回翻译成
SurfaceRun。不判定品牌提及、不算指标、不猜缺失字段、不自行重试。

依赖说明:本模块只允许引用 engine.models 中的契约类型(第 6 章数据契约),
不得引用 engine 的任何逻辑模块(scheduler / storage / detectors / metrics)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from engine.models import ACCOUNT_ERROR_TYPES, AskRequest, FrozenModel, RunErrorType, SearchMode, SurfaceRun

# ── 异常契约(13.1):适配器对外只允许抛以下三类 ─────────────────────────


class AdapterError(Exception):
    """Transient/Permanent 的公共基类,便于引擎统一捕获。

    response_body:平台响应体,随失败 SurfaceRun 进 raw_payload 存档——失败不蒸发也包括失败的证据(13.1 增补)。
    2xx 却报失败的整份存(与成功回答一样全量);HTTP 错误页只留头尾(防代理吐巨型网页)。

    cost_usd:这次尝试已经花掉的钱(第二轮审核 26)。平台正常回了话、却在响应里报失败时,token 与检索费照样计了;
    适配器按该家价目从响应里折算后带上,调度器记进失败行(或加到重试后成功的那一行),不让花费蒸发。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_body: str | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_body = response_body
        self.cost_usd = cost_usd


class AdapterTransientError(AdapterError):
    """超时、限流、5xx。引擎按 13.2 重试;耗尽后落 outcome=TRANSIENT_FAILURE。

    retry_after_s:平台 Retry-After 头要求的等待秒数(v0.4.26 C5)。适配器只把头翻译进来,
    等不等、等多久、等不起怎么办,全归调度器(有界等待,单次上限见 SamplingPolicy.max_wait_s)。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_body: str | None = None,
        retry_after_s: float | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        super().__init__(message, http_status=http_status, response_body=response_body, cost_usd=cost_usd)
        self.retry_after_s = retry_after_s


class AdapterPermanentError(AdapterError):
    """非法请求等永久性错误。不重试,落 outcome=PERMANENT_FAILURE 并告警。"""


class AdapterAccountError(AdapterPermanentError):
    """账户问题(v0.4.21;2026-10-29 细分五种):换 key(AUTH)、充值或调高上限(CREDIT)、地区不支持(REGION)、
    账户或项目受限(RESTRICTED)、周期额度用完(QUOTA),见 engine.models.RunErrorType。
    仍是永久类(不重试),但它不是对 surface 的观测——是我们这边没能发出观测。引擎另作处理:
    照常落库(error_type 取 error_type 参数),不计入尝试数 / 失败率 / 熔断窗口,并立即停止整个
    任务;任务页按五种代码分别解释(每种一句话、一个下一步,文案在 config/errors.yaml)。"""

    def __init__(
        self,
        message: str,
        *,
        error_type: RunErrorType,
        http_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        if error_type not in ACCOUNT_ERROR_TYPES:
            raise ValueError(f"AdapterAccountError 只接受账户类 error_type,收到 {error_type!r}")
        super().__init__(message, http_status=http_status, response_body=response_body)
        self.error_type = error_type


class CapabilityNotSupportedError(Exception):
    """请求了未声明的能力。这是编程错误而非运行时波动:直接上抛,
    任务失败,不落 SurfaceRun。故意不继承 AdapterError,
    避免被引擎的重试/落库路径捕获吞掉(铁律:显式报错,不静默降级)。"""


# ── 能力声明(6.1)────────────────────────────────────────────────────


class CapabilityRegistry(FrozenModel):
    """每个适配器必须声明的能力集合。引擎按能力调度;
    声明不支持的能力被请求时显式报错,禁止静默降级。"""

    supports_query_trace: bool = False
    supports_citations: bool = False
    supports_region: bool = False
    supports_forced_search: bool = False
    supports_search_disabled: bool = False
    supports_offline: bool = False
    # 该仪器给的引用是平台跳转链,链接主机名是跳转服务不是来源:适配器照旧只翻译(domain = 链接主机名),
    # 引擎采样后跟随跳转还原终点再判(v0.4.26 S4,C2)
    citation_urls_redirect: bool = False

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
    display_name: str  # 确认页等处的产品名(v0.4.14);厂商名只许出现在适配器与配置
    # 报告里的名字写「接口 · 型号」(产品文档 P4,用户定;S8.1):接口名由适配器声明,如 "Anthropic Messages API"
    api_name: str
    models: list[str]
    default_model: str
    # 型号标记(配置资产,S9.0;用户定 2026-10-14):「消费端默认」= 该家聊天产品界面默认用的型号;
    # 「最强」= 该家公认最强的型号。两个都必须在 models 里(加载器校验)。
    # 来源与核对日期写在 models_checked,查不到官方说法的写明「待核对」,不猜(铁律 7)。
    consumer_default_model: str
    strongest_model: str
    models_checked: str
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
        """key 有效性、配额可用性(最小请求,成本近零)。账户问题(key 无效 / 额度不足)直接抛 AdapterAccountError——
        开跑前预检据此停单并说清是换 key 还是充值(S11.1);其余问题(服务不可用、网络)返回 False。"""

    def close(self) -> None:  # noqa: B027 —— 刻意的可选钩子,不设为抽象方法
        """释放底层连接。持有 httpx.Client 的适配器必须覆写;
        默认无资源可释放(测试替身/模拟适配器无需实现)。"""
