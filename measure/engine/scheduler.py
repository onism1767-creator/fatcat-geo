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
from datetime import UTC, datetime
from typing import Callable, Protocol
from uuid import uuid4

import structlog

from adapters.base import AdapterPermanentError, AdapterTransientError, SurfaceAdapter, SurfaceMeta
from engine.metrics import (
    ProportionEstimate,
    SampleAccounting,
    account_outcomes,
    estimate_proportion,
)
from engine.models import (
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
    timeout_s: float = 60.0


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
    ) -> SamplingResult:
        policy = policy or SamplingPolicy()
        meta = adapter.meta()
        resolved_model = model or meta.default_model

        outcomes: list[Outcome] = []
        hits: list[bool] = []
        recent_failures: deque[bool] = deque(maxlen=policy.failure_window)
        run_ids: list[str] = []
        total_cost = 0.0
        stop_reason: StopReason | None = None

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
            run = self._attempt_with_retries(adapter, request, meta, resolved_model, policy)
            self._storage.insert_surface_run(run)
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
            n_valid = len(hits)
            if n_valid >= policy.min_n:
                estimate = estimate_proportion(sum(hits), n_valid)
                if estimate.width <= policy.ci_width_threshold:
                    stop_reason = StopReason.CI_CONVERGED
                    break
            if n_valid >= policy.max_n:
                stop_reason = StopReason.MAX_N_REACHED
                break

        estimate = estimate_proportion(sum(hits), len(hits)) if hits else None
        result = SamplingResult(
            information_need_id=need.need_id,
            prompt_variant_id=variant.variant_id,
            provider=meta.provider,
            surface=meta.surface,
            model=resolved_model,
            stop_reason=stop_reason,
            n_attempts=len(outcomes),
            accounting=account_outcomes(outcomes),
            k_hits=sum(hits),
            estimate=estimate,
            total_cost_usd=total_cost,
            run_ids=run_ids,
        )
        log.info("plan_stopped", surface=meta.surface, stop_reason=stop_reason.value,
                 n_attempts=result.n_attempts, n_valid=result.accounting.n_valid,
                 k_hits=result.k_hits, cost_usd=total_cost)
        return result

    def _attempt_with_retries(
        self,
        adapter: SurfaceAdapter,
        request: AskRequest,
        meta: SurfaceMeta,
        resolved_model: str,
        policy: SamplingPolicy,
    ) -> SurfaceRun:
        """一次采样 = 最多 max_attempts 次调用(仅 Transient 重试,指数退避+抖动)。
        耗尽/永久失败 → 构造失败 SurfaceRun 返回(失败不蒸发)。"""
        started_at = datetime.now(UTC)
        t0 = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                return adapter.ask(request)
            except AdapterTransientError as exc:
                log.warning("adapter_retry", run_id=request.run_id, surface=meta.surface,
                            attempt=attempts, http_status=exc.http_status, error=str(exc))
                if attempts >= policy.max_attempts:
                    return self._failure_run(
                        request, meta, resolved_model, started_at, t0,
                        outcome=Outcome.TRANSIENT_FAILURE,
                        error=RunError(
                            error_type=RunErrorType.TRANSIENT, message=str(exc),
                            http_status=exc.http_status, attempts=attempts,
                        ),
                    )
                cap = min(policy.backoff_max_s, policy.backoff_base_s * (2 ** (attempts - 1)))
                self._sleep(self._rng.uniform(0, cap))
            except AdapterPermanentError as exc:
                log.error("adapter_permanent_failure", run_id=request.run_id,
                          surface=meta.surface, http_status=exc.http_status, error=str(exc))
                return self._failure_run(
                    request, meta, resolved_model, started_at, t0,
                    outcome=Outcome.PERMANENT_FAILURE,
                    error=RunError(
                        error_type=RunErrorType.PERMANENT, message=str(exc),
                        http_status=exc.http_status, attempts=attempts,
                    ),
                )
            # CapabilityNotSupportedError 故意不捕获:编程错误直接上抛(13.1)

    def _failure_run(
        self,
        request: AskRequest,
        meta: SurfaceMeta,
        resolved_model: str,
        started_at: datetime,
        t0: float,
        *,
        outcome: Outcome,
        error: RunError,
    ) -> SurfaceRun:
        return SurfaceRun(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider=meta.provider,
            surface=meta.surface,
            model=resolved_model,
            search_mode=request.search_mode,
            locale=request.locale,
            region=request.region,
            outcome=outcome,
            error=error,
            # SearchTrace 默认全 UNKNOWN/NOT_OBSERVABLE;失败时无回答、无成本数据
            timestamp=started_at,
            latency_ms=int((time.perf_counter() - t0) * 1000),  # 含重试与退避的真实耗时
            cost_usd=0.0,
        )
