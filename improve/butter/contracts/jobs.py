"""后台作业的数据形状(设计 §5.2、§10.2)。job/1。状态用 Toast 的七态。"""

from __future__ import annotations

from typing import Any, Literal

from butter.contracts.common import Frozen

JobState = Literal["DRAFT", "CONFIRMED", "RUNNING", "DONE", "PARTIAL", "FAILED", "INTERRUPTED"]
JobKind = Literal["run"]  # 抓站 + 出包 + 建批次与卡;测量是另一条路(测量驱动,设计 §10.2)


class JobEvent(Frozen):
    """作业状态的一次变化。只追加;作业的当前样子由这些事件推出来。"""

    job_id: str
    at: str                       # ISO 时间
    kind: JobKind = "run"
    state: JobState
    progress: str = ""            # 给站长看的一句话:现在做到哪了
    params: dict[str, Any] = {}   # 只在第一条里:网址、最多几页、起不起草(没有密钥)
    result: dict[str, Any] = {}   # 做完时:运行号、批次号、花费
    error: str = ""


class Job(Frozen):
    job_id: str
    kind: JobKind = "run"
    state: JobState
    progress: str = ""
    created_at: str
    updated_at: str
    params: dict[str, Any] = {}
    result: dict[str, Any] = {}
    error: str = ""

    @property
    def active(self) -> bool:
        return self.state in ("CONFIRMED", "RUNNING")
