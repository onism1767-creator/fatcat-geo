"""L4 Web 层(第 16 章):输入 → 确认 → 任务 → 报告,FastAPI + Jinja2 服务端渲染。本模块只做装配。

v1 单租户自用:无登录,仅本机访问。确认页是采样花钱前的唯一闸门。
路由在 report/routes.py,网页公共部件在 report/web.py。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from fastapi import FastAPI

from adapters.base import SurfaceAdapter
from diagnosis.recommender import EvidenceEntry, load_evidence_registry
from engine.envfile import load_env_file
from engine.loader import build_adapters
from engine.storage import CallStat, Storage
from report import runner
from report.jobs import JobRegistry
from report.launcher import JobLauncher, JobRunner
from report.routes import router
from report.web import WebContext, target_namer

DEFAULT_DSN = "postgresql://geo@localhost:5433/geo_tool"


def create_app(
    *,
    adapters: dict[str, SurfaceAdapter] | None = None,
    call_stats: Callable[[], list[CallStat]] | None = None,  # 预估用的历史统计;None = 查库
    dsn: str | None = None,
    evidence: list[EvidenceEntry] | None = None,
    job_runner: JobRunner | None = None,
    registry: JobRegistry | None = None,  # 启动恢复(v0.4.9)由入口注入;缺省全新登记处
) -> FastAPI:
    if adapters is None:
        load_env_file()
        adapters = build_adapters(os.environ)
    resolved_dsn = dsn or os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)

    def measured_call_stats() -> list[CallStat]:
        with Storage(resolved_dsn) as store:
            return store.call_stats()

    app = FastAPI(title="GEO 诊断")
    app.state.ctx = WebContext(
        adapters=adapters,
        dsn=resolved_dsn,
        call_stats=call_stats or measured_call_stats,
        registry=registry or JobRegistry(),
        launcher=JobLauncher(
            job_runner or runner.execute_job, adapters=adapters, dsn=resolved_dsn,
            evidence=evidence if evidence is not None else load_evidence_registry(),
        ),
        target_name=target_namer(adapters),
    )
    app.include_router(router)
    return app
