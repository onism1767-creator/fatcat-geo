"""L4 网页公共部件:模板环境、统一渲染(界面语言与各类标签)、跳转、被测对象显示名、各路由共用的依赖。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from adapters.base import SurfaceAdapter
from diagnosis.access_audit import AccessLayer
from diagnosis.messages import msg, render_msg
from engine.calibration import CalibrationReport
from engine.category_check import TASK_LABEL, CategoryCheck, helper_model, run_category_check
from engine.judge import JudgeConfig
from engine.storage import CallStat, Storage, StorageUnavailable
from engine.tiers import TierConfig
from report.clock import local_date, local_time
from report.errors import ErrorLibrary
from report.i18n import (
    ACCESS_LABELS,
    DEP_LABELS,
    INTENT_LABELS,
    JOB_STATE_LABELS,
    LEVEL_LABELS,
    MESSAGES,
    STATUS_LABELS,
    resolve_lang,
)
from report.jobs import DiagnosisJob, JobRegistry
from report.launcher import JobLauncher
from report.narrative import action_text, api_line_text, count_models, verdict_text, wrap_up_text

log = structlog.get_logger("report.web")

TargetNamer = Callable[[str, str], str]
CategoryChecker = Callable[[DiagnosisJob], CategoryCheck | None]  # 品类自动核对(S11.4);None = 没核对出来,照常往下走


def category_checker(adapters: dict[str, SurfaceAdapter], judges: JudgeConfig, dsn: str) -> CategoryChecker:
    """提交后、出题前的品类自动核对:先挑仪器(免费;没有能用的就不核对,也不碰库),花掉的钱记进本单的辅助调用账本;
    连不上库就不核对(钱没处记就不花)。哪一环没成都记一行日志。"""

    def check(job: DiagnosisJob) -> CategoryCheck | None:
        picked = helper_model(judges, adapters)
        if picked is None:
            log.info("category_check_skipped", reason="no_ai", job_id=job.job_id)
            return None
        adapter, spec = picked
        try:
            with Storage(dsn) as store:
                return run_category_check(
                    adapter, spec.model, website=job.website, category=job.category, locale=job.locale,
                    ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task=TASK_LABEL, run=run),
                )
        except StorageUnavailable as exc:
            log.warning("category_check_skipped", reason="db_unreachable", job_id=job.job_id, error=str(exc))
            return None

    return check


def target_namer(adapters: dict[str, SurfaceAdapter]) -> TargetNamer:
    api_names = {name: adapter.meta().api_name for name, adapter in adapters.items()}

    def target_name(surface: str, model: str) -> str:
        """被测对象(仪器 × 型号)的显示名:「接口 · 型号」(产品文档 P4,用户定;S8.1)——接口名由适配器 meta 声明,
        型号永远写出来(型号是口径的一部分);连仪器都没加载(历史报告里的旧仪器)才如实用内部名,不猜。"""
        return f"{api_names.get(surface, surface)} · {model}"

    return target_name


def _templates(target_name: TargetNamer) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    env = templates.env
    env.filters["msg"] = render_msg  # 结论码 → 当前界面语言的成句
    env.globals["access_layers"] = list(AccessLayer)  # 报告按证据层分组(v0.4.13 条目 6)
    env.globals["layer_msg"] = lambda layer: msg(f"access.layer.{layer.value}")
    # 用户视图(v0.4.14):人话渲染器 + 阶段人话标签;技术目录只在「检测依据与方法」
    env.filters["plain"] = lambda message, lang: render_msg(message, lang, "plain")
    env.filters["local_time"] = local_time  # 时刻一律当地时间 + 时区标记(用户 2026-10-29 定)
    env.filters["local_date"] = local_date
    env.globals["access_headline"] = lambda overall: msg(f"access.headline.{overall.value.lower()}")
    env.globals["target_name"] = target_name
    env.globals["judge_names"] = lambda targets: ";".join(target_name(x.surface, x.model) for x in targets)
    # S8.7:人话段落(各 AI 一句话 / 型号数数 / 末尾汇总 / 每个型号一句结论)在 report/narrative.py 拼
    env.globals["api_line_text"] = api_line_text
    env.globals["count_models"] = count_models
    env.globals["wrap_up_text"] = wrap_up_text
    env.globals["action_text"] = action_text
    env.globals["verdict_text"] = verdict_text
    return templates


@dataclass
class WebContext:
    """各路由共用的依赖(create_app 装配,挂在 app.state.ctx)。"""

    adapters: dict[str, SurfaceAdapter]
    dsn: str
    call_stats: Callable[[str | None], list[CallStat]]  # 预估用的历史统计:None = 全库;给品牌名 = 只看这个品牌的单(D5)
    registry: JobRegistry
    launcher: JobLauncher
    target_name: TargetNamer
    tiers: TierConfig  # 档位配置(S9.1,config/tiers.yaml)
    judges: JudgeConfig  # 高档裁判配置(S9.3,config/judges.yaml):预估按它算裁判费用
    calibration: CalibrationReport | None  # 自动校准结果(S9.4,config/calibration.yaml);None = 还没跑过
    errors: ErrorLibrary  # 错误库(S10.1,config/errors.yaml):全部错误码与文案的唯一出处
    category_check: CategoryChecker  # 品类自动核对(S11.4):create_app 装配,测试注入桩

    def __post_init__(self) -> None:
        self._templates = _templates(self.target_name)
        self.display_names = {name: a.meta().display_name for name, a in self.adapters.items()}
        self.api_names = {name: a.meta().api_name for name, a in self.adapters.items()}

    def api_name(self, surface: str) -> str:
        """接口名(报告里「各 AI 一句话」的主语;没加载的旧仪器如实用内部名)。"""
        return self.api_names.get(surface, surface)

    def render(self, request: Request, name: str, **context) -> HTMLResponse:
        lang = lang_of(request)
        context.setdefault("lang", lang)
        context.setdefault("t", MESSAGES[lang])
        context.setdefault("intent_labels", INTENT_LABELS[lang])
        context.setdefault("status_labels", STATUS_LABELS[lang])
        context.setdefault("access_labels", ACCESS_LABELS[lang])
        context.setdefault("dep_labels", DEP_LABELS[lang])
        context.setdefault("level_labels", LEVEL_LABELS[lang])
        context.setdefault("state_labels", JOB_STATE_LABELS[lang])
        context.setdefault("display_names", self.display_names)  # AI 产品名(适配器 meta 声明)
        return self._templates.TemplateResponse(request, name, context)


def ctx_of(request: Request) -> WebContext:
    return request.app.state.ctx


def lang_of(request: Request) -> str:
    return resolve_lang(request.query_params.get("lang"))


def error_text(request: Request, code: str, **params: str) -> str:
    """错误码 → 当前界面语言的一句人话 + 下一步(文案只在 config/errors.yaml,S10.1);未知码或缺参数就不显示——
    错误码可能来自地址栏,不把任意文字回显到页面上,也不因缺参数出错。"""
    entry = ctx_of(request).errors.get(code) if code else None
    if entry is None:
        return ""
    try:
        return entry.text(lang_of(request), with_next_step=True, **params)
    except (KeyError, IndexError):
        return ""


def redirect(request: Request, path: str) -> RedirectResponse:
    joiner = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{joiner}lang={lang_of(request)}", status_code=303)


def not_found() -> HTMLResponse:
    """找不到任务:中英各一句(界面默认英文;用户 2026-10-28 定)。英文界面把任务叫 check,这里同一个词(第二轮审核 B16)。"""
    return HTMLResponse("Check not found / 任务不存在", status_code=404)
