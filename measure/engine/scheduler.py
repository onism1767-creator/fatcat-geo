"""L2 采样调度器:每道题原样问 runs_per_prompt 次(产品文档 15.2;v0.4.26 S2)+ 重试与熔断(13.2)。

职责边界:
- 重试/退避/熔断全部在这里,适配器内禁止(13.1);
- 每次采样尝试必落一条 SurfaceRun,失败不蒸发(6.3):适配器抛出的
  Transient(重试耗尽)/Permanent 错误由本模块翻译为失败运行落库;
- 限流时遵守 Retry-After(v0.4.26 C5):等 max(退避, Retry-After),单次等待上限 max_wait_s;
  平台要求等得更久就不等了,这次直接记失败(有界等待,不挂死);
- CapabilityNotSupportedError 是编程错误:直接上抛,不落库、不重试;
- 调度器不内嵌任何判定逻辑:次数固定,不按结果提前停——同一道题的几次回答是一簇,
  不是独立样本,「收敛」无从谈起。

一个计划 = 一个被测对象(仪器 × 型号)× 一种检索模式,跨全部题目:失败率熔断窗口与预算都按计划计。
幂等键 = (题 × surface × 型号 × 检索模式 × 第几次):已落库的槽位不再提问——任何终态都算一次尝试,
拒答与失败如实进口径;账户问题行除外(那不是尝试,v0.4.21),其槽位续跑时重问。
"""

from __future__ import annotations

import random
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import structlog

from adapters.base import (
    AdapterAccountError,
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
    SurfaceAdapter,
)
from adapters.http_support import classify_account_problem
from engine.metrics import SampleAccounting, account_runs
from engine.models import (
    ACCOUNT_ERROR_TYPES,
    AskRequest,
    FrozenModel,
    Outcome,
    Prompt,
    RunError,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
)
from engine.validity import public_error_code

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

    def insert_call_usage(self, run_id: str, usage: dict[str, float]) -> None: ...


class SamplingPolicy(FrozenModel):
    """次数与重试/熔断参数(13.2,阈值可配)。"""

    runs_per_prompt: int = 3  # 每道题原样问几次(产品文档 15.2)
    max_attempts: int = 3  # 单次采样的最大尝试数(含首次),仅 Transient 重试
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    max_wait_s: float = 120.0  # 单次等待上限(含 Retry-After);平台要求等得更久就当这次失败(v0.4.26 C5)
    failure_window: int = 20
    failure_min_attempts: int = 4
    failure_rate_threshold: float = 0.5
    budget_usd: float | None = None  # 本计划成本上限;None = 不限
    timeout_s: float = 180.0  # v0.4.21:旗舰型号单发约 30 s,并发下 60 s 会误判超时


class SamplingResult(FrozenModel):
    """一个采样计划(被测对象 × 检索模式,跨全部题目)的结果与口径。"""

    provider: str
    surface: str
    model: str
    search_mode: SearchMode
    stop_reason: StopReason
    n_attempts: int
    accounting: SampleAccounting
    total_cost_usd: float
    run_ids: list[str]
    # S10.2(熔断要说清原因,用户定):没拿到有效回答的尝试按对外错误码分组计数,附最后一条平台原话
    failure_counts: dict[str, int] = {}
    last_failure_message: str | None = None


class StopDetail(FrozenModel):
    """一条腿怎么停的(S10.2):随报告快照存,任务页与报告按错误库组句
    「{AI} 的 {型号} 提前停了:n 次尝试里 k 次没拿到回答——a 次限流、b 次接口错误」,码与原话进折叠区。"""

    surface: str
    model: str
    search_mode: SearchMode
    stop_reason: StopReason
    n_attempts: int
    n_valid: int
    failure_counts: dict[str, int]
    last_failure_message: str | None

    @classmethod
    def from_result(cls, result: SamplingResult) -> StopDetail:
        return cls(surface=result.surface, model=result.model, search_mode=result.search_mode,
                   stop_reason=result.stop_reason, n_attempts=result.n_attempts, n_valid=result.accounting.n_valid,
                   failure_counts=dict(result.failure_counts), last_failure_message=result.last_failure_message)

    @property
    def truncated(self) -> bool:
        """提前停了(熔断 / 预算),报告要说清;问完的和重算不补采的不算。"""
        return self.stop_reason in (StopReason.FAILURE_RATE_EXCEEDED, StopReason.BUDGET_EXHAUSTED)


class RepeatSampler:
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
        prompts: list[Prompt],
        *,
        policy: SamplingPolicy | None = None,
        search_mode: SearchMode = SearchMode.DEFAULT,
        model: str | None = None,
        locale: str = "zh-CN",
        region: str | None = None,
        prior_runs: dict[str, list[SurfaceRun]] | None = None,
        allow_new_calls: bool = True,
    ) -> SamplingResult:
        """prior_runs:幂等续跑(v0.4.9)——按题已落库的运行(调用方必须已按 surface × 型号 × 检索模式
        筛过;型号是口径的一部分,v0.4.19)。已填的槽位(run_index)直接复用,指标与成本合并计算。
        allow_new_calls=False:只按既有样本重算(重新生成报告),一次新调用都不发;
        有空槽位就记 NOT_RESAMPLED。"""
        policy = policy or SamplingPolicy()
        meta = adapter.meta()
        resolved_model = model or meta.default_model
        prior_runs = prior_runs or {}

        counted: list[SurfaceRun] = []  # 进口径的尝试(账户问题行不算)
        run_ids: list[str] = []
        total_cost = 0.0
        n_account_error = 0
        # 失败率熔断只看本次运行(v0.4.21):它防的是"现在还在失败就别再花钱",上一次运行的
        # 失败已是过去式——用它开场就熔断,会让续跑一次新调用都发不出
        recent_failures: deque[bool] = deque(maxlen=policy.failure_window)
        stop_reason: StopReason | None = None

        for prompt in prompts:
            prior = prior_runs.get(prompt.prompt_id, [])
            n_account_error += sum(is_account_error(r) for r in prior)
            filled: dict[int, SurfaceRun] = {}
            for run in prior:
                if not is_account_error(run):
                    filled[run.run_index] = run
            for run in filled.values():
                counted.append(run)
                run_ids.append(run.run_id)
                total_cost += run.cost_usd
            for run_index in range(1, policy.runs_per_prompt + 1):
                if run_index in filled:
                    continue
                stop_reason = self._pre_call_stop(
                    recent_failures, total_cost, policy, allow_new_calls=allow_new_calls, surface=meta.surface,
                )
                if stop_reason is not None:
                    break
                request = AskRequest(
                    run_id=str(uuid4()),
                    prompt_id=prompt.prompt_id,
                    run_index=run_index,
                    prompt_text=prompt.text,
                    search_mode=search_mode,
                    model=model,
                    locale=locale,
                    region=region,
                    timeout_s=policy.timeout_s,
                )
                run = ask_with_retries(adapter, request, policy, sleep=self._sleep, rng=self._rng)
                self._storage.insert_surface_run(run)
                if run.outcome is Outcome.SUCCESS:
                    usage = adapter.usage_of(run.raw_payload)  # 计费用量进派生表,确认页按它估价(成本调研②)
                    if usage:
                        self._storage.insert_call_usage(run.run_id, usage)
                if is_account_error(run):  # 落库后立刻停:不进本计划的任何统计,也不再花一分钱
                    assert run.error is not None
                    raise AccountProblem(provider=meta.provider, surface=meta.surface, model=resolved_model,
                                         error_type=run.error.error_type, message=run.error.message)
                counted.append(run)
                run_ids.append(run.run_id)
                total_cost += run.cost_usd
                recent_failures.append(
                    run.outcome in (Outcome.TRANSIENT_FAILURE, Outcome.PERMANENT_FAILURE)
                )
                code = public_error_code(run)
                log.info("sample_recorded", run_id=run.run_id, surface=meta.surface, prompt_id=prompt.prompt_id,
                         run_index=run_index, outcome=run.outcome.value, latency_ms=run.latency_ms,
                         cost_usd=run.cost_usd, error_code=code.value if code else None)
            if stop_reason is not None:
                break

        codes = [code for run in counted if (code := public_error_code(run)) is not None]
        result = SamplingResult(
            provider=meta.provider,
            surface=meta.surface,
            model=resolved_model,
            search_mode=search_mode,
            stop_reason=stop_reason or StopReason.COMPLETED,
            n_attempts=len(counted),
            accounting=account_runs(counted, n_account_error=n_account_error),
            total_cost_usd=total_cost,
            run_ids=run_ids,
            failure_counts={code.value: n for code, n in Counter(codes).items()},
            last_failure_message=next((r.error.message for r in reversed(counted) if r.error is not None), None),
        )
        log.info("plan_stopped", surface=meta.surface, model=resolved_model, search_mode=search_mode.value,
                 stop_reason=result.stop_reason.value, n_attempts=result.n_attempts,
                 n_valid=result.accounting.n_valid, cost_usd=total_cost)
        return result

    @staticmethod
    def _pre_call_stop(
        recent_failures: deque[bool], total_cost: float, policy: SamplingPolicy, *,
        allow_new_calls: bool, surface: str,
    ) -> StopReason | None:
        """采样前检查(会阻止花钱的停止条件)。"""
        if (
            len(recent_failures) >= policy.failure_min_attempts
            and sum(recent_failures) / len(recent_failures) > policy.failure_rate_threshold
        ):
            log.warning("failure_breaker_tripped", surface=surface,
                        window=len(recent_failures), failures=sum(recent_failures))
            return StopReason.FAILURE_RATE_EXCEEDED
        if policy.budget_usd is not None and total_cost >= policy.budget_usd:
            log.warning("budget_exhausted", surface=surface, cost_usd=total_cost)
            return StopReason.BUDGET_EXHAUSTED
        if not allow_new_calls:  # 只重算:结构上不可能再花钱
            return StopReason.NOT_RESAMPLED
        return None


def ask_with_retries(
    adapter: SurfaceAdapter,
    request: AskRequest,
    policy: SamplingPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> SurfaceRun:
    """一次调用 = 最多 max_attempts 次尝试(仅 Transient 重试,指数退避+抖动;限流时等 max(退避, Retry-After),
    单次等待不超过 max_wait_s——平台要求等得更久就不等、这次直接记失败)。耗尽 / 永久失败 →
    构造失败 SurfaceRun 返回(失败不蒸发)。采样与辅助 AI 调用(engine/ai_task.py)共用这一套(13.2)。

    花费也不蒸发(第二轮审核 26):平台回了话却报失败的尝试照样计费,适配器把那笔钱带在异常上;一次调用的
    cost_usd = 各次尝试花费之和——失败收场记在失败行上,重试后成功就加到成功那一行(预算与实测均值都按真花的算)。"""
    rng = rng or random.Random()
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    attempts = 0
    spent = 0.0  # 之前几次失败尝试已经花掉的钱
    while True:
        attempts += 1
        try:
            run = adapter.ask(request)
        except AdapterTransientError as exc:
            spent += exc.cost_usd
            if attempts >= policy.max_attempts:
                log.warning("adapter_retries_exhausted", run_id=request.run_id, surface=adapter.meta().surface,
                            attempt=attempts, http_status=exc.http_status, error=str(exc))
                return _failure_run(
                    adapter, request, started_at, t0,
                    outcome=Outcome.TRANSIENT_FAILURE,
                    error=RunError(
                        error_type=RunErrorType.TRANSIENT, message=str(exc),
                        http_status=exc.http_status, attempts=attempts,
                    ),
                    raw_payload=_error_body(exc.response_body),
                    cost_usd=spent,
                )
            cap = min(policy.backoff_max_s, policy.backoff_base_s * (2 ** (attempts - 1)))
            wait_s = rng.uniform(0, cap)
            if exc.retry_after_s is not None:
                wait_s = max(wait_s, exc.retry_after_s)
            if wait_s > policy.max_wait_s:  # 等不起:不挂死,这次直接记失败(有界等待)
                message = (f"{exc}(平台要求等 {wait_s:.0f} 秒,超过单次等待上限 "
                           f"{policy.max_wait_s:.0f} 秒,本次不再重试)")
                log.warning("adapter_wait_exceeds_cap", run_id=request.run_id, surface=adapter.meta().surface,
                            attempt=attempts, http_status=exc.http_status, retry_after_s=exc.retry_after_s,
                            max_wait_s=policy.max_wait_s)
                return _failure_run(
                    adapter, request, started_at, t0,
                    outcome=Outcome.TRANSIENT_FAILURE,
                    error=RunError(
                        error_type=RunErrorType.TRANSIENT, message=message,
                        http_status=exc.http_status, attempts=attempts,
                    ),
                    raw_payload=_error_body(exc.response_body),
                    cost_usd=spent,
                )
            log.warning("adapter_retry", run_id=request.run_id, surface=adapter.meta().surface,
                        attempt=attempts, http_status=exc.http_status, retry_after_s=exc.retry_after_s,
                        wait_s=round(wait_s, 1), error=str(exc))
            sleep(wait_s)
        except AdapterAccountError as exc:  # 子类须先于 AdapterPermanentError 捕获
            spent += exc.cost_usd
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
                raw_payload=_error_body(exc.response_body),
                cost_usd=spent,
            )
        except AdapterPermanentError as exc:
            spent += exc.cost_usd
            log.error("adapter_permanent_failure", run_id=request.run_id,
                      surface=adapter.meta().surface, http_status=exc.http_status, error=str(exc))
            return _failure_run(
                adapter, request, started_at, t0,
                outcome=Outcome.PERMANENT_FAILURE,
                error=RunError(
                    error_type=RunErrorType.PERMANENT, message=str(exc),
                    http_status=exc.http_status, attempts=attempts,
                ),
                raw_payload=_error_body(exc.response_body),
                cost_usd=spent,
            )
        except CapabilityNotSupportedError:
            raise  # 编程错误直接上抛,不落 SurfaceRun(13.1)
        except Exception as exc:  # 三类契约之外的异常 = 适配器契约违规(13.4 记 ERROR):当永久失败落库,整单不崩
            # 平台返回了适配器没见过的结构时,翻译代码可能抛 AttributeError / 校验错之类;这次尝试照样要有失败行
            # (失败不蒸发),要修的是适配器,不该让一次怪返回把整单撂倒(审核 2026-10-27 黄 4)
            # 说法(第二轮审核 B9):这条路也接住适配器自己的代码出错,原来一律说「平台返回了没见过的结构」会误导;
            # 平台原始响应在这里拿不到,改存出错的位置(哪个文件哪一行),修适配器时一眼能找到
            where = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ is not None else None
            at = f"{Path(where.filename).name}:{where.lineno} {where.name}" if where is not None else "?"
            log.error("adapter_contract_violation", run_id=request.run_id, surface=adapter.meta().surface,
                      error=f"{type(exc).__name__}: {exc}", at=at)
            return _failure_run(
                adapter, request, started_at, t0,
                outcome=Outcome.PERMANENT_FAILURE,
                error=RunError(
                    error_type=RunErrorType.PERMANENT,
                    message=(f"适配器抛出契约之外的异常(可能是平台返回了没见过的结构,也可能是适配器自己的代码出错,"
                             f"要修适配器;出错位置 {at}): {type(exc).__name__}: {exc}"),
                    attempts=attempts,
                ),
                raw_payload={"contract_violation": {"exception": type(exc).__name__, "message": str(exc)[:2000],
                                                    "at": at}},
                cost_usd=spent,  # 这次花了多少看不出,之前几次失败尝试花掉的照记
            )
        else:
            if spent:
                log.info("charged_failed_attempts", run_id=request.run_id, surface=adapter.meta().surface,
                         attempts=attempts, spent_usd=round(spent, 6))
                return run.model_copy(update={"cost_usd": run.cost_usd + spent})
            return run


def _error_body(response_body: str | None) -> dict[str, Any] | None:
    """平台响应体(有的话)按原样存进失败行的 raw_payload。"""
    return {"error_response_text": response_body} if response_body is not None else None


def _failure_run(
    adapter: SurfaceAdapter,
    request: AskRequest,
    started_at: datetime,
    t0: float,
    *,
    outcome: Outcome,
    error: RunError,
    raw_payload: dict[str, Any] | None = None,
    cost_usd: float = 0.0,
) -> SurfaceRun:
    meta = adapter.meta()  # 只在失败时取:构造失败记录要知道是哪台仪器、哪个型号
    return SurfaceRun(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
        provider=meta.provider,
        surface=meta.surface,
        model=request.model or meta.default_model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        outcome=outcome,
        error=error,
        # SearchTrace 默认全 UNKNOWN/NOT_OBSERVABLE;失败时无回答
        # 失败的证据也不蒸发(13.1):平台响应体,或契约外异常的出错位置
        raw_payload=raw_payload,
        timestamp=started_at,
        latency_ms=int((time.perf_counter() - t0) * 1000),  # 含重试与退避的真实耗时
        cost_usd=cost_usd,  # 各次尝试里平台回了话却报失败时照样计的费(第二轮审核 26)
    )
