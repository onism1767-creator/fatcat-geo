"""L4 起执行器:确认页与「接着跑 / 重新生成报告」都只走这里。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from adapters.base import SurfaceAdapter
from diagnosis.recommender import EvidenceEntry
from report.jobs import DiagnosisJob, JobState

# 与 report.runner.execute_job 同签名(关键字参数 job / adapters / dsn / evidence / replay)
JobRunner = Callable[..., None]


class JobLauncher:
    def __init__(
        self, run: JobRunner, *, adapters: dict[str, SurfaceAdapter], dsn: str, evidence: list[EvidenceEntry],
    ) -> None:
        self._run, self._adapters, self._dsn, self._evidence = run, adapters, dsn, evidence

    def launch(self, job: DiagnosisJob, *, replay: bool) -> None:
        """起执行器前先同步把任务标成「排队中」。调用方必须在 job.lock 里做「查状态 → launch」:路由跑在线程池里,
        不在一把锁里做完,连点两下会让同一单同时有两个执行器、花双份钱(第二轮)。replay=True = 只重算、不向 AI 提问。"""
        job.state = JobState.CONFIRMED
        threading.Thread(
            target=self._run,
            kwargs=dict(job=job, adapters=self._adapters, dsn=self._dsn, evidence=self._evidence, replay=replay),
            daemon=True,
        ).start()
