"""L2 辅助 AI 调用:「让 AI 办件事」(出题、改写问法;以后的 AI 裁判)的唯一入口。

- 调用走适配器(L1),和采样同一套有界重试(scheduler.ask_with_retries,重试口径归调度器);
- 回答交给调用方的解析 / 校验函数;没成功、解析不了、校验不过都算这一轮不合格,重问,有上限;
- 账户问题(认证 / 额度)不重问,直接抛 AccountProblem——与采样一致:停下、说人话;
- 每一次尝试(含不合格与失败)立刻交给调用方给的记账函数(ledger,必填)——钱花出去就有记录,
  哪怕这件事最后没办成;全部尝试也随结果返回。

辅助调用不是观测采样:不进任何指标分母。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from adapters.base import SurfaceAdapter
from engine.models import AskRequest, SearchMode, SurfaceRun
from engine.scheduler import AccountProblem, SamplingPolicy, ask_with_retries, is_account_error
from engine.validity import is_valid_answer

MAX_ROUNDS = 3  # 一件事最多问几轮(每轮内部另有 Transient 重试)

Ledger = Callable[[SurfaceRun], None]  # 记账:每次尝试一调用(如写进 aux_call 账本)


class TaskFailed(ValueError):
    """问满轮数仍没拿到合格结果:显式报错,不静默降级。"""


@dataclass(frozen=True)
class TaskResult[T]:
    value: T
    runs: list[SurfaceRun]  # 全部尝试,按先后

    @property
    def cost_usd(self) -> float:
        return sum(run.cost_usd for run in self.runs)


def run_task[T](
    adapter: SurfaceAdapter,
    prompt: str,
    parse: Callable[[str], T],
    *,
    label: str,
    prompt_id: str,
    locale: str,
    ledger: Ledger,
    search_mode: SearchMode = SearchMode.DEFAULT,
    policy: SamplingPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TaskResult[T]:
    """label 是这件事的人话名字(如「变体生成」),用于调用编号与报错。"""
    policy = policy or SamplingPolicy()
    runs: list[SurfaceRun] = []
    last: Exception | None = None
    for _ in range(MAX_ROUNDS):
        request = AskRequest(
            run_id=f"task-{uuid4().hex[:12]}",
            prompt_id=prompt_id,
            run_index=len(runs) + 1,  # 第几轮(不合格重问)
            prompt_text=prompt,
            locale=locale,
            search_mode=search_mode,
            timeout_s=policy.timeout_s,
        )
        run = ask_with_retries(adapter, request, policy, sleep=sleep)
        ledger(run)
        runs.append(run)
        if is_account_error(run):
            assert run.error is not None
            raise AccountProblem(provider=run.provider, surface=run.surface, model=run.model,
                                 error_type=run.error.error_type, message=run.error.message)
        if not is_valid_answer(run):  # 失败 / 拒答 / 空回答都算没办成,重问(有效口径全项目一处定)
            last = ValueError(f"调用未成功:outcome={run.outcome.value}")
            continue
        assert run.answer is not None
        try:
            return TaskResult(value=parse(run.answer.answer_text), runs=runs)
        except (ValueError, KeyError, TypeError) as exc:
            last = exc
    raise TaskFailed(f"{label}失败:{MAX_ROUNDS} 次尝试仍未产出合格结果:{last}")


def extract_json(text: str) -> Any:
    """容忍模型偶尔加围栏 / 前后语:抠出首个 {...} 对象解析。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("回答中未含 JSON 对象")
    return json.loads(text[start : end + 1])
