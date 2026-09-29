"""L4 路由(第 16 章四页:输入 → 确认 → 任务 → 报告)。每个路由只管收发:
表单解析与校验在 report/forms.py,预估在 report/estimate.py,起执行器在 report/launcher.py。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from engine.models import Target
from engine.needs import NeedCandidate, draft_need_candidates
from engine.storage import Storage
from report.estimate import estimate_job, unit_prices
from report.forms import (
    brand_from_form,
    confirm_choice,
    count_needs,
    manual_candidates,
    preselect_candidates,
    selected_targets,
)
from report.i18n import MESSAGES, TIER_TABLE
from report.jobs import STALL_MINUTES, TIERS, DiagnosisJob, JobState
from report.summary import summarize
from report.web import ctx_of, error_text, lang_of, not_found, redirect

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = "", names: str = ""):
    ctx = ctx_of(request)
    return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                      error=error_text(request, error, names=names))


@router.post("/jobs")
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
    ctx = ctx_of(request)
    locale = locale if locale in ("zh-CN", "en-US") else "zh-CN"
    brand, normalized = brand_from_form(brand_name, aliases, website)
    # 先有任务编号再出题:出题的每次调用当场记进辅助调用账本(哪怕出题失败,花掉的钱也有记录)
    job = DiagnosisJob(brand=brand, website=normalized, category=category, locale=locale,
                       candidates=[], competitors_suggested=[])
    drafted: list[NeedCandidate] = []
    generation_adapter = next(
        (a for a in ctx.adapters.values() if a.meta().capabilities.supports_citations), None
    )
    if generate and generation_adapter is not None:
        with Storage(ctx.dsn) as store:
            drafted, job.competitors_suggested = draft_need_candidates(
                generation_adapter, category=category, brand=brand, locale=locale,
                ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task="需求生成", run=run),
            )
    manual = manual_candidates(questions, brand)
    job.candidates = drafted + manual
    if not job.candidates:
        return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                          error=error_text(request, "err_no_question"))
    job.preselected = preselect_candidates(job.candidates, n_manual=len(manual))
    ctx.registry.add(job)
    return redirect(request, f"/jobs/{job.job_id}/confirm")


@router.get("/jobs/{job_id}/confirm", response_class=HTMLResponse)
async def confirm_page(request: Request, job_id: str, error: str = ""):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if job.state is not JobState.DRAFT:
        return redirect(request, f"/jobs/{job.job_id}")
    prices = unit_prices(ctx.adapters, ctx.call_stats())
    target_groups = [  # 按 AI 分组列出全部型号(默认全勾)
        (ctx.display_names[name], [Target(surface=name, model=m) for m in ctx.adapters[name].meta().models])
        for name in sorted(ctx.adapters)
    ]
    t = MESSAGES[lang_of(request)]
    basis = []
    if prices.measured:
        basis.append(t["est_basis_measured"].format(
            names="、".join(ctx.target_name(x.surface, x.model) for x in prices.measured)))
    if prices.declared:
        basis.append(t["est_basis_declared"].format(
            names="、".join(ctx.target_name(x.surface, x.model) for x in prices.declared)))
    basis.append(t["est_basis_tail"])
    return ctx.render(request, "confirm.html", job=job, tiers=TIERS, tier_table=TIER_TABLE[lang_of(request)],
                      target_groups=target_groups, est_basis=" ".join(basis),
                      error=error_text(request, error))


@router.post("/jobs/{job_id}/estimate")
async def confirm_estimate(
    request: Request,
    job_id: str,
    needs_selected: list[int] = Form([]),
    custom: str = Form(""),
    targets_selected: list[str] = Form([]),
    tier: str = Form("quick"),
    variants: int = Form(3),
    dependency: str = Form(""),
):
    """确认页的实时预估:页面每次勾选变化都把整张表单发来,算好的数字发回去,页面只负责显示。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None or tier not in TIERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    estimate = estimate_job(
        n_needs=count_needs(job, needs_selected, custom), variants_per_need=variants,
        targets=selected_targets(targets_selected, ctx.adapters), tier_id=tier, dependency=bool(dependency),
        prices=unit_prices(ctx.adapters, ctx.call_stats()),
    )
    return JSONResponse({
        "calls": estimate.calls,
        "cost_usd": estimate.cost_usd,
        "minutes": estimate.minutes,
        "breakdown": [{"display": ctx.target_name(t.surface, t.model), "cost_usd": cost}
                      for t, cost in estimate.per_target],
    })


@router.post("/jobs/{job_id}/confirm")
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
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if job.state is not JobState.DRAFT:
        return redirect(request, f"/jobs/{job.job_id}")
    choice = confirm_choice(
        job, ctx.adapters, needs_selected=needs_selected, custom=custom,
        competitors_selected=competitors_selected, targets_selected=targets_selected,
        tier=tier, variants=variants, budget=budget, dependency=dependency,
    )
    if isinstance(choice, str):
        return redirect(request, f"/jobs/{job.job_id}/confirm?error={choice}")
    job.freeze(needs=choice.needs, competitors=choice.competitors, targets=choice.targets,
               tier_id=choice.tier_id, variants_per_need=choice.variants_per_need,
               budget_usd=choice.budget_usd, dependency_enabled=choice.dependency_enabled)
    ctx.launcher.launch(job)
    return redirect(request, f"/jobs/{job.job_id}")


@router.post("/jobs/{job_id}/resume")
async def resume(request: Request, job_id: str):
    """幂等续跑(v0.4.9):中断/失败任务补缺口;已完成任务重新生成报告
    (v0.4.16:追加一版报告快照,旧版本留档)。执行器可重入,已存样本按幂等键
    复用——已花的钱不重花。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not (job.state in (JobState.INTERRUPTED, JobState.FAILED) or job.finished):
        return redirect(request, f"/jobs/{job.job_id}")
    missing = sorted({t.surface for t in job.targets if t.surface not in ctx.adapters})
    if missing:
        return redirect(request, f"/?error=err_missing_ai&names={','.join(missing)}")
    ctx.launcher.launch(job)
    return redirect(request, f"/jobs/{job.job_id}")


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    now = datetime.now(UTC)
    t = MESSAGES[lang_of(request)]
    # 有代码的出错说明按当前界面语言组句;没代码的(意外异常)照原文
    failure = t[job.error_code].format(**job.error_params) if job.error_code else job.error
    quiet = job.quiet_minutes(now)  # 卡住了没有?(v0.4.22)超过阈值给 JOB_STALLED
    stalled = t["job_stalled"].format(minutes=round(quiet)) if quiet is not None and quiet >= STALL_MINUTES else None
    return ctx.render(request, "job.html", job=job, elapsed_minutes=job.elapsed_minutes(now),
                      eta_minutes=job.eta_minutes(now), error_text=failure, quiet_minutes=quiet,
                      stalled_text=stalled)


@router.get("/jobs/{job_id}/report", response_class=HTMLResponse)
async def job_report(request: Request, job_id: str):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished:
        return redirect(request, f"/jobs/{job.job_id}")
    return ctx.render(request, "report.html", job=job,
                      summary=summarize(job.need_reports, job.surface_verdicts, ctx.target_name))
