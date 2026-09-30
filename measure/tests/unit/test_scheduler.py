"""调度器表驱动测试(12.1):构造观测序列(含失败与拒答)→
断言停止点、stop_reason、分母计数、重试与熔断行为。零网络零数据库。"""

import random
from datetime import UTC, datetime

import pytest

from adapters.base import (
    AdapterAccountError,
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from engine.metrics import jeffreys_interval
from engine.models import (
    Answer,
    AskRequest,
    InformationNeed,
    NeedIntent,
    Outcome,
    PromptOrigin,
    PromptVariant,
    RunError,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
)
from engine.scheduler import AccountProblem, SamplingPolicy, SequentialSampler

NEED = InformationNeed(need_id="need-1", statement="测试需求", intent=NeedIntent.DISCOVERY)
VARIANT = PromptVariant(
    variant_id="variant-1", need_id="need-1", prompt_text="测试问题?", origin=PromptOrigin.USER
)


class MemoryStorage:
    def __init__(self):
        self.runs: list[SurfaceRun] = []

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self.runs.append(run)


class ScriptedAdapter(SurfaceAdapter):
    """脚本化适配器:script 元素按 ask() 调用逐个消费(重试也消费),
    耗尽后重复最后一个。元素:'hit' / 'miss' / 'refuse' 或异常实例。"""

    def __init__(self, script):
        self._script = list(script)
        self._calls = 0

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            display_name="Scripted", provider="test_provider", surface="test_surface",
            models=["m1"], default_model="m1",
            capabilities=CapabilityRegistry(supports_citations=True),
            est_cost_per_call_usd={"m1": 0.005},
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        step = self._script[min(self._calls, len(self._script) - 1)]
        self._calls += 1
        if isinstance(step, Exception):
            raise step
        common = dict(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="test_provider", surface="test_surface", model="m1",
            search_mode=request.search_mode, locale=request.locale,
            timestamp=datetime.now(UTC), latency_ms=10, cost_usd=0.005,
        )
        if step == "refuse":
            return SurfaceRun(
                outcome=Outcome.REFUSED,
                error=RunError(error_type=RunErrorType.REFUSAL, message="filtered"),
                raw_payload={}, **common,
            )
        return SurfaceRun(
            outcome=Outcome.SUCCESS,
            answer=Answer(answer_text="回答", citations_available=True),
            raw_payload={"hit": step == "hit"}, **common,
        )

    def healthcheck(self) -> bool:
        return True


def hit_fn(run: SurfaceRun):
    return run.raw_payload["hit"]


def make_sampler(storage, sleeps=None):
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    return SequentialSampler(storage, sleep=sleep, rng=random.Random(0))


def first_converging_n(width_threshold: float = 0.20, min_n: int = 5) -> int:
    """全命中流下首个满足宽度阈值的 n(用 metrics 同源函数推期望停止点)。"""
    for n in range(min_n, 100):
        low, high = jeffreys_interval(n, n)
        if high - low <= width_threshold:
            return n
    raise AssertionError("unreachable")


def test_stable_hits_converge():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn
    )
    expected_n = first_converging_n()
    assert result.stop_reason is StopReason.CI_CONVERGED
    assert result.n_attempts == expected_n == len(storage.runs)
    assert result.accounting.n_valid == expected_n
    assert result.k_hits == expected_n
    assert result.estimate.ci_high == 1.0
    assert len(set(result.run_ids)) == expected_n  # run_id 唯一


def test_alternating_hits_stop_at_max_n():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit", "miss"] * 20), NEED, VARIANT, hit_fn=hit_fn
    )
    assert result.stop_reason is StopReason.MAX_N_REACHED
    assert result.accounting.n_valid == 20
    assert result.k_hits == 10
    assert result.estimate.width > 0.20  # 确实没收敛,报告须注明


def test_refusals_excluded_from_denominator_but_recorded():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["refuse", "hit"]), NEED, VARIANT, hit_fn=hit_fn
    )
    assert result.stop_reason is StopReason.CI_CONVERGED
    assert result.accounting.n_refused == 1  # 拒答落库进口径
    assert result.accounting.n_valid == first_converging_n()  # 但不进分母
    refused = [r for r in storage.runs if r.outcome is Outcome.REFUSED]
    assert len(refused) == 1 and refused[0].error.error_type is RunErrorType.REFUSAL


def make_prior(run_id: str, *, outcome=Outcome.SUCCESS, hit: bool = True,
               cost: float = 0.005) -> SurfaceRun:
    """幂等续跑测试用:模拟此前会话已落库的运行(raw_payload 携带 hit_fn 判据)。"""
    return SurfaceRun(
        run_id=run_id, information_need_id="need-1", prompt_variant_id="variant-1",
        provider="test_provider", surface="test_surface", model="test-model",
        search_mode=SearchMode.DEFAULT, locale="zh-CN",
        outcome=outcome,
        error=(None if outcome is Outcome.SUCCESS
               else RunError(error_type=RunErrorType.TRANSIENT, message="x")),
        raw_payload={"hit": hit},
        timestamp=datetime(2026, 9, 8, tzinfo=UTC), latency_ms=1, cost_usd=cost,
    )


def test_resume_with_converged_priors_makes_zero_calls():
    """幂等续跑(v0.4.9):既有样本已收敛 → 零新调用、零新落库。"""
    storage = MemoryStorage()
    priors = [make_prior(f"p-{i}") for i in range(first_converging_n())]
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn, prior_runs=priors
    )
    assert result.stop_reason is StopReason.CI_CONVERGED
    assert storage.runs == []  # 一分钱新调用都没发
    assert result.n_attempts == len(priors)
    assert result.k_hits == len(priors)
    assert set(result.run_ids) == {p.run_id for p in priors}


def test_replay_only_never_calls_even_when_priors_have_not_converged():
    """重新生成报告(allow_new_calls=False):已有样本没满足停止条件(如官网名单后改、判据变了)
    也一次新调用都不发,如实记 NOT_RESAMPLED——「重新生成不花钱」由结构保证。"""
    storage = MemoryStorage()
    adapter = ScriptedAdapter(["hit"])
    result = make_sampler(storage).run_plan(
        adapter, NEED, VARIANT, hit_fn=hit_fn,
        prior_runs=[make_prior("p-0"), make_prior("p-1")], allow_new_calls=False,
    )
    assert result.stop_reason is StopReason.NOT_RESAMPLED
    assert storage.runs == [] and result.n_attempts == 2 and result.k_hits == 2


def test_resume_with_partial_priors_fills_gap_only():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn,
        prior_runs=[make_prior("p-0"), make_prior("p-1")],
    )
    assert result.stop_reason is StopReason.CI_CONVERGED
    assert result.n_attempts == first_converging_n()  # 口径合并:前缀 + 新采样
    assert len(storage.runs) == first_converging_n() - 2  # 只补缺口
    assert result.estimate is not None and result.estimate.n == first_converging_n()


def test_resume_priors_count_against_budget():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn,
        policy=SamplingPolicy(budget_usd=0.01),
        prior_runs=[make_prior("p-0", cost=0.02)],
    )
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert storage.runs == []  # 已花的钱计入预算,不再烧新钱
    assert result.total_cost_usd == pytest.approx(0.02)


def test_failure_run_archives_platform_error_body():
    """13.1 增补:失败的证据也不蒸发——平台错误响应体(截断)随失败运行存档。"""
    storage = MemoryStorage()
    body = '{"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}'
    make_sampler(storage).run_plan(
        ScriptedAdapter(
            [AdapterPermanentError("非法请求 HTTP 400", http_status=400, response_body=body), "hit"]
        ),
        NEED, VARIANT, hit_fn=hit_fn,
    )
    failure = storage.runs[0]
    assert failure.outcome is Outcome.PERMANENT_FAILURE
    assert failure.raw_payload == {"error_response_text": body}
    # 无响应体的失败(如超时)raw_payload 保持 None,不臆造
    storage2 = MemoryStorage()
    make_sampler(storage2).run_plan(
        ScriptedAdapter([AdapterTransientError("请求超时")] * 3 + ["hit"]),
        NEED, VARIANT, hit_fn=hit_fn, policy=SamplingPolicy(max_attempts=3),
    )
    assert storage2.runs[0].outcome is Outcome.TRANSIENT_FAILURE
    assert storage2.runs[0].raw_payload is None


def test_transient_retries_then_breaker():
    storage = MemoryStorage()
    sleeps = []
    policy = SamplingPolicy()
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterTransientError("限流", http_status=429)]),
        NEED, VARIANT, hit_fn=hit_fn, policy=policy,
    )
    # 每次采样 3 次尝试(2 次退避),连续 4 次失败样本后熔断
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED
    assert result.n_attempts == policy.failure_min_attempts == len(storage.runs)
    assert all(r.outcome is Outcome.TRANSIENT_FAILURE for r in storage.runs)
    assert all(r.error.attempts == policy.max_attempts for r in storage.runs)
    assert all(r.error.http_status == 429 for r in storage.runs)
    assert len(sleeps) == policy.failure_min_attempts * (policy.max_attempts - 1)
    assert all(0 <= s <= policy.backoff_max_s for s in sleeps)
    assert result.estimate is None and result.accounting.n_valid == 0


def test_permanent_failure_no_retry():
    storage = MemoryStorage()
    sleeps = []
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterPermanentError("认证失败", http_status=401)]),
        NEED, VARIANT, hit_fn=hit_fn,
    )
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED
    assert sleeps == []  # Permanent 不重试不退避
    assert all(r.outcome is Outcome.PERMANENT_FAILURE for r in storage.runs)
    assert all(r.error.attempts == 1 for r in storage.runs)


def test_budget_breaker_stops_before_spending():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn,
        policy=SamplingPolicy(budget_usd=0.012),
    )
    # 每次 0.005:第 3 次后累计 0.015 >= 预算,第 4 次不再发生
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.n_attempts == 3
    assert result.total_cost_usd == pytest.approx(0.015)


def test_all_refused_hits_total_attempt_cap():
    storage = MemoryStorage()
    policy = SamplingPolicy(max_total_attempts=10)
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["refuse"]), NEED, VARIANT, hit_fn=hit_fn, policy=policy,
    )
    assert result.stop_reason is StopReason.MAX_N_REACHED
    assert result.n_attempts == 10
    assert result.accounting.n_refused == 10
    assert result.accounting.low_confidence is True  # 真相由口径呈现
    assert result.estimate is None


def test_unobservable_hits_do_not_enter_denominator():
    storage = MemoryStorage()
    policy = SamplingPolicy(max_total_attempts=7)
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT,
        hit_fn=lambda run: None,  # SUCCESS 但指标不可观测
        policy=policy,
    )
    assert result.stop_reason is StopReason.MAX_N_REACHED
    assert result.accounting.n_valid == 7  # SUCCESS 计入记账
    assert result.k_hits == 0 and result.estimate is None  # 但分母为空


def test_capability_error_propagates_without_recording():
    storage = MemoryStorage()
    with pytest.raises(CapabilityNotSupportedError):
        make_sampler(storage).run_plan(
            ScriptedAdapter([CapabilityNotSupportedError("未声明能力")]),
            NEED, VARIANT, hit_fn=hit_fn,
        )
    assert storage.runs == []  # 编程错误不落 SurfaceRun(13.1)


# ── 账户问题(v0.4.21):不是观测,落库后立刻停,不进任何比率 ─────────────────────


def test_account_error_is_recorded_then_halts_plan_without_retry():
    storage = MemoryStorage()
    sleeps = []
    body = '{"error":{"type":"invalid_request_error","message":"Your credit balance is too low"}}'
    with pytest.raises(AccountProblem) as exc_info:
        make_sampler(storage, sleeps).run_plan(
            ScriptedAdapter([
                AdapterAccountError(f"账户额度不足或欠费(HTTP 400): {body}", error_type=RunErrorType.ACCOUNT_CREDIT,
                                    http_status=400, response_body=body),
                "hit",
            ]),
            NEED, VARIANT, hit_fn=hit_fn,
        )
    assert sleeps == []  # 不重试不退避:充值前再试也是白试
    [run] = storage.runs  # 失败不蒸发:这一次尝试照常落库
    assert run.outcome is Outcome.PERMANENT_FAILURE and run.error.error_type is RunErrorType.ACCOUNT_CREDIT
    assert run.raw_payload == {"error_response_text": body}
    assert exc_info.value.model == "m1" and "credit balance" in exc_info.value.message
    assert exc_info.value.error_type is RunErrorType.ACCOUNT_CREDIT


def test_resume_excludes_account_error_priors_and_breaker_ignores_history():
    """续跑时:①账户问题行只计数,不进分母 / 失败率 / 熔断;②上一次运行的普通失败不再
    充当熔断窗口的开场——否则 4 条历史失败会让续跑一次新调用都发不出(2026-09-21 真实踩到)。"""
    storage = MemoryStorage()
    account_rows = [
        make_prior(f"acct-{i}", outcome=Outcome.PERMANENT_FAILURE).model_copy(update={
            "error": RunError(error_type=RunErrorType.ACCOUNT_AUTH if i % 2 else RunErrorType.ACCOUNT_CREDIT,
                              message="credit balance is too low",
                              http_status=400),
        })
        for i in range(4)
    ]
    stale_failures = [make_prior(f"old-fail-{i}", outcome=Outcome.TRANSIENT_FAILURE) for i in range(4)]
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["hit"]), NEED, VARIANT, hit_fn=hit_fn,
        prior_runs=account_rows + stale_failures + [make_prior("old-ok")],
    )
    assert result.stop_reason is StopReason.CI_CONVERGED  # 没被熔断,正常补齐后收敛
    assert len(storage.runs) == first_converging_n() - 1  # 只补缺口
    acc = result.accounting
    assert acc.n_account_error == 4
    assert acc.n_total == 4 + first_converging_n()  # 历史普通失败仍是观测,账户行不是
    assert acc.n_transient_failure == 4 and acc.n_valid == first_converging_n()
    assert not any(r.startswith("acct-") for r in result.run_ids)
