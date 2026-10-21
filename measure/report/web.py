"""L4 网页公共部件:模板环境、统一渲染(界面语言与各类标签)、跳转、被测对象显示名、各路由共用的依赖。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from adapters.base import SurfaceAdapter
from diagnosis.access_audit import AccessLayer
from diagnosis.messages import msg, render_msg
from engine.calibration import CalibrationReport
from engine.judge import JudgeConfig
from engine.storage import CallStat
from engine.tiers import TierConfig
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
from report.jobs import JobRegistry
from report.launcher import JobLauncher
from report.narrative import api_line_text, count_models, verdict_text, wrap_up_text

TargetNamer = Callable[[str, str], str]


def target_namer(adapters: dict[str, SurfaceAdapter]) -> TargetNamer:
    api_names = {name: adapter.meta().api_name for name, adapter in adapters.items()}

    def target_name(surface: str, model: str) -> str:
        """被测对象(仪器 × 型号)的显示名:「接口 · 型号」(产品文档 P4,用户定;S8.1)——接口名由适配器 meta 声明,
        型号永远写出来(型号是口径的一部分);连仪器都没加载(历史报告里的旧仪器)才如实用内部名,不猜。"""
        return f"{api_names.get(surface, surface)} · {model}"

    return target_name


def utc_time(moment: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """时刻一律按 UTC 显示(S8.3):库里回来的时间带本机时区,直接 strftime 会显示成本机时间。"""
    return moment.astimezone(UTC).strftime(fmt)


def _templates(target_name: TargetNamer) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    env = templates.env
    env.filters["msg"] = render_msg  # 结论码 → 当前界面语言的成句
    env.globals["access_layers"] = list(AccessLayer)  # 报告按证据层分组(v0.4.13 条目 6)
    env.globals["layer_msg"] = lambda layer: msg(f"access.layer.{layer.value}")
    # 用户视图(v0.4.14):人话渲染器 + 阶段人话标签;技术目录只在「检测依据与方法」
    env.filters["plain"] = lambda message, lang: render_msg(message, lang, "plain")
    env.filters["utc_time"] = utc_time  # 时刻按 UTC 显示(S8.3)
    env.globals["access_headline"] = lambda overall: msg(f"access.headline.{overall.value.lower()}")
    env.globals["target_name"] = target_name
    # S8.7:人话段落(各 AI 一句话 / 型号数数 / 末尾汇总 / 每个型号一句结论)在 report/narrative.py 拼
    env.globals["api_line_text"] = api_line_text
    env.globals["count_models"] = count_models
    env.globals["wrap_up_text"] = wrap_up_text
    env.globals["verdict_text"] = verdict_text
    return templates


@dataclass
class WebContext:
    """各路由共用的依赖(create_app 装配,挂在 app.state.ctx)。"""

    adapters: dict[str, SurfaceAdapter]
    dsn: str
    call_stats: Callable[[], list[CallStat]]  # 预估用的历史统计
    registry: JobRegistry
    launcher: JobLauncher
    target_name: TargetNamer
    tiers: TierConfig  # 档位配置(S9.1,config/tiers.yaml)
    judges: JudgeConfig  # 高档裁判配置(S9.3,config/judges.yaml):预估按它算裁判费用
    calibration: CalibrationReport | None  # 自动校准结果(S9.4,config/calibration.yaml);None = 还没跑过

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
    """错误码(err_ 开头)→ 当前界面语言的一句人话;未知码或缺参数就不显示——错误码可能来自地址栏,
    不把任意文字回显到页面上,也不因缺参数出错。"""
    template = MESSAGES[lang_of(request)].get(code) if code.startswith("err_") else None
    if template is None:
        return ""
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return ""


def redirect(request: Request, path: str) -> RedirectResponse:
    joiner = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{joiner}lang={lang_of(request)}", status_code=303)


def not_found() -> HTMLResponse:
    return HTMLResponse("任务不存在", status_code=404)
