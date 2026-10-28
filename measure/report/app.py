"""L4 Web 层(第 16 章):输入 → 确认 → 任务 → 报告,FastAPI + Jinja2 服务端渲染。本模块只做装配。

v1 单租户自用:无登录,仅本机访问。确认页是采样花钱前的唯一闸门。
路由在 report/routes.py,网页公共部件在 report/web.py。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from adapters.base import SurfaceAdapter
from diagnosis.recommender import EvidenceEntry, load_evidence_registry
from engine.calibration import CalibrationReport, load_calibration
from engine.envfile import load_env_file
from engine.judge import JudgeConfig, load_judges
from engine.loader import build_adapters
from engine.storage import CallStat, Storage, default_dsn
from engine.tiers import TierConfig, load_tiers
from report import runner
from report.errors import ErrorLibrary, load_errors
from report.jobs import JobRegistry
from report.launcher import JobLauncher, JobRunner
from report.routes import router
from report.web import CategoryChecker, WebContext, category_checker, target_namer


class _Unset:
    """「没传」与「明确没有校准结果」要分得开:测试里传 None 表示还没校准,不读本机文件。"""


_UNSET = _Unset()


def create_app(
    *,
    adapters: dict[str, SurfaceAdapter] | None = None,
    call_stats: Callable[[], list[CallStat]] | None = None,  # 预估用的历史统计;None = 查库
    dsn: str | None = None,
    evidence: list[EvidenceEntry] | None = None,
    job_runner: JobRunner | None = None,
    registry: JobRegistry | None = None,  # 启动恢复(v0.4.9)由入口注入;缺省全新登记处
    tiers: TierConfig | None = None,  # 档位配置(S9.1);None = 读 config/tiers.yaml
    judges: JudgeConfig | None = None,  # 高档裁判配置(S9.3);None = 读 config/judges.yaml
    calibration: CalibrationReport | None | _Unset = _UNSET,  # 自动校准结果(S9.4);不给 = 读 config/calibration.yaml
    errors: ErrorLibrary | None = None,  # 错误库(S10.1);None = 读 config/errors.yaml
    category_check: CategoryChecker | None = None,  # 品类自动核对(S11.4);None = 真抓首页 + 真调用,记辅助调用账本
) -> FastAPI:
    if adapters is None:
        load_env_file()
        adapters = build_adapters(os.environ)
    resolved_dsn = dsn or default_dsn()
    resolved_judges = judges or load_judges()

    def measured_call_stats() -> list[CallStat]:
        with Storage(resolved_dsn) as store:
            return store.call_stats()

    app = FastAPI(title="GEO 诊断")

    @app.middleware("http")
    async def reject_cross_site_posts(request: Request,
                                      call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """本机工具没有登录,POST 只认本站页面发来的:浏览器跨站提交必带 Origin 头,和本站来源对不上就拒绝。
        建任务那条 POST 不要任务编号,用户浏览器里打开的任意网页都能替他花钱出题(审核 2026-10-27 黄 19)。"""
        origin = request.headers.get("origin")
        if request.method == "POST" and origin is not None:
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin.lower() != own.lower():
                return PlainTextResponse("这个请求来自别的网站,本工具只接受自己页面发来的操作。\n"
                                         "This request came from another site; only this tool's own pages may act.",
                                         status_code=403)
        return await call_next(request)

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
        tiers=tiers or load_tiers(),
        judges=resolved_judges,
        calibration=load_calibration() if isinstance(calibration, _Unset) else calibration,
        errors=errors or load_errors(),
        category_check=category_check or category_checker(adapters, resolved_judges, resolved_dsn),
    )
    app.include_router(router)
    return app
