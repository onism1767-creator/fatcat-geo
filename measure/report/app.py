"""L4 Web 层(第 16 章):输入 → 确认 → 任务 → 报告,FastAPI + Jinja2 服务端渲染。

v1 单租户自用:无登录,仅本机访问。确认页是采样花钱前的唯一闸门。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from diagnosis.access_audit import AccessLayer
from diagnosis.findings import Stage
from diagnosis.messages import msg, render_msg
from diagnosis.recommender import EvidenceEntry, load_evidence_registry
from engine.envfile import load_env_file
from engine.loader import build_adapters
from engine.models import BrandSpec, InformationNeed, Target
from engine.needs import NeedCandidate, draft_need_candidates
from engine.storage import CallStat, Storage
from report import runner
from report.estimate import estimate_job, unit_prices
from report.i18n import (
    ACCESS_LABELS,
    DEP_LABELS,
    INTENT_LABELS,
    JOB_STATE_LABELS,
    LEVEL_LABELS,
    MESSAGES,
    STATUS_LABELS,
    TIER_TABLE,
    resolve_lang,
)
from report.jobs import (
    TIERS,
    DiagnosisJob,
    JobRegistry,
    JobState,
    manual_intent,
    preselect_candidates,
)
from report.summary import summarize

DEFAULT_DSN = "postgresql://geo@localhost:5433/geo_tool"


# 多少分钟没有任何新样本就在任务页报 JOB_STALLED(v0.4.22):一次采样最坏 = 3 次尝试 × 180 s 超时 + 退避 ≈ 10 分钟
STALL_MINUTES = 10


def create_app(
    *,
    adapters: dict | None = None,
    call_stats: Callable[[], list[CallStat]] | None = None,  # 预估用的历史统计;None = 查库
    dsn: str | None = None,
    evidence: list[EvidenceEntry] | None = None,
    job_runner=None,
    registry: JobRegistry | None = None,  # 启动恢复(v0.4.9)由入口注入;缺省全新登记处
) -> FastAPI:
    if adapters is None:
        load_env_file()
        adapters = build_adapters(os.environ)
    dsn = dsn or os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)

    def measured_call_stats() -> list[CallStat]:
        with Storage(dsn) as store:
            return store.call_stats()

    stats_provider = call_stats or measured_call_stats
    evidence = evidence if evidence is not None else load_evidence_registry()
    job_runner = job_runner or runner.execute_job

    app = FastAPI(title="GEO 诊断")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["msg"] = render_msg  # 结论码 → 当前界面语言的成句
    templates.env.globals["access_layers"] = list(AccessLayer)  # 报告按证据层分组(v0.4.13 条目 6)
    templates.env.globals["layer_msg"] = lambda layer: msg(f"access.layer.{layer.value}")
    # 用户视图(v0.4.14):人话渲染器 + 阶段人话标签;技术目录只在「检测依据与方法」
    templates.env.filters["plain"] = lambda message, lang: render_msg(message, lang, "plain")
    templates.env.globals["funnel_stages"] = [Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION]
    templates.env.globals["stage_msg"] = lambda stage: msg(f"stage.{stage.value}")
    templates.env.globals["access_headline"] = lambda overall: msg(f"access.headline.{overall.value.lower()}")
    display_names = {name: adapter.meta().display_name for name, adapter in adapters.items()}
    models_of = {name: adapter.meta().models for name, adapter in adapters.items()}

    def target_name(surface: str, model: str) -> str:
        """被测对象(仪器 × 型号,v0.4.19)的显示名:只有一个型号的仪器直接用产品名,否则「产品名 · 型号」。
        型号已移出清单(历史报告里还有)照样「产品名 · 型号」;连仪器都没加载才如实用内部名,不猜。"""
        if surface not in display_names:
            return f"{surface} · {model}"
        if models_of[surface] == [model]:
            return display_names[surface]
        return f"{display_names[surface]} · {model}"

    templates.env.globals["target_name"] = target_name
    registry = registry or JobRegistry()
    app.state.registry = registry
    app.state.adapters = adapters

    def render(request: Request, name: str, **context) -> HTMLResponse:
        lang = resolve_lang(request.query_params.get("lang"))
        context.setdefault("lang", lang)
        context.setdefault("t", MESSAGES[lang])
        context.setdefault("intent_labels", INTENT_LABELS[lang])
        context.setdefault("status_labels", STATUS_LABELS[lang])
        context.setdefault("access_labels", ACCESS_LABELS[lang])
        context.setdefault("dep_labels", DEP_LABELS[lang])
        context.setdefault("level_labels", LEVEL_LABELS[lang])
        context.setdefault("state_labels", JOB_STATE_LABELS[lang])
        context.setdefault("display_names", display_names)  # AI 产品名(适配器 meta 声明)
        return templates.TemplateResponse(request, name, context)

    def selected_targets(keys: list[str]) -> list[Target]:
        """表单勾选的被测对象,只认当前清单里真实存在的(同序)。"""
        return [
            target for target in map(Target.parse, keys)
            if target.surface in adapters and target.model in adapters[target.surface].meta().models
        ]

    def launch(job: DiagnosisJob) -> None:
        """起执行器前先同步把任务标成「排队中」:查状态与改状态之间不让出控制权,连点两下也不会让
        同一单同时有两个执行器(不靠执行器线程抢先改状态)。确认页与「接着跑」都只走这里。"""
        job.state = JobState.CONFIRMED
        threading.Thread(
            target=job_runner,
            kwargs=dict(job=job, adapters=adapters, dsn=dsn, evidence=evidence),
            daemon=True,
        ).start()

    def redirect(request: Request, path: str) -> RedirectResponse:
        lang = resolve_lang(request.query_params.get("lang"))
        joiner = "&" if "?" in path else "?"
        return RedirectResponse(f"{path}{joiner}lang={lang}", status_code=303)

    def not_found(request: Request) -> HTMLResponse:
        return HTMLResponse("任务不存在", status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, error: str = ""):
        return render(request, "index.html", jobs=registry.recent(),
                      surfaces=sorted(adapters), error=error)

    @app.post("/jobs")
    async def create_job(
        request: Request,
        brand_name: str = Form(...),
        aliases: str = Form(""),
        website: str = Form(...),
        category: str = Form(...),
        locale: str = Form("zh-CN"),
        questions: str = Form(""),
        generate: str = Form(""),
    ):
        locale = locale if locale in ("zh-CN", "en-US") else "zh-CN"
        normalized = website if "://" in website else f"https://{website}"
        domain = urlsplit(normalized).hostname or ""
        brand = BrandSpec(
            name=brand_name.strip(),
            aliases=[a.strip() for a in aliases.replace(",", ",").split(",") if a.strip()],
            domains=[domain] if domain else [],
        )
        # 先有任务编号再出题:出题的每次调用当场记进辅助调用账本(哪怕出题失败,花掉的钱也有记录)
        job = DiagnosisJob(brand=brand, website=normalized, category=category, locale=locale,
                           candidates=[], competitors_suggested=[])
        drafted: list[NeedCandidate] = []
        if generate and adapters:
            generation_adapter = next(
                (a for a in adapters.values() if a.meta().capabilities.supports_citations), None
            )
            if generation_adapter is not None:
                with Storage(dsn) as store:
                    drafted, job.competitors_suggested = draft_need_candidates(
                        generation_adapter, category=category, brand=brand, locale=locale,
                        ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task="需求生成", run=run),
                    )
        manual = [line.strip() for line in questions.splitlines() if line.strip()]
        job.candidates = drafted + [
            NeedCandidate(intent=manual_intent(text, brand), text=text) for text in manual
        ]
        if not job.candidates:
            return render(request, "index.html", jobs=registry.recent(),
                          surfaces=sorted(adapters),
                          error="请至少手填一个目标问题,或勾选「帮我生成」")
        job.preselected = preselect_candidates(job.candidates, n_manual=len(manual))
        registry.add(job)
        return redirect(request, f"/jobs/{job.job_id}/confirm")

    @app.get("/jobs/{job_id}/confirm", response_class=HTMLResponse)
    async def confirm_page(request: Request, job_id: str, error: str = ""):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if job.state is not JobState.DRAFT:
            return redirect(request, f"/jobs/{job.job_id}")
        prices = unit_prices(adapters, stats_provider())
        target_groups = [  # 按 AI 分组列出全部型号(默认全勾)
            (display_names[name], [Target(surface=name, model=model) for model in adapters[name].meta().models])
            for name in sorted(adapters)
        ]
        lang = resolve_lang(request.query_params.get("lang"))
        basis = []
        if prices.measured:
            basis.append(MESSAGES[lang]["est_basis_measured"].format(
                names="、".join(target_name(t.surface, t.model) for t in prices.measured)))
        if prices.declared:
            basis.append(MESSAGES[lang]["est_basis_declared"].format(
                names="、".join(target_name(t.surface, t.model) for t in prices.declared)))
        basis.append(MESSAGES[lang]["est_basis_tail"])
        return render(request, "confirm.html", job=job, tiers=TIERS, tier_table=TIER_TABLE[lang],
                      target_groups=target_groups, est_basis=" ".join(basis), error=error)

    @app.post("/jobs/{job_id}/estimate")
    async def confirm_estimate(
        job_id: str,
        needs_selected: list[int] = Form([]),
        custom: str = Form(""),
        targets_selected: list[str] = Form([]),
        tier: str = Form("quick"),
        variants: int = Form(3),
        dependency: str = Form(""),
    ):
        """确认页的实时预估:页面每次勾选变化都把整张表单发来,算好的数字发回去,页面只负责显示。"""
        job = registry.get(job_id)
        if job is None or tier not in TIERS:
            return JSONResponse({"error": "not_found"}, status_code=404)
        n_needs = sum(0 <= i < len(job.candidates) for i in needs_selected) + len(
            [line for line in custom.splitlines() if line.strip()])
        estimate = estimate_job(
            n_needs=n_needs, variants_per_need=variants, targets=selected_targets(targets_selected),
            tier_id=tier, dependency=bool(dependency), prices=unit_prices(adapters, stats_provider()),
        )
        return JSONResponse({
            "calls": estimate.calls,
            "cost_usd": estimate.cost_usd,
            "minutes": estimate.minutes,
            "breakdown": [{"display": target_name(t.surface, t.model), "cost_usd": cost}
                          for t, cost in estimate.per_target],
        })

    @app.post("/jobs/{job_id}/confirm")
    async def confirm(
        request: Request,
        job_id: str,
        needs_selected: list[int] = Form([]),
        custom: str = Form(""),
        competitors_selected: list[int] = Form([]),
        targets_selected: list[str] = Form([]),
        tier: str = Form("quick"),
        variants: int = Form(3),
        budget: str = Form(""),
        dependency: str = Form(""),
    ):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if job.state is not JobState.DRAFT:
            return redirect(request, f"/jobs/{job.job_id}")

        needs = [
            InformationNeed(statement=job.candidates[i].text, intent=job.candidates[i].intent)
            for i in needs_selected
            if 0 <= i < len(job.candidates)
        ] + [
            InformationNeed(statement=line.strip(), intent=manual_intent(line, job.brand))
            for line in custom.splitlines()
            if line.strip()
        ]

        def back(error: str):
            return redirect(request, f"/jobs/{job.job_id}/confirm?error={error}")

        if not 1 <= len(needs) <= 10:
            return back("需求数量须在 1–10 个之间(架构口径建议 3–10)")
        targets = selected_targets(targets_selected)
        if not targets:
            return back("至少选一个要测的 AI 型号")
        if tier not in TIERS or not 1 <= variants <= 5:
            return back("参数非法")
        budget_usd = float(budget) if budget.strip() else None

        competitors = [
            BrandSpec(name=c.name, domains=[c.domain])
            for i in competitors_selected
            if 0 <= i < len(job.competitors_suggested)
            for c in [job.competitors_suggested[i]]
        ]
        job.freeze(needs=needs, competitors=competitors, targets=targets,
                   tier_id=tier, variants_per_need=variants, budget_usd=budget_usd,
                   dependency_enabled=bool(dependency))
        launch(job)
        return redirect(request, f"/jobs/{job.job_id}")

    @app.post("/jobs/{job_id}/resume")
    async def resume(request: Request, job_id: str):
        """幂等续跑(v0.4.9):中断/失败任务补缺口;已完成任务重新生成报告
        (v0.4.16:追加一版报告快照,旧版本留档)。执行器可重入,已存样本按幂等键
        复用——已花的钱不重花。"""
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if not (job.state in (JobState.INTERRUPTED, JobState.FAILED) or job.finished):
            return redirect(request, f"/jobs/{job.job_id}")
        missing = sorted({t.surface for t in job.targets if t.surface not in adapters})
        if missing:
            return redirect(request, f"/?error=有的 AI 没接入(缺 API key),不能接着跑:{', '.join(missing)}")
        launch(job)
        return redirect(request, f"/jobs/{job.job_id}")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_status(request: Request, job_id: str):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        # 运行中按实际速度算剩余时间(v0.4.17):已用时 ÷ 已完成计划 × 未完成计划
        eta_minutes = elapsed_minutes = None
        if job.state is JobState.RUNNING and job.started_at is not None:
            elapsed_minutes = (datetime.now(UTC) - job.started_at).total_seconds() / 60
            if job.completed_plans and job.total_plans:
                eta_minutes = elapsed_minutes / job.completed_plans * (job.total_plans - job.completed_plans)
        lang = resolve_lang(request.query_params.get("lang"))
        error_text = (  # 有代码的出错说明按当前界面语言组句;没代码的(意外异常)照原文
            MESSAGES[lang][job.error_code].format(**job.error_params) if job.error_code else job.error
        )
        # 卡住了没有?(v0.4.22)运行中按「最近一条样本 / 本次开始」到现在的分钟数报;超过阈值给 JOB_STALLED
        quiet_minutes = stalled_text = None
        if job.state is JobState.RUNNING and job.started_at is not None:
            since = job.last_result_at or job.started_at
            quiet_minutes = (datetime.now(UTC) - since).total_seconds() / 60
            if quiet_minutes >= STALL_MINUTES:
                stalled_text = MESSAGES[lang]["job_stalled"].format(minutes=round(quiet_minutes))
        return render(request, "job.html", job=job, elapsed_minutes=elapsed_minutes, eta_minutes=eta_minutes,
                      error_text=error_text, quiet_minutes=quiet_minutes, stalled_text=stalled_text)

    @app.get("/jobs/{job_id}/report", response_class=HTMLResponse)
    async def job_report(request: Request, job_id: str):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if not job.finished:
            return redirect(request, f"/jobs/{job.job_id}")
        return render(
            request, "report.html", job=job,
            summary=summarize(job.need_reports, job.surface_verdicts, target_name),
        )

    return app
