"""L4 路由(第 16 章四页:输入 → 确认 → 任务 → 报告)。每个路由只管收发:
表单解析与校验在 report/forms.py,预估在 report/estimate.py,起执行器在 report/launcher.py。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version as _package_version

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from engine.drafting import PromptCandidate, draft_prompt_candidates
from engine.models import Target
from engine.page_check import BUDGET_S as VERIFY_BUDGET_S
from engine.storage import Storage
from engine.tiers import tier_targets
from report.estimate import estimate_job, unit_prices
from report.export import build_bundle, content_disposition
from report.forms import (
    brand_from_form,
    confirm_choice,
    count_prompts,
    manual_candidates,
    preselect_candidates,
    tier_choice,
)
from report.i18n import MESSAGES
from report.jobs import STALL_MINUTES, DiagnosisJob, JobState
from report.response_view import build_response_view, run_belongs_to
from report.summary import summarize
from report.web import ctx_of, error_text, lang_of, not_found, redirect

router = APIRouter()
PSL_SNAPSHOT_VERSION = _package_version("tldextract")  # 注册域名用的公共后缀表快照版本,写进报告方法说明(S4)


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
    drafted: list[PromptCandidate] = []
    generation_adapter = next(
        (a for a in ctx.adapters.values() if a.meta().capabilities.supports_citations), None
    )
    if generate and generation_adapter is not None:
        with Storage(ctx.dsn) as store:
            drafted, job.competitors_suggested = draft_prompt_candidates(
                generation_adapter, category=category, brand=brand, locale=locale,
                ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task="出题", run=run),
            )
    job.candidates = drafted + manual_candidates(questions, brand)
    if not job.candidates:
        return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                          error=error_text(request, "err_no_question"))
    job.preselected = preselect_candidates(job.candidates)
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
    target_groups = [  # 自定义区按 AI 分组列出全部型号;默认勾选跟默认档位(S9.1)
        (ctx.display_names[name], [Target(surface=name, model=m) for m in ctx.adapters[name].meta().models])
        for name in sorted(ctx.adapters)
    ]
    tiers = ctx.tiers
    tier_cards = [(spec, tier_targets(spec, ctx.adapters)) for spec in tiers.tiers]
    tier_defaults = {spec.name: {"runs": spec.runs_per_prompt, "keys": [x.key for x in targets]}
                     for spec, targets in tier_cards}
    default_keys = {x.key for spec, targets in tier_cards if spec.name == tiers.default for x in targets}
    t = MESSAGES[lang_of(request)]
    basis = []
    if prices.measured:
        basis.append(t["est_basis_measured"].format(
            names="、".join(ctx.target_name(x.surface, x.model) for x in prices.measured)))
    if prices.declared:
        basis.append(t["est_basis_declared"].format(
            names="、".join(ctx.target_name(x.surface, x.model) for x in prices.declared)))
    basis.append(t["est_basis_tail"])
    return ctx.render(request, "confirm.html", job=job, target_groups=target_groups,
                      tier_cards=tier_cards, tier_defaults=tier_defaults, default_tier=tiers.default,
                      default_keys=default_keys,
                      default_runs=tiers.default_tier.runs_per_prompt, custom_runs=tiers.custom_runs,
                      est_basis=" ".join(basis),
                      error=error_text(request, error, min=str(tiers.custom_runs.min), max=str(tiers.custom_runs.max)))


@router.post("/jobs/{job_id}/estimate")
async def confirm_estimate(
    request: Request,
    job_id: str,
    prompts_selected: list[int] = Form([]),
    custom: str = Form(""),
    targets_selected: list[str] = Form([]),
    tier: str = Form(""),
    runs: str = Form(""),
    dependency: str = Form(""),
):
    """确认页的实时预估:页面每次勾选变化都把整张表单发来,算好的数字发回去,页面只负责显示。
    S9.1:同时按三档各算一份(卡片上显示),并告诉页面当前勾选是不是自定义。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    chosen = tier_choice(ctx.tiers, ctx.adapters, tier=tier, runs=runs, targets_selected=targets_selected)
    if isinstance(chosen, str):
        return JSONResponse({"error": chosen}, status_code=400)
    choice, targets = chosen
    n_prompts = count_prompts(job, prompts_selected, custom)
    prices = unit_prices(ctx.adapters, ctx.call_stats())
    estimate = estimate_job(n_prompts=n_prompts, runs_per_prompt=choice.runs_per_prompt, targets=targets,
                            dependency=bool(dependency), prices=prices)
    per_tier = {
        spec.name: estimate_job(n_prompts=n_prompts, runs_per_prompt=spec.runs_per_prompt,
                                targets=tier_targets(spec, ctx.adapters), dependency=bool(dependency), prices=prices)
        for spec in ctx.tiers.tiers
    }
    return JSONResponse({
        "calls": estimate.calls,
        "cost_usd": estimate.cost_usd,
        "minutes": estimate.minutes,
        "custom": choice.custom,
        "breakdown": [{"display": ctx.target_name(t.surface, t.model), "cost_usd": cost}
                      for t, cost in estimate.per_target],
        "tiers": {name: {"calls": e.calls, "cost_usd": round(e.cost_usd, 4), "minutes": round(e.minutes, 1)}
                  for name, e in per_tier.items()},
    })


@router.post("/jobs/{job_id}/confirm")
async def confirm(
    request: Request,
    job_id: str,
    prompts_selected: list[int] = Form([]),
    custom: str = Form(""),
    competitors_selected: list[int] = Form([]),
    targets_selected: list[str] = Form([]),
    tier: str = Form(""),
    runs: str = Form(""),
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
        job, ctx.adapters, ctx.tiers, prompts_selected=prompts_selected, custom=custom,
        competitors_selected=competitors_selected, targets_selected=targets_selected, tier=tier, runs=runs,
        budget=budget, dependency=dependency,
    )
    if isinstance(choice, str):
        return redirect(request, f"/jobs/{job.job_id}/confirm?error={choice}")
    job.freeze(prompts=choice.prompts, competitors=choice.competitors, targets=choice.targets, tier=choice.tier,
               budget_usd=choice.budget_usd, dependency_enabled=choice.dependency_enabled)
    ctx.launcher.launch(job, replay=False)
    return redirect(request, f"/jobs/{job.job_id}")


@router.post("/jobs/{job_id}/resume")
async def resume(request: Request, job_id: str):
    """幂等续跑(v0.4.9):中断/失败任务补缺口;已完成任务重新生成报告(v0.4.16:追加一版报告快照,
    旧版本留档)——只用已有样本重算,不向 AI 提问。已存样本按幂等键复用,已花的钱不重花。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not (job.state in (JobState.INTERRUPTED, JobState.FAILED) or job.finished):
        return redirect(request, f"/jobs/{job.job_id}")
    return _relaunch(request, job, replay=job.finished)


@router.post("/jobs/{job_id}/official_domains")
async def choose_official_domains(
    request: Request, job_id: str, confirm: list[str] = Form([]), remove: list[str] = Form([]),
):
    """用户确认 B 类 / 去掉 A 类(S5,产品文档 D12):只认名单里列出的网址;名单改了就按新名单
    重新生成报告——只用已有样本重算,不向 AI 提问;没改就回报告页。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished:
        return redirect(request, f"/jobs/{job.job_id}")
    if not job.apply_domain_choice(confirm=confirm, remove=remove):
        return redirect(request, f"/jobs/{job.job_id}/report")
    return _relaunch(request, job, replay=True)


def _relaunch(request: Request, job: DiagnosisJob, *, replay: bool):
    """再起一次执行器;有 AI 没接入(缺 key)就不起,回首页说明。"""
    ctx = ctx_of(request)
    missing = sorted({t.surface for t in job.targets if t.surface not in ctx.adapters})
    if missing:
        return redirect(request, f"/?error=err_missing_ai&names={','.join(missing)}")
    ctx.launcher.launch(job, replay=replay)
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
    return ctx.render(request, "job.html", job=job, runs_per_prompt=job.frozen_tier().runs_per_prompt,
                      elapsed_minutes=job.elapsed_minutes(now), eta_minutes=job.eta_minutes(now),
                      verify_remaining_minutes=job.verify_remaining_minutes(now, VERIFY_BUDGET_S),
                      error_text=failure, quiet_minutes=quiet, stalled_text=stalled)


@router.get("/jobs/{job_id}/report", response_class=HTMLResponse)
async def job_report(request: Request, job_id: str):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished:
        return redirect(request, f"/jobs/{job.job_id}")
    # 报告写实际发生的每题次数(采样说明),不读当前策略:策略以后随档位变,旧报告不能跟着变
    return ctx.render(request, "report.html", job=job, runs_per_prompt=job.sampling_note().tier.runs_per_prompt,
                      summary=summarize(job.class_reports, ctx.target_name, ctx.api_name),
                      psl_version=PSL_SNAPSHOT_VERSION)


@router.get("/jobs/{job_id}/responses/{run_id}", response_class=HTMLResponse)
async def response_page(request: Request, job_id: str, run_id: str):
    """单条回答页(S8.3):只读库里的原始记录与已存的判定行,不属于本单的回答 404。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with Storage(ctx.dsn) as store:
        run = store.run_by_id(run_id)
        if run is None or not run_belongs_to(job, run):
            return not_found()
        observations = store.observations_for_run(run_id)
    return ctx.render(request, "response.html", job=job, view=build_response_view(job, run, observations))


@router.get("/jobs/{job_id}/responses/{run_id}/download")
async def response_download(request: Request, job_id: str, run_id: str):
    """原始记录下载(S8.3):整条 SurfaceRun 的 JSON,含平台原始返回。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with Storage(ctx.dsn) as store:
        run = store.run_by_id(run_id)
    if run is None or not run_belongs_to(job, run):
        return not_found()
    return JSONResponse(run.model_dump(mode="json"),
                        headers={"Content-Disposition": f'attachment; filename="response-{run_id}.json"'})


@router.get("/jobs/{job_id}/download")
async def job_download(request: Request, job_id: str):
    """下载结果文件(S8.4):从库里打包 zip;没完成的任务回任务页。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished:
        return redirect(request, f"/jobs/{job.job_id}")
    summary = summarize(job.class_reports, ctx.target_name, ctx.api_name)
    with Storage(ctx.dsn) as store:
        data = build_bundle(job, store, lang_of(request), ctx.target_name, summary)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": content_disposition(job)})
