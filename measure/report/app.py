"""L4 Web 层(第 16 章):输入 → 确认 → 任务 → 报告,FastAPI + Jinja2 服务端渲染。本模块只做装配。

v1 单租户自用:无登录,仅本机访问。确认页是采样花钱前的唯一闸门。
路由在 report/routes.py,网页公共部件在 report/web.py。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from adapters.base import SurfaceAdapter
from diagnosis.recommender import EvidenceEntry, load_evidence_registry
from engine.calibration import CalibrationReport, load_calibration
from engine.envfile import load_env_file
from engine.hard_cases import calibration_prompt_ids
from engine.judge import JudgeConfig, load_judges
from engine.loader import build_adapters
from engine.storage import STORAGE_DOWN_ERRORS, CallStat, Storage, default_dsn
from engine.tiers import TierConfig, load_tiers
from report import runner
from report.errors import ErrorLibrary, load_errors
from report.jobs import JobRegistry
from report.launcher import JobLauncher, JobRunner
from report.routes import router
from report.web import (
    CategoryChecker,
    NotFound,
    WebContext,
    category_checker,
    ctx_of,
    error_text,
    not_found,
    target_namer,
)


class _Unset:
    """「没传」与「明确没有校准结果」要分得开:测试里传 None 表示还没校准,不读本机文件。"""


_UNSET = _Unset()
log = structlog.get_logger("report.app")

# 只认这两个主机名(本机工具,入口只绑 127.0.0.1;第二轮审核 24)。测试的 TestClient 发的是 testserver,
# 由 tests/unit/conftest.py 在测试期间补进来,正式运行不认。
ALLOWED_HOSTS: Final = ("127.0.0.1", "localhost")


def create_app(
    *,
    adapters: dict[str, SurfaceAdapter] | None = None,
    call_stats: Callable[[str | None], list[CallStat]] | None = None,  # 预估用的历史统计(全库 / 某品牌);None = 查库
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

    def measured_call_stats(brand: str | None) -> list[CallStat]:
        with Storage(resolved_dsn) as store:
            return store.call_stats(store.brand_prompt_ids(brand) if brand is not None else None,
                                    exclude_prompt_ids=calibration_prompt_ids())  # 校准题不当实测(用户 2026-11-20 定)

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

    @app.middleware("http")
    async def reject_foreign_hosts(request: Request,
                                   call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """DNS 重绑定:别的网站把自己的域名解析到 127.0.0.1,浏览器就把那个域名当主机名发来,来源头也是它——上面的跨站检查
        看来源与主机名一致就放行了(第二轮审核 24)。主机名只认本机的两个名字,别的一律拒绝;放在最外层,先于跨站检查。"""
        host = (urlsplit("//" + request.headers.get("host", "")).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return PlainTextResponse("这个网址不是本工具的地址。"
                                     "请在本机用 http://127.0.0.1 或 http://localhost 打开。\n"
                                     "This address is not this tool's. "
                                     "Open it on this computer via http://127.0.0.1 or http://localhost.",
                                     status_code=400)
        return await call_next(request)

    @app.middleware("http")
    async def forbid_framing(request: Request,
                             call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """不许别的网页把本工具的页面套进框里(点击劫持):透明的框骗用户点一下「接着跑」,那次点击是从本站页面发出的,
        上面的跨站检查拦不住,会用用户的 key 花钱(第四轮审核)。最后注册 = 最外层,连上面两条拒绝的回应也带上。"""
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

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
    async def storage_down(request: Request, exc: Exception) -> Response:
        """库连不上或查到一半断了:给人话页 + 下一步(WEB_DB_UNREACHABLE;第二轮审核 B13,原来是裸 500)。
        出题那条路自己接(撤掉草稿回首页),这里接其余所有页面:确认页查单价、单条回答页、下载结果……"""
        log.error("web_db_unreachable", path=request.url.path, error=f"{type(exc).__name__}: {exc}")
        page = ctx_of(request).render(request, "unavailable.html",
                                      message=error_text(request, "WEB_DB_UNREACHABLE", detail=str(exc)[:200]))
        page.status_code = 503
        return page

    for error_type in STORAGE_DOWN_ERRORS:
        app.add_exception_handler(error_type, storage_down)

    async def missing(request: Request, exc: Exception) -> Response:
        """任务或回答不存在(第三轮审核 B3:路由统一抛 NotFound,这里统一给 404 页)。"""
        return not_found()

    app.add_exception_handler(NotFound, missing)
    app.include_router(router)
    return app
