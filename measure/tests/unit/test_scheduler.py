"""调度器表驱动测试(12.1):构造观测序列(含失败与拒答)→
断言次数、stop_reason、分母计数、重试与熔断行为。零网络零数据库。
v0.4.26 S2:每道题原样问 runs_per_prompt 次,不按结果提前停。"""

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
from engine.models import (
    ACCOUNT_ERROR_TYPES,
    Answer,
    AskRequest,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    RunError,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
)
from engine.scheduler import AccountProblem, RepeatSampler, SamplingPolicy

P1 = Prompt(prompt_id="p-1", text="测试问题一?", intent=PromptIntent.CATEGORY_DISCOVERY,
            origin=PromptOrigin.USER, locale="zh-CN")
P2 = Prompt(prompt_id="p-2", text="测试问题二?", intent=PromptIntent.CATEGORY_DISCOVERY,
            origin=PromptOrigin.USER, locale="zh-CN")


class MemoryStorage:
    def __init__(self):
        self.runs: list[SurfaceRun] = []

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self.runs.append(run)


class ScriptedAdapter(SurfaceAdapter):
    """脚本化适配器:script 元素按 ask() 调用逐个消费(重试也消费),
    耗尽后重复最后一个。元素:'ok' / 'refuse' / 'empty'(SUCCESS 但一个字没给)或异常实例。"""

    def __init__(self, script):
        self._script = list(script)
        self._calls = 0

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            display_name="Scripted", api_name="Scripted", provider="test_provider", surface="test_surface",
            models=["m1"], default_model="m1",
            consumer_default_model="m1", main_model="m1", models_checked="test",
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
            prompt_id=request.prompt_id,
            run_index=request.run_index,
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
        if step == "empty":
            return SurfaceRun(outcome=Outcome.SUCCESS, answer=None, raw_payload={}, **common)
        return SurfaceRun(
            outcome=Outcome.SUCCESS,
            answer=Answer(answer_text="回答", citations_available=True),
            raw_payload={"text": request.prompt_text}, **common,
        )

    def healthcheck(self) -> bool:
        return True


def make_sampler(storage, sleeps=None):
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    return RepeatSampler(storage, sleep=sleep, rng=random.Random(0))


def test_every_prompt_is_asked_runs_per_prompt_times_verbatim():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(ScriptedAdapter(["ok"]), [P1, P2])
    assert result.stop_reason is StopReason.COMPLETED
    assert result.n_attempts == 6 == len(storage.runs) == result.accounting.n_valid
    assert [(r.prompt_id, r.run_index) for r in storage.runs] == [("p-1", 1), ("p-1", 2), ("p-1", 3),
                                                                   ("p-2", 1), ("p-2", 2), ("p-2", 3)]
    assert {r.raw_payload["text"] for r in storage.runs} == {P1.text, P2.text}  # 原样问,不改措辞
    assert len(set(result.run_ids)) == 6  # run_id 唯一
    assert result.total_cost_usd == pytest.approx(0.03)


def test_refusals_fill_their_slot_and_stay_out_of_denominator():
    """拒答也是一次尝试:槽位算填了(不补问),进口径、不进分母。"""
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(ScriptedAdapter(["refuse", "ok"]), [P1])
    assert result.stop_reason is StopReason.COMPLETED
    assert result.n_attempts == 3 and result.accounting.n_refused == 1 and result.accounting.n_valid == 2
    refused = [r for r in storage.runs if r.outcome is Outcome.REFUSED]
    assert len(refused) == 1 and refused[0].error.error_type is RunErrorType.REFUSAL


def test_empty_answers_fill_their_slot_but_count_as_invalid():
    """SUCCESS 但正文为空(v0.4.26 S3):占槽、花了钱、进口径,但不进分母——数量单独记在 n_empty。"""
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(ScriptedAdapter(["empty", "ok"]), [P1])
    assert result.stop_reason is StopReason.COMPLETED and len(storage.runs) == 3
    acc = result.accounting
    assert (acc.n_total, acc.n_valid, acc.n_empty, acc.n_refused) == (3, 2, 1, 0)
    assert storage.runs[0].outcome is Outcome.SUCCESS and storage.runs[0].answer is None  # 原始层原样,不改写


def test_retry_after_is_honored_within_the_cap():
    """限流时等 max(退避, Retry-After)(v0.4.26 C5):首轮退避抖动 ≤ 1 秒,平台要 5 秒 → 等 5 秒。"""
    storage = MemoryStorage()
    sleeps = []
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterTransientError("限流", http_status=429, retry_after_s=5.0), "ok"]), [P1],
    )
    assert sleeps == [5.0] and result.accounting.n_valid == 3  # 等完一次就成功了


def test_retry_after_beyond_the_cap_fails_now_without_waiting():
    """平台要求等 300 秒 > 上限 120 秒:不挂死、不违约去硬试——这次直接记失败,计划继续下一槽位。"""
    storage = MemoryStorage()
    sleeps = []
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterTransientError("限流", http_status=429, retry_after_s=300.0), "ok"]), [P1],
    )
    assert sleeps == []
    failure = storage.runs[0]
    assert failure.outcome is Outcome.TRANSIENT_FAILURE and failure.error.attempts == 1
    assert failure.error.http_status == 429 and "上限" in failure.error.message and "300" in failure.error.message
    assert result.stop_reason is StopReason.COMPLETED and result.accounting.n_valid == 2  # 其余槽位照常补齐


def make_prior(run_id: str, run_index: int, *, prompt_id: str = "p-1", outcome=Outcome.SUCCESS,
               cost: float = 0.005) -> SurfaceRun:
    """幂等续跑测试用:模拟此前会话已落库的运行。"""
    return SurfaceRun(
        run_id=run_id, prompt_id=prompt_id, run_index=run_index,
        provider="test_provider", surface="test_surface", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN",
        outcome=outcome,
        error=(None if outcome is Outcome.SUCCESS
               else RunError(error_type=RunErrorType.TRANSIENT, message="x")),
        answer=Answer(answer_text="回答", citations_available=True) if outcome is Outcome.SUCCESS else None,
        raw_payload={},
        timestamp=datetime(2026, 9, 8, tzinfo=UTC), latency_ms=1, cost_usd=cost,
    )


def test_resume_with_all_slots_filled_makes_zero_calls():
    """幂等续跑(v0.4.9):槽位都填了 → 零新调用、零新落库。"""
    storage = MemoryStorage()
    priors = {"p-1": [make_prior(f"p-{i}", i) for i in (1, 2, 3)]}
    result = make_sampler(storage).run_plan(ScriptedAdapter(["ok"]), [P1], prior_runs=priors)
    assert result.stop_reason is StopReason.COMPLETED
    assert storage.runs == []  # 一分钱新调用都没发
    assert result.n_attempts == 3 and set(result.run_ids) == {"p-1", "p-2", "p-3"}


def test_replay_only_never_calls_even_with_empty_slots():
    """重新生成报告(allow_new_calls=False):有空槽位也一次新调用都不发,如实记 NOT_RESAMPLED
    ——「重新生成不花钱」由结构保证。"""
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["ok"]), [P1], prior_runs={"p-1": [make_prior("a", 1)]}, allow_new_calls=False,
    )
    assert result.stop_reason is StopReason.NOT_RESAMPLED
    assert storage.runs == [] and result.n_attempts == 1


def test_resume_fills_only_empty_slots():
    storage = MemoryStorage()
    priors = {"p-1": [make_prior("a", 1), make_prior("b", 3)]}
    result = make_sampler(storage).run_plan(ScriptedAdapter(["ok"]), [P1, P2], prior_runs=priors)
    assert result.stop_reason is StopReason.COMPLETED
    assert [(r.prompt_id, r.run_index) for r in storage.runs] == [("p-1", 2), ("p-2", 1), ("p-2", 2), ("p-2", 3)]
    assert result.n_attempts == 6  # 口径合并:已有 + 新采样


def test_resume_priors_count_against_budget():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["ok"]), [P1],
        policy=SamplingPolicy(budget_usd=0.01),
        prior_runs={"p-1": [make_prior("a", 1, cost=0.02)]},
    )
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert storage.runs == []  # 已花的钱计入预算,不再烧新钱
    assert result.total_cost_usd == pytest.approx(0.02)


def test_failure_run_archives_platform_error_body():
    """13.1 增补:失败的证据也不蒸发——平台错误响应体(截断)随失败运行存档。"""
    storage = MemoryStorage()
    body = '{"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}'
    make_sampler(storage).run_plan(
        ScriptedAdapter([AdapterPermanentError("非法请求 HTTP 400", http_status=400, response_body=body), "ok"]),
        [P1],
    )
    failure = storage.runs[0]
    assert failure.outcome is Outcome.PERMANENT_FAILURE and failure.run_index == 1
    assert failure.raw_payload == {"error_response_text": body}
    assert len(storage.runs) == 3  # 失败占了槽位,不补问
    # 无响应体的失败(如超时)raw_payload 保持 None,不臆造
    storage2 = MemoryStorage()
    make_sampler(storage2).run_plan(
        ScriptedAdapter([AdapterTransientError("请求超时")] * 3 + ["ok"]), [P1],
        policy=SamplingPolicy(max_attempts=3),
    )
    assert storage2.runs[0].outcome is Outcome.TRANSIENT_FAILURE
    assert storage2.runs[0].raw_payload is None


def test_transient_retries_then_breaker():
    storage = MemoryStorage()
    sleeps = []
    policy = SamplingPolicy()
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterTransientError("限流", http_status=429)]), [P1, P2], policy=policy,
    )
    # 每次采样 3 次尝试(2 次退避),连续 4 次失败样本后熔断(计划级窗口,跨题)
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED
    assert result.n_attempts == policy.failure_min_attempts == len(storage.runs)
    assert all(r.outcome is Outcome.TRANSIENT_FAILURE for r in storage.runs)
    assert all(r.error.attempts == policy.max_attempts for r in storage.runs)
    assert all(r.error.http_status == 429 for r in storage.runs)
    assert len(sleeps) == policy.failure_min_attempts * (policy.max_attempts - 1)
    assert all(0 <= s <= policy.backoff_max_s for s in sleeps)
    assert result.accounting.n_valid == 0


def test_permanent_failure_no_retry():
    storage = MemoryStorage()
    sleeps = []
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AdapterPermanentError("认证失败", http_status=401)]), [P1, P2],
    )
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED
    assert sleeps == []  # Permanent 不重试不退避
    assert all(r.outcome is Outcome.PERMANENT_FAILURE for r in storage.runs)
    assert all(r.error.attempts == 1 for r in storage.runs)


def test_budget_breaker_stops_before_spending():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["ok"]), [P1, P2], policy=SamplingPolicy(budget_usd=0.012),
    )
    # 每次 0.005:第 3 次后累计 0.015 >= 预算,第 4 次不再发生
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.n_attempts == 3
    assert result.total_cost_usd == pytest.approx(0.015)


def test_all_refused_still_completes_with_low_confidence_in_caliber():
    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(ScriptedAdapter(["refuse"]), [P1, P2])
    assert result.stop_reason is StopReason.COMPLETED
    assert result.n_attempts == 6 and result.accounting.n_refused == 6
    assert result.accounting.low_confidence is True  # 真相由口径呈现


def test_capability_error_propagates_without_recording():
    storage = MemoryStorage()
    with pytest.raises(CapabilityNotSupportedError):
        make_sampler(storage).run_plan(ScriptedAdapter([CapabilityNotSupportedError("未声明能力")]), [P1])
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
                "ok",
            ]),
            [P1],
        )
    assert sleeps == []  # 不重试不退避:充值前再试也是白试
    [run] = storage.runs  # 失败不蒸发:这一次尝试照常落库
    assert run.outcome is Outcome.PERMANENT_FAILURE and run.error.error_type is RunErrorType.ACCOUNT_CREDIT
    assert run.raw_payload == {"error_response_text": body}
    assert exc_info.value.model == "m1" and "credit balance" in exc_info.value.message
    assert exc_info.value.error_type is RunErrorType.ACCOUNT_CREDIT


def test_resume_excludes_account_error_priors_and_breaker_ignores_history():
    """续跑时:①账户问题行只计数、不占槽位,不进分母 / 失败率 / 熔断;②上一次运行的普通失败不再
    充当熔断窗口的开场——否则几条历史失败会让续跑一次新调用都发不出(2026-09-21 真实踩到)。"""
    storage = MemoryStorage()
    account_rows = [
        make_prior(f"acct-{i}", run_index=i % 3 + 1, outcome=Outcome.PERMANENT_FAILURE).model_copy(update={
            "error": RunError(error_type=RunErrorType.ACCOUNT_AUTH if i % 2 else RunErrorType.ACCOUNT_CREDIT,
                              message="credit balance is too low",
                              http_status=400),
        })
        for i in range(4)
    ]
    stale = [make_prior("old-fail", 1, outcome=Outcome.TRANSIENT_FAILURE), make_prior("old-ok", 2)]
    result = make_sampler(storage).run_plan(
        ScriptedAdapter(["ok"]), [P1], prior_runs={"p-1": account_rows + stale},
    )
    assert result.stop_reason is StopReason.COMPLETED  # 没被熔断,正常补齐
    assert [(r.prompt_id, r.run_index) for r in storage.runs] == [("p-1", 3)]  # 只补空槽位
    acc = result.accounting
    assert acc.n_account_error == 4
    assert acc.n_total == 3  # 历史普通失败仍是观测,账户行不是
    assert acc.n_transient_failure == 1 and acc.n_valid == 2
    assert not any(r.startswith("acct-") for r in result.run_ids)


def test_result_carries_failure_counts_by_code_and_the_last_platform_message():
    """S10.2(熔断要说清原因):没拿到有效回答的尝试按对外错误码分组计数,附最后一条平台原话;StopDetail 原样带走。"""
    from engine.scheduler import StopDetail

    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter([AdapterTransientError("限流 429", http_status=429)]), [P1, P2], policy=SamplingPolicy(),
    )
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED
    assert result.failure_counts == {"AI_PROVIDER_RATE_LIMITED": result.n_attempts}
    assert result.last_failure_message and "限流" in result.last_failure_message
    detail = StopDetail.from_result(result)
    assert detail.truncated and detail.n_valid == 0 and detail.failure_counts == result.failure_counts
    assert detail.last_failure_message == result.last_failure_message
    mixed = make_sampler(MemoryStorage()).run_plan(ScriptedAdapter(["refuse", "ok"]), [P1], policy=SamplingPolicy())
    assert mixed.stop_reason is StopReason.COMPLETED and mixed.failure_counts == {"AI_RESPONSE_INVALID": 1}
    assert mixed.last_failure_message == "filtered" and StopDetail.from_result(mixed).truncated is False


def test_non_contract_exception_is_recorded_as_permanent_failure_not_a_crash():
    """适配器抛了三类契约之外的异常(平台返回了没见过的结构,翻译代码炸了):当永久失败落库、整单不崩、
    这次尝试不蒸发(审核 2026-10-27 黄 4);CapabilityNotSupportedError 仍直接上抛(见上一个测试)。"""
    storage = MemoryStorage()
    sleeps = []
    result = make_sampler(storage, sleeps).run_plan(
        ScriptedAdapter([AttributeError("'str' object has no attribute 'get'")]), [P1, P2],
    )
    assert result.stop_reason is StopReason.FAILURE_RATE_EXCEEDED and sleeps == []  # 不重试不退避
    assert storage.runs and all(r.outcome is Outcome.PERMANENT_FAILURE and r.error.attempts == 1 for r in storage.runs)
    assert all("AttributeError" in r.error.message and "契约之外" in r.error.message for r in storage.runs)


def test_money_spent_on_failed_attempts_is_recorded_not_lost():
    """平台回了话却报失败的尝试照样计费(第二轮审核 26:原来失败行一律记 0 元)。
    失败收场 → 各次尝试的花费记在失败行上;重试后成功 → 加到成功那一行;预算按真花的算。"""
    def charged():
        return AdapterTransientError("响应 status=failed", response_body="{}", cost_usd=0.02)

    storage = MemoryStorage()
    result = make_sampler(storage).run_plan(
        ScriptedAdapter([charged(), charged(), charged(), charged(), "ok"]), [P1],
        policy=SamplingPolicy(max_attempts=3),
    )
    failed, retried, clean = storage.runs
    assert failed.outcome is Outcome.TRANSIENT_FAILURE and failed.cost_usd == pytest.approx(0.06)
    assert retried.outcome is Outcome.SUCCESS and retried.cost_usd == pytest.approx(0.005 + 0.02)
    assert clean.cost_usd == pytest.approx(0.005)
    assert result.total_cost_usd == pytest.approx(0.06 + 0.025 + 0.005)

    storage2 = MemoryStorage()
    make_sampler(storage2).run_plan(
        ScriptedAdapter([AdapterPermanentError("平台返回 status='failed'", cost_usd=0.01), "ok"]), [P1],
    )
    assert storage2.runs[0].outcome is Outcome.PERMANENT_FAILURE and storage2.runs[0].cost_usd == pytest.approx(0.01)


@pytest.mark.parametrize("kind", sorted(ACCOUNT_ERROR_TYPES))
def test_every_kind_of_account_problem_stops_the_plan_without_retry(kind):
    """五种账户问题(换 key / 充值 / 地区不支持 / 账户受限 / 额度用完)都不重试、照常落库、整单停下,带上是哪一种。"""
    storage = MemoryStorage()
    sleeps: list[float] = []
    with pytest.raises(AccountProblem) as exc_info:
        make_sampler(storage, sleeps).run_plan(
            ScriptedAdapter([AdapterAccountError("账户问题", error_type=kind, http_status=403), "ok"]), [P1],
        )
    assert sleeps == [] and exc_info.value.error_type is kind
    [run] = storage.runs
    assert run.error.error_type is kind


def test_non_contract_exception_says_it_may_be_the_adapters_own_bug_and_keeps_where_it_happened():
    """兜底那条失败行(第二轮审核 B9):原来一律说「平台返回了没见过的结构」,适配器自己的代码出错也这么说,会误导;
    平台原始响应在这里拿不到,改存出错的位置,修适配器时一眼能找到。"""
    storage = MemoryStorage()
    make_sampler(storage).run_plan(ScriptedAdapter([KeyError("usage"), "ok"]), [P1])
    failure = storage.runs[0]
    assert "也可能是适配器自己的代码出错" in failure.error.message and "出错位置" in failure.error.message
    evidence = failure.raw_payload["contract_violation"]
    assert evidence["exception"] == "KeyError" and evidence["at"].startswith("test_scheduler.py:")


# ── 任务页「边跑边重估」的两个按钮(用户 2026-11-18 定):叫停、上限当场改 ──────────────


def test_a_user_stop_ends_the_plan_before_the_next_ask_and_keeps_what_was_asked():
    storage = MemoryStorage()
    asked: list[int] = []

    def stop() -> bool:  # 每次提问前看一眼:问过 2 次后用户点了「现在停下」
        return len(storage.runs) >= 2

    class Counting(ScriptedAdapter):
        def ask(self, request: AskRequest) -> SurfaceRun:
            asked.append(request.run_index)
            return super().ask(request)

    result = make_sampler(storage).run_plan(Counting(["ok"]), [P1, P2], stop_requested=stop)
    assert result.stop_reason is StopReason.USER_STOPPED
    assert result.n_attempts == 2 == len(storage.runs) == len(asked)  # 第 3 次没有发出去;已问的两条照常落库、进口径
    assert result.accounting.n_valid == 2 and result.total_cost_usd == pytest.approx(0.01)
    from engine.scheduler import StopDetail

    assert StopDetail.from_result(result).truncated  # 提前停,报告要说清
    # 只重算(重新生成报告)时不记成用户叫停:结构上本来就不提问
    replay = make_sampler(MemoryStorage()).run_plan(ScriptedAdapter(["ok"]), [P1], allow_new_calls=False,
                                                   stop_requested=lambda: True)
    assert replay.stop_reason is StopReason.NOT_RESAMPLED


def test_the_cap_is_read_before_every_ask_so_raising_it_midway_lets_the_plan_go_on():
    storage = MemoryStorage()
    cap = {"usd": 0.01}  # 每次 $0.005:原上限问 2 次就到

    def live() -> float:
        if len(storage.runs) == 2:  # 用户在任务页把上限提高了
            cap["usd"] = 1.0
        return cap["usd"]

    result = make_sampler(storage).run_plan(ScriptedAdapter(["ok"]), [P1, P2], live_budget=live,
                                            policy=SamplingPolicy(budget_usd=0.001))  # 给了现值就不看 policy 里那个
    assert result.stop_reason is StopReason.COMPLETED and result.n_attempts == 6
    fixed = make_sampler(MemoryStorage()).run_plan(ScriptedAdapter(["ok"]), [P1, P2], live_budget=lambda: 0.01)
    assert fixed.stop_reason is StopReason.BUDGET_EXHAUSTED and fixed.n_attempts == 2  # 没人提高:照旧到上限停
    unlimited = make_sampler(MemoryStorage()).run_plan(ScriptedAdapter(["ok"]), [P1], live_budget=lambda: None)
    assert unlimited.stop_reason is StopReason.COMPLETED
