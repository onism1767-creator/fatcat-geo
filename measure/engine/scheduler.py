"""L2 序贯采样调度器:停止规则状态机(7.2)+ 重试与熔断(13.2)。

职责边界:
- 重试/退避/熔断全部在这里,适配器内禁止(13.1);
- 每次采样尝试必落一条 SurfaceRun,失败不蒸发(6.3):适配器抛出的
  Transient(重试耗尽)/Permanent 错误由本模块翻译为失败运行落库;
- CapabilityNotSupportedError 是编程错误:直接上抛,不落库、不重试;
- 收敛判据的"命中"由调用方注入(hit_fn:SurfaceRun → bool | None),
  None 表示该运行不可观测此指标,不进分母——调度器不内嵌任何判定逻辑。

v1 实现口径(偏架构书细化,见文档附录):
- 失败率熔断窗口为近 failure_window 次,但只要累计尝试 ≥ failure_min_attempts
  即开始评估——若必须攒满 20 次才评估,max_n=20 的计划将永远得不到保护;
- 分母长期为空(如全拒答)时由 max_total_attempts 兜底停止,记 MAX_N_REACHED,
  真相由口径中的拒答率/失败率负责呈现。
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

import structlog

from adapters.base import (
    AdapterAccountError,
    AdapterPermanentError,
    AdapterTransientError,
    SurfaceAdapter,
)
from adapters.http_support import classify_account_problem
from engine.metrics import (
    ProportionEstimate,
    SampleAccounting,
    account_outcomes,
    estimate_proportion,
)
from engine.models import (
    ACCOUNT_ERROR_TYPES,
    AskRequest,
    FrozenModel,
    InformationNeed,
    Outcome,
    PromptVariant,
    RunError,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
)

log = structlog.get_logger("engine.scheduler")


class AccountProblem(Exception):
    """我方账户问题(v0.4.21):认证失败 / 额度不足 / 欠费。这不是采样结果,是"发不出观测":
    该次尝试已落库(error_type 为两种账户类之一),然后整个任务应立即停下,等用户换 key 或充值后续跑。"""

    def __init__(
        self, *, provider: str, surface: str, model: str, error_type: RunErrorType, message: str,
    ) -> None:
        super().__init__(f"{provider}/{surface}/{model}: {message}")
        self.provider, self.surface, self.model = provider, surface, model
        self.error_type, self.message = error_type, message


def is_account_error(run: SurfaceRun) -> bool:
    return run.error is not None and run.error.error_type in ACCOUNT_ERROR_TYPES


def reinterpret_account_errors(runs: list[SurfaceRun]) -> list[SurfaceRun]:
    """v0.4.21 之前落库的账户问题行带的是 PERMANENT(当时还没有 ACCOUNT 这一类)。原始层
    不可改,读出续跑前缀时按 L1 现行的同一条翻译规则重新解读——只在内存,库里的行原样不动。"""
    reinterpreted = []
    for run in runs:
        error = run.error
        payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        body = payload.get("error_response_text")
        kind = (
            classify_account_problem(error.http_status, body if isinstance(body, str) else None)
            if error is not None and error.error_type is RunErrorType.PERMANENT else None
        )
        if error is not None and kind is not None:
            run = run.model_copy(update={"error": error.model_copy(update={"error_type": kind})})
        reinterpreted.append(run)
    return reinterpreted


class RunSink(Protocol):
    """调度器对存储的最小依赖面。"""

    def insert_surface_run(self, run: SurfaceRun) -> None: ...


class SamplingPolicy(FrozenModel):
    """停止规则与重试/熔断参数(7.2 / 13.2,阈值可配)。"""

    min_n: int = 5
    max_n: int = 20
    ci_width_threshold: float = 0.20
    max_total_attempts: int = 60  # 分母停滞时的兜底上限
    max_attempts: int = 3  # 单次采样的最大尝试数(含首次),仅 Transient 重试
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    failure_window: int = 20
    failure_min_attempts: int = 4
    failure_rate_threshold: float = 0.5
    budget_usd: float | None = None  # 本计划成本上限;None = 不限
    timeout_s: float = 180.0  # v0.4.21:旗舰型号单发约 30 s,并发下 60 s 会误判超时


class SamplingResult(FrozenModel):
    """一个 (PromptVariant × surface) 采样计划的结果与完整口径。"""

    information_need_id: str
    prompt_variant_id: str
    provider: str
    surface: str
    model: str
    stop_reason: StopReason
    n_attempts: int
    accounting: SampleAccounting
    k_hits: int
    estimate: ProportionEstimate | None  # 分母为空时 None
    total_cost_usd: float
    run_ids: list[str]


class SequentialSampler:
    def __init__(
        self,
        storage: RunSink,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._storage = storage
        self._sleep = sleep
        self._rng = rng or random.Random()

    def run_plan(
        self,
        adapter: SurfaceAdapter,
        need: InformationNeed,
        variant: PromptVariant,
        *,
        hit_fn: Callable[[SurfaceRun], bool | None],
        policy: SamplingPolicy | None = None,
        search_mode: SearchMode = SearchMode.DEFAULT,
        model: str | None = None,
        locale: str = "zh-CN",
        region: str | None = None,
        prior_runs: list[SurfaceRun] | None = None,
        allow_new_calls: bool = True,
    ) -> SamplingResult:
        """prior_runs:幂等续跑(v0.4.9)——本计划已落库的运行按时间序作为前缀,
        指标、成本、熔断窗口与预算合并计算;既有样本已满足停止条件时零新调用。
        幂等键 = (问法 × surface × 型号 × 检索模式),尝试序号由时间戳顺序隐含;
        调用方传入的 prior_runs 必须已按这四项筛过(型号是口径的一部分,v0.4.19)。
        allow_new_calls=False:只按既有样本重算(重新生成报告),一次新调用都不发;
        没满足任何停止条件就记 NOT_RESAMPLED。"""
        policy = policy or SamplingPolicy()
        meta = adapter.meta()
        resolved_model = model or meta.default_model

        # 账户问题行(v0.4.21)不是尝试:只计数,不进 outcomes / 命中 / 熔断窗口
        n_account_error = sum(is_account_error(r) for r in prior_runs or [])
        prior_runs = [r for r in prior_runs or [] if not is_account_error(r)]
        outcomes: list[Outcome] = [r.outcome for r in prior_runs]
        hits: list[bool] = []
        for prior in prior_runs:
            if prior.outcome is Outcome.SUCCESS and (prior_hit := hit_fn(prior)) is not None:
                hits.append(prior_hit)
        # 失败率熔断只看本次运行(v0.4.21):它防的是"现在还在失败就别再花钱",上一次运行的
        # 失败已是过去式——用它开场就熔断,会让续跑一次新调用都发不出
        recent_failures: deque[bool] = deque(maxlen=policy.failure_window)
        run_ids: list[str] = [r.run_id for r in prior_runs]
        total_cost = sum(r.cost_usd for r in prior_runs)
        # 续跑:先按既有样本判停——已收敛的计划一分钱新调用都不发
        stop_reason: StopReason | None = self._statistical_stop(hits, policy)

        while stop_reason is None:
            # ── 采样前检查(会阻止花钱的停止条件)──────────────────────
            if (
                len(recent_failures) >= policy.failure_min_attempts
                and sum(recent_failures) / len(recent_failures) > policy.failure_rate_threshold
            ):
                stop_reason = StopReason.FAILURE_RATE_EXCEEDED
                log.warning("failure_breaker_tripped", surface=meta.surface,
                            window=len(recent_failures), failures=sum(recent_failures))
                break
            if policy.budget_usd is not None and total_cost >= policy.budget_usd:
                stop_reason = StopReason.BUDGET_EXHAUSTED
                log.warning("budget_exhausted", surface=meta.surface, cost_usd=total_cost)
                break
            if len(outcomes) >= policy.max_total_attempts:
                stop_reason = StopReason.MAX_N_REACHED
                log.warning("total_attempts_exhausted", surface=meta.surface,
                            n_attempts=len(outcomes), n_valid=len(hits))
                break
            if not allow_new_calls:  # 只重算:结构上不可能再花钱
                stop_reason = StopReason.NOT_RESAMPLED
                break

            # ── 执行一次采样(含 Transient 重试)────────────────────────
            request = AskRequest(
                run_id=str(uuid4()),
                information_need_id=need.need_id,
                prompt_variant_id=variant.variant_id,
                prompt_text=variant.prompt_text,
                search_mode=search_mode,
                model=model,
                locale=locale,
                region=region,
                timeout_s=policy.timeout_s,
            )
            run = ask_with_retries(adapter, request, policy, sleep=self._sleep, rng=self._rng)
            self._storage.insert_surface_run(run)
            if is_account_error(run):  # 落库后立刻停:不进本计划的任何统计,也不再花一分钱
                assert run.error is not None
                raise AccountProblem(provider=meta.provider, surface=meta.surface, model=resolved_model,
                                     error_type=run.error.error_type, message=run.error.message)
            run_ids.append(run.run_id)
            outcomes.append(run.outcome)
            total_cost += run.cost_usd
            recent_failures.append(
                run.outcome in (Outcome.TRANSIENT_FAILURE, Outcome.PERMANENT_FAILURE)
            )
            log.info("sample_recorded", run_id=run.run_id, surface=meta.surface,
                     outcome=run.outcome.value, latency_ms=run.latency_ms, cost_usd=run.cost_usd)

            if run.outcome is Outcome.SUCCESS:
                hit = hit_fn(run)
                if hit is not None:
                    hits.append(hit)

            # ── 采样后检查(统计停止条件,7.2 顺序)────────────────────
            stop_reason = self._statistical_stop(hits, policy)

        estimate = estimate_proportion(sum(hits), len(hits)) if hits else None
        result = SamplingResult(
            information_need_id=need.need_id,
            prompt_variant_id=variant.variant_id,
            provider=meta.provider,
            surface=meta.surface,
            model=resolved_model,
            stop_reason=stop_reason,
            n_attempts=len(outcomes),
            accounting=account_outcomes(outcomes, n_account_error=n_account_error),
            k_hits=sum(hits),
            estimate=estimate,
            total_cost_usd=total_cost,
            run_ids=run_ids,
        )
        log.info("plan_stopped", surface=meta.surface, stop_reason=stop_reason.value,
                 n_attempts=result.n_attempts, n_valid=result.accounting.n_valid,
                 k_hits=result.k_hits, cost_usd=total_cost)
        return result

    @staticmethod
    def _statistical_stop(hits: list[bool], policy: SamplingPolicy) -> StopReason | None:
        """统计停止条件(7.2):收敛优先于 max_n。既用于逐样本后检查,
        也用于续跑时对既有样本的开场判停。"""
        n_valid = len(hits)
        if n_valid >= policy.min_n:
            interim = estimate_proportion(sum(hits), n_valid)
            if interim.width <= policy.ci_width_threshold:
                return StopReason.CI_CONVERGED
        if n_valid >= policy.max_n:
            return StopReason.MAX_N_REACHED
        return None


def ask_with_retries(
    adapter: SurfaceAdapter,
    request: AskRequest,
    policy: SamplingPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> SurfaceRun:
    """一次调用 = 最多 max_attempts 次尝试(仅 Transient 重试,指数退避+抖动)。耗尽 / 永久失败 →
    构造失败 SurfaceRun 返回(失败不蒸发)。采样与辅助 AI 调用(engine/ai_task.py)共用这一套(13.2)。"""
    rng = rng or random.Random()
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    attempts = 0
    while True:
        attempts += 1
        try:
            return adapter.ask(request)
        except AdapterTransientError as exc:
            log.warning("adapter_retry", run_id=request.run_id, surface=adapter.meta().surface,
                        attempt=attempts, http_status=exc.http_status, error=str(exc))
            if attempts >= policy.max_attempts:
                return _failure_run(
                    adapter, request, started_at, t0,
                    outcome=Outcome.TRANSIENT_FAILURE,
                    error=RunError(
                        error_type=RunErrorType.TRANSIENT, message=str(exc),
                        http_status=exc.http_status, attempts=attempts,
                    ),
                    response_body=exc.response_body,
                )
            cap = min(policy.backoff_max_s, policy.backoff_base_s * (2 ** (attempts - 1)))
            sleep(rng.uniform(0, cap))
        except AdapterAccountError as exc:  # 子类须先于 AdapterPermanentError 捕获
            meta = adapter.meta()
            log.error("adapter_account_problem", run_id=request.run_id, surface=meta.surface,
                      model=request.model or meta.default_model, http_status=exc.http_status, error=str(exc))
            return _failure_run(
                adapter, request, started_at, t0,
                outcome=Outcome.PERMANENT_FAILURE,
                error=RunError(
                    error_type=exc.error_type, message=str(exc),
                    http_status=exc.http_status, attempts=attempts,
                ),
                response_body=exc.response_body,
            )
        except AdapterPermanentError as exc:
            log.error("adapter_permanent_failure", run_id=request.run_id,
                      surface=adapter.meta().surface, http_status=exc.http_status, error=str(exc))
            return _failure_run(
                adapter, request, started_at, t0,
                outcome=Outcome.PERMANENT_FAILURE,
                error=RunError(
                    error_type=RunErrorType.PERMANENT, message=str(exc),
                    http_status=exc.http_status, attempts=attempts,
                ),
                response_body=exc.response_body,
            )
        # CapabilityNotSupportedError 故意不捕获:编程错误直接上抛(13.1)


def _failure_run(
    adapter: SurfaceAdapter,
    request: AskRequest,
    started_at: datetime,
    t0: float,
    *,
    outcome: Outcome,
    error: RunError,
    response_body: str | None = None,
) -> SurfaceRun:
    meta = adapter.meta()  # 只在失败时取:构造失败记录要知道是哪台仪器、哪个型号
    return SurfaceRun(
        run_id=request.run_id,
        information_need_id=request.information_need_id,
        prompt_variant_id=request.prompt_variant_id,
        provider=meta.provider,
        surface=meta.surface,
        model=request.model or meta.default_model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        outcome=outcome,
        error=error,
        # SearchTrace 默认全 UNKNOWN/NOT_OBSERVABLE;失败时无回答、无成本数据
        # 平台错误响应体(截断)存档:失败的证据也不蒸发(13.1)
        raw_payload=(
            {"error_response_text": response_body} if response_body is not None else None
        ),
        timestamp=started_at,
        latency_ms=int((time.perf_counter() - t0) * 1000),  # 含重试与退避的真实耗时
        cost_usd=0.0,
    )
