"""辅助 AI 调用的唯一入口(engine/ai_task.py):重试与采样同一套、不合格重问有上限、
账户问题立即停、全部尝试随结果返回供记账。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.base import (
    AdapterAccountError,
    AdapterTransientError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from engine.ai_task import MAX_ROUNDS, TaskFailed, run_task
from engine.models import Answer, Outcome, RunErrorType, SurfaceRun
from engine.scheduler import AccountProblem


class ScriptedAdapter(SurfaceAdapter):
    """按脚本逐次返回:字符串 = 成功回答(花费 0.01),异常 = 抛出。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="T", api_name="T", provider="test_provider", surface="t", models=["m"],
                           default_model="m", capabilities=CapabilityRegistry(supports_citations=True),
                           consumer_default_model="m", strongest_model="m", models_checked="test",
                           est_cost_per_call_usd={"m": 0.01})

    def ask(self, request):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id,
            run_index=request.run_index, provider="test_provider", surface="t", model="m",
            search_mode=request.search_mode, locale=request.locale, outcome=Outcome.SUCCESS,
            answer=Answer(answer_text=step, citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.01,
        )

    def healthcheck(self) -> bool:
        return True


def _digit(text: str) -> int:
    return int(text)  # 不是数字 → ValueError → 这一轮不合格


def _run(adapter, ledger=None):
    return run_task(adapter, "说一个数字", _digit, label="测试任务", prompt_id="n", locale="zh-CN",
                    ledger=(ledger if ledger is not None else []).append, sleep=lambda s: None)


def test_transient_error_retried_with_scheduler_policy_then_succeeds():
    adapter = ScriptedAdapter([AdapterTransientError("限流", http_status=429), "7"])
    result = _run(adapter)
    assert result.value == 7 and adapter.calls == 2
    assert len(result.runs) == 1  # Transient 重试在一轮之内,不另起一轮


def test_unusable_answer_is_asked_again_and_all_rounds_are_accounted():
    adapter = ScriptedAdapter(["不是数字", "8"])
    ledger: list = []
    result = _run(adapter, ledger)
    assert result.value == 8
    assert len(result.runs) == 2 and result.cost_usd == pytest.approx(0.02)  # 不合格那轮的钱也记上
    assert ledger == result.runs  # 每轮当场记账


def test_account_problem_stops_immediately_without_asking_again():
    adapter = ScriptedAdapter([AdapterAccountError("余额不足", error_type=RunErrorType.ACCOUNT_CREDIT,
                                                   http_status=402), "9"])
    with pytest.raises(AccountProblem):
        _run(adapter)
    assert adapter.calls == 1


def test_fails_loudly_after_max_rounds():
    adapter = ScriptedAdapter(["甲"] * MAX_ROUNDS)
    ledger: list = []
    with pytest.raises(TaskFailed, match="测试任务失败:3 次尝试仍未产出合格结果"):
        _run(adapter, ledger)
    assert adapter.calls == MAX_ROUNDS
    assert len(ledger) == MAX_ROUNDS  # 事情没办成,花掉的钱也都记了账
