"""L4 路由(第 16 章四页:输入 → 确认 → 任务 → 报告)。每个路由只管收发:
表单解析与校验在 report/forms.py,预估在 report/estimate.py,起执行器在 report/launcher.py。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version as _package_version
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from adapters.base import SurfaceAdapter
from engine.ai_task import TaskFailed
from engine.detectors.name_risk import is_risky
from engine.drafting import PromptCandidate, draft_prompt_candidates
from engine.judge import cannot_judge, pick_judge, usable_judges
from engine.models import SearchMode, StopReason, Target
from engine.page_check import BUDGET_S as VERIFY_BUDGET_S
from engine.page_check import PageChecker
from engine.scheduler import AccountProblem
from engine.spellings import suggest_spellings
from engine.storage import STORAGE_DOWN_ERRORS, Storage
from engine.tiers import MentionMethod, tier_targets
from report.estimate import VERIFY_MAX_MINUTES, estimate_job, unit_prices
from report.export import build_bundle, content_disposition
from report.forms import (
    brand_from_form,
    category_choice,
    confirm_choice,
    count_prompts,
    lines_of,
    manual_candidates,
    preselect_candidates,
    tier_choice,
    website_host,
)
from report.i18n import MESSAGES
from report.jobs import MAX_UNSURE_JUDGED, STALL_MINUTES, DiagnosisJob, JobState, PendingDraft
from report.narrative import calibration_text, stop_detail_text
from report.response_view import build_response_view, run_belongs_to
from report.summary import summarize
from report.web import WebContext, ctx_of, error_text, lang_of, not_found, redirect

log = structlog.get_logger("report.routes")
router = APIRouter()
SPELLING_PAGES_BUDGET_S = 45.0  # 出题时为数写法开网页的时间上限(用户在等确认页;开不完的不算分母)
PSL_SNAPSHOT_VERSION = _package_version("tldextract")  # 注册域名用的公共后缀表快照版本,写进报告方法说明(S4)


@router.get("/", response_class=HTMLResponse)
def index(request: Request, error: str = ""):
    ctx = ctx_of(request)
    params = {k: v for k, v in request.query_params.items() if k not in ("lang", "error")}  # 错误句子的占位符
    return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                      error=error_text(request, error, **params))


@router.post("/jobs")
def create_job(
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
    if website_host(website) is None:  # 不是网址就别往下走:否则官网名单为空、一单白花钱(审核 2026-10-27 黄 18)
        return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                          error=error_text(request, "FORM_BAD_WEBSITE"))
    brand, normalized = brand_from_form(brand_name, aliases, website)
    # 先有任务编号再花钱:品类核对与出题的每次调用都当场记进辅助调用账本(哪怕最后没成,花掉的钱也有记录)
    job = DiagnosisJob(brand=brand, website=normalized, category=category.strip(), locale=locale,
                       candidates=[], competitors_suggested=[])
    if not lines_of(questions) and not (generate and _generation_adapter(ctx) is not None):
        return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                          error=error_text(request, "FORM_NO_QUESTION"))  # 没题可测:一分钱都别花
    # 品类自动核对(S11.4,产品文档 D24):出题前照官网核一下;只有「不是一类」才拦一页问清楚,其余照常往下走
    check = ctx.category_check(job)
    if check is not None and check.blocks:
        job.category_check = check
        job.pending_draft = PendingDraft(questions=questions, generate=bool(generate))
        ctx.registry.add(job)
        return redirect(request, f"/jobs/{job.job_id}/category")
    return _draft_and_go(request, job, questions=questions, generate=bool(generate))


def _generation_adapter(ctx: WebContext) -> SurfaceAdapter | None:
    """出题 / 列写法用的仪器:第一个支持引用(能联网)的。"""
    return next((a for a in ctx.adapters.values() if a.meta().capabilities.supports_citations), None)


@router.get("/jobs/{job_id}/category", response_class=HTMLResponse)
def category_page(request: Request, job_id: str, error: str = ""):
    """品类核对页(S11.4):只在自动核对判「不是一类」时出现——你填的、官网原句、照官网更像什么,三选一,选完才出题。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if job.pending_draft is None or job.category_check is None:
        return redirect(request, f"/jobs/{job.job_id}/confirm")
    return ctx.render(request, "category.html", job=job, check=job.category_check,
                      error=error_text(request, error))


@router.post("/jobs/{job_id}/category")
def category_decide(request: Request, job_id: str, choice: str = Form(""), custom_category: str = Form("")):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with job.lock:  # 连点两下:第二个请求看到品类已定 → 去确认页(正在出题就显示等待)
        pending = job.pending_draft
        if pending is None:
            return redirect(request, f"/jobs/{job.job_id}/confirm")
        decided = category_choice(job, choice=choice, custom=custom_category)
        if decided is None:
            return redirect(request, f"/jobs/{job.job_id}/category?error=FORM_CATEGORY_EMPTY")
        log.info("category_decided", job_id=job.job_id, choice=choice, before=job.category, after=decided)
        job.category = decided
        job.pending_draft = None
        job.drafting = pending.generate
    return _draft_and_go(request, job, questions=pending.questions, generate=pending.generate)


def _abandon_draft(request: Request, job: DiagnosisJob, path: str) -> Response:
    """出题没成:把这单从登记处撤掉(品类页来的已登记过),历史表不留一条没题、点不开的空任务;
    花掉的钱已按任务编号记在辅助调用账本里,不随之消失(审核 2026-10-27 红 2)。只撤仍是草稿的——
    出题期间用户若已在别处确认开跑,这单在花钱,不能撤(第二轮)。"""
    with job.lock:
        if job.state is JobState.DRAFT:
            ctx_of(request).registry.discard(job.job_id)
    return redirect(request, path)


def _draft_and_go(request: Request, job: DiagnosisJob, *, questions: str, generate: bool) -> Response:
    """品类定下来之后:(勾了就)让 AI 出题、列写法,登记任务,去确认页。出题出错回首页说人话(S11.1)。
    出题期间 job.drafting 为真(确认页显示等待),出完或出错都放下。"""
    generation_adapter = _generation_adapter(ctx_of(request))
    try:
        return _draft_then_register(request, job, questions=questions, generate=generate,
                                    generation_adapter=generation_adapter)
    finally:
        job.drafting = False


def _draft_then_register(
    request: Request, job: DiagnosisJob, *, questions: str, generate: bool, generation_adapter: SurfaceAdapter | None,
) -> Response:
    ctx = ctx_of(request)
    locale = job.locale
    brand, normalized, category = job.brand, job.website, job.category
    drafted: list[PromptCandidate] = []
    if generate and generation_adapter is not None:
        job.drafting = True
        try:
            with Storage(ctx.dsn) as store:
                try:
                    drafted, job.competitors_suggested = draft_prompt_candidates(
                        generation_adapter, category=category, brand=brand, locale=locale,
                        ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task="出题", run=run),
                    )
                except TaskFailed as exc:  # 出不好题:回首页说人话(S11.1),花掉的钱已记账
                    return _abandon_draft(request, job, f"/?error=DRAFTING_FAILED&detail={quote(str(exc)[:200])}")
                # 顺手让 AI 列品牌的常见写法,并在它搜到的网页 + 官网首页里数次数(S9.2b);网页同样落 page_check 表
                opener = PageChecker(store, budget_s=SPELLING_PAGES_BUDGET_S)
                try:
                    job.spellings_suggested = suggest_spellings(
                        generation_adapter, brand=brand, website=normalized, locale=locale, pages=opener,
                        ledger=lambda run: store.insert_aux_call(job_id=job.job_id, task="常见写法", run=run),
                    )
                except TaskFailed as exc:
                    return _abandon_draft(request, job, f"/?error=SPELLINGS_FAILED&detail={quote(str(exc)[:200])}")
                finally:
                    opener.close()
        except STORAGE_DOWN_ERRORS as exc:  # 出题途中库掉了:撤掉草稿回首页说人话(第二轮审核 B13;原来裸 500)
            return _abandon_draft(request, job, f"/?error=WEB_DB_UNREACHABLE&detail={quote(str(exc)[:200])}")
        except AccountProblem as exc:  # 出题时就撞上账户问题:同一套话(错误库的码与账户类 error_type 同名),回首页说
            code = exc.error_type.value
            display = ctx.display_names.get(exc.surface, exc.surface)
            return _abandon_draft(request, job, f"/?error={code}&ai={quote(display)}&model={quote(exc.model)}"
                                                f"&detail={quote(exc.message[:200])}")
    job.candidates = drafted + manual_candidates(questions, brand, locale)
    if not job.candidates:
        return ctx.render(request, "index.html", jobs=ctx.registry.recent(), surfaces=sorted(ctx.adapters),
                          error=error_text(request, "FORM_NO_QUESTION"))
    job.preselected = preselect_candidates(job.candidates)
    ctx.registry.add(job)  # 品类页来的已登记过;同一编号再登记一次是幂等的
    return redirect(request, f"/jobs/{job.job_id}/confirm")


@router.get("/jobs/{job_id}/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, job_id: str, error: str = ""):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if job.state is not JobState.DRAFT:
        return redirect(request, f"/jobs/{job.job_id}")
    if job.pending_draft is not None:  # 品类还没定(S11.4):先回品类页
        return redirect(request, f"/jobs/{job.job_id}/category")
    if job.drafting:  # AI 正在出题:显示等待页(自动刷新),不显示空的候选题(第二轮)
        return ctx.render(request, "drafting.html", job=job)
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
                      est_basis=" ".join(basis), verify_minutes=round(VERIFY_MAX_MINUTES),
                      calibration_text=(calibration_text(ctx.calibration, lang_of(request))
                                        if ctx.calibration is not None else None),
                      name_risky=is_risky(job.brand),
                      no_judge=cannot_judge(ctx.adapters) if not usable_judges(ctx.judges, ctx.adapters) else [],
                      judge_candidates=[candidate.name for candidate in ctx.judges.candidates],
                      error=error_text(request, error, min=str(tiers.custom_runs.min), max=str(tiers.custom_runs.max)))


@router.post("/jobs/{job_id}/estimate")
def confirm_estimate(
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
    usable = usable_judges(ctx.judges, ctx.adapters)

    def judging(method: MentionMethod, chosen_targets: list[Target]) -> dict[str, Any]:
        """裁判的钱:高档每条回答判一次(S9.3);中档名字有风险时按上限算,只判说不准的(BR4);其余不请。
        单价取这几个被测对象会请到的裁判里最贵的(上界,BR6);一家裁判都请不到就没有这笔钱。"""
        picked = [pick_judge(usable, ctx.adapters[t.surface].meta().provider) for t in chosen_targets]
        unit = [judge.spec.est_cost_per_call_usd for judge in picked if judge is not None]
        if not unit:
            return {}
        price = max(unit)
        if method is MentionMethod.RULES_FUZZY_JUDGE:
            return {"judge_cost_per_call": price}
        if method is MentionMethod.RULES_FUZZY and is_risky(job.brand):
            return {"judge_cost_per_call": price, "judge_limit": MAX_UNSURE_JUDGED}
        return {}

    estimate = estimate_job(n_prompts=n_prompts, runs_per_prompt=choice.runs_per_prompt, targets=targets,
                            dependency=bool(dependency), prices=prices, **judging(choice.mention_method, targets))
    per_tier = {
        spec.name: estimate_job(n_prompts=n_prompts, runs_per_prompt=spec.runs_per_prompt,
                                targets=tier_targets(spec, ctx.adapters), dependency=bool(dependency), prices=prices,
                                **judging(spec.mention_method, tier_targets(spec, ctx.adapters)))
        for spec in ctx.tiers.tiers
    }
    breakdown = [{"display": ctx.target_name(t.surface, t.model), "cost_usd": cost} for t, cost in estimate.per_target]
    if estimate.judge_calls:
        breakdown.append({"display": MESSAGES[lang_of(request)]["est_judge_word"], "cost_usd": estimate.judge_cost_usd})
    return JSONResponse({
        "calls": estimate.calls,
        "cost_usd": estimate.cost_usd,
        "minutes": estimate.minutes,
        "custom": choice.custom,
        "judge_calls": estimate.judge_calls,
        "judge_cost_usd": estimate.judge_cost_usd,
        "judge_upto": estimate.judge_upto,
        "breakdown": breakdown,
        "tiers": {name: {"calls": e.calls, "cost_usd": round(e.cost_usd, 4), "minutes": round(e.minutes, 1),
                         "judge_calls": e.judge_calls, "judge_cost_usd": round(e.judge_cost_usd, 4),
                         "judge_upto": e.judge_upto}
                  for name, e in per_tier.items()},
    })


@router.post("/jobs/{job_id}/confirm")
def confirm(
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
    spellings_selected: list[int] = Form([]),
):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with job.lock:  # 查状态 → 冻结 → 起执行器一步做完:连点两下只起一个执行器(第二轮)
        if job.state is not JobState.DRAFT:
            return redirect(request, f"/jobs/{job.job_id}")
        if job.pending_draft is not None:
            return redirect(request, f"/jobs/{job.job_id}/category")
        if job.drafting:
            return redirect(request, f"/jobs/{job.job_id}/confirm")
        choice = confirm_choice(
            job, ctx.adapters, ctx.tiers, prompts_selected=prompts_selected, custom=custom,
            competitors_selected=competitors_selected, targets_selected=targets_selected, tier=tier, runs=runs,
            budget=budget, dependency=dependency, spellings_selected=spellings_selected,
        )
        if isinstance(choice, str):
            return redirect(request, f"/jobs/{job.job_id}/confirm?error={choice}")
        # 勾上的常见写法进别名表(S9.2b),随冻结进指纹
        job.brand = job.brand.model_copy(update={"aliases": choice.aliases})
        job.freeze(prompts=choice.prompts, competitors=choice.competitors, targets=choice.targets, tier=choice.tier,
                   budget_usd=choice.budget_usd, dependency_enabled=choice.dependency_enabled)
        ctx.launcher.launch(job, replay=False)
    return redirect(request, f"/jobs/{job.job_id}")


@router.post("/jobs/{job_id}/resume")
def resume(request: Request, job_id: str, budget: str = Form(""), regenerate: str = Form("")):
    """幂等续跑(v0.4.9):中断 / 失败 / **部分完成(S10.3)** 的任务补缺口——只补空槽位,已花的钱不重花;
    部分完成的可顺手提高费用上限(预算截断的腿才有机会补齐)。「重新生成报告」按钮带 regenerate=1(或全部完成的任务):
    v0.4.16 追加一版报告快照、旧版本留档——只用已有样本重算,不向 AI 提问。原来部分完成的单点「重新生成报告」
    会接着问 AI 花钱,按钮旁却写着不花钱(第二轮)。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with job.lock:  # 查状态 → 起执行器一步做完(第二轮)
        if not (job.state in (JobState.INTERRUPTED, JobState.FAILED) or job.finished):
            return redirect(request, f"/jobs/{job.job_id}")
        if budget.strip():
            try:
                job.raise_budget(float(budget))
            except ValueError:
                return redirect(request, f"/jobs/{job.job_id}?error=FORM_BAD_BUDGET")
        only_recompute = job.state is JobState.DONE or (bool(regenerate) and job.finished)
        return _relaunch(request, job, replay=only_recompute)


@router.post("/jobs/{job_id}/retest")
def retest(request: Request, job_id: str):
    """「修好后再测」(S11.3):按这一单冻结的口径新开一单,一律重新提问、不走回答缓存,直接开跑(费用上限与上一单相同),
    跳到新单的任务页。没跑完的单不能再测;上一次再测还在跑就跳到它、不另开一单(连点两下不花两份钱);
    有 AI 没接入(缺 key)就不开,回首页说明。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with job.lock:  # 「找在跑的再测 → 开新单 → 起执行器」一步做完:连点两下只开一张新单(第二轮)
        if not job.finished:
            return redirect(request, f"/jobs/{job.job_id}")
        running = next((r for r in ctx.registry.retests_of(job.job_id)
                        if r.state in (JobState.CONFIRMED, JobState.RUNNING)), None)
        if running is not None:
            return redirect(request, f"/jobs/{running.job_id}")
        if missing := _missing_adapters(ctx, job):
            return redirect(request, f"/?error=RESUME_MISSING_AI&names={','.join(missing)}")
        new = job.retest()
        ctx.registry.add(new)
        ctx.launcher.launch(new, replay=False)
    return redirect(request, f"/jobs/{new.job_id}")


@router.post("/jobs/{job_id}/official_domains")
def choose_official_domains(
    request: Request, job_id: str, confirm: list[str] = Form([]), remove: list[str] = Form([]),
):
    """用户确认 B 类 / 去掉 A 类(S5,产品文档 D12):只认名单里列出的网址;名单改了就按新名单
    重新生成报告——只用已有样本重算,不向 AI 提问;没改就回报告页。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    with job.lock:  # 改名单 → 只重算,一步做完(第二轮)
        if not job.finished:
            return redirect(request, f"/jobs/{job.job_id}")
        if not job.apply_domain_choice(confirm=confirm, remove=remove):
            return redirect(request, f"/jobs/{job.job_id}/report")
        return _relaunch(request, job, replay=True)


def _missing_adapters(ctx: WebContext, job: DiagnosisJob) -> list[str]:
    """任务的被测对象里没接入(缺 key)的 AI。"""
    return sorted({t.surface for t in job.targets if t.surface not in ctx.adapters})


def _relaunch(request: Request, job: DiagnosisJob, *, replay: bool):
    """再起一次执行器;有 AI 没接入(缺 key)就不起,回首页说明。"""
    ctx = ctx_of(request)
    if missing := _missing_adapters(ctx, job):
        return redirect(request, f"/?error=RESUME_MISSING_AI&names={','.join(missing)}")
    ctx.launcher.launch(job, replay=replay)
    return redirect(request, f"/jobs/{job.job_id}")


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if job.state is JobState.DRAFT:  # 还没确认就没有档位参数:回到它该在的页(确认页;品类没定再转品类页)
        return redirect(request, f"/jobs/{job.job_id}/confirm")
    now = datetime.now(UTC)
    lang = lang_of(request)
    # 有代码的出错说明从错误库按当前界面语言组句(S10.1);没代码的(旧快照)照原文
    failure = (ctx.errors[job.error_code].text(lang, with_next_step=True, **job.error_params)
               if job.error_code else job.error)
    quiet = job.quiet_minutes(now)  # 卡住了没有?(v0.4.22)超过阈值给 JOB_STALLED
    stalled = (ctx.errors["JOB_STALLED"].text(lang, with_next_step=True, minutes=str(round(quiet)))
               if quiet is not None and quiet >= STALL_MINUTES else None)
    form_error = error_text(request, request.query_params.get("error", ""))  # 接着跑时费用上限填错(S10.3)
    return ctx.render(request, "job.html", job=job, runs_per_prompt=job.frozen_tier().runs_per_prompt,
                      elapsed_minutes=job.elapsed_minutes(now), eta_minutes=job.eta_minutes(now),
                      verify_remaining_minutes=job.verify_remaining_minutes(now, VERIFY_BUDGET_S),
                      error_text=failure, quiet_minutes=quiet, stalled_text=stalled,
                      stop_texts=stop_texts(ctx, job, lang), form_error=form_error,
                      cap_dialog=_cap_dialog(ctx, job, lang))


def stop_texts(ctx: WebContext, job: DiagnosisJob, lang: str) -> list[tuple[str, str]]:
    """提前停的腿各一句(码, 句子),任务页 / 报告 / 结果文件共用(S10.2)。"""
    return [(d.stop_reason.value, stop_detail_text(d, lang, ctx.errors, ctx.api_name(d.surface)))
            for d in job.stop_details if d.truncated]


def _cap_dialog(ctx: WebContext, job: DiagnosisJob, lang: str) -> dict[str, Any] | None:
    """跑完了、有型号是因为费用上限停的:任务页弹窗问要不要补齐(用户 2026-11-03 定「跑完再问」,别家不等、不多花钱)。
    建议上限 = 已花的钱 + 没问完的次数 × 单价(确认页同一份),再留一成余量,向上取到 0.1 美元。"""
    stops = [d for d in job.stop_details if d.stop_reason is StopReason.BUDGET_EXHAUSTED]
    if job.state is not JobState.PARTIAL or not stops:
        return None
    t = MESSAGES[lang]
    prices = unit_prices(ctx.adapters, ctx.call_stats())
    slots = len(job.prompts) * job.frozen_tier().runs_per_prompt
    extra = 0.0
    lines = []
    for d in stops:
        key = Target(surface=d.surface, model=d.model).key
        table = prices.search_disabled if d.search_mode is SearchMode.SEARCH_DISABLED else prices.default
        extra += max(0, slots - d.n_attempts) * table.get(key, 0.0)
        lines.append(t["cap_line"].format(ai=ctx.api_name(d.surface), model=d.model, k=d.n_attempts, n=slots))
    suggested = (int((job.spent_usd + extra * 1.1) * 10) + 1) / 10
    return {"lines": lines, "suggested": suggested}


@router.get("/errors", response_class=HTMLResponse)
def error_codes(request: Request):
    """错误码对照页(S10.1):config/errors.yaml 全部条目,给用户与客服对照。"""
    ctx = ctx_of(request)
    return ctx.render(request, "errors.html", groups=ctx.errors.by_scope())


@router.get("/jobs/{job_id}/report", response_class=HTMLResponse)
def job_report(request: Request, job_id: str):
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished or job.sampling is None:  # 没完成、或报告快照读不动(任务页给「重新生成报告」):回任务页
        return redirect(request, f"/jobs/{job.job_id}")
    # 报告写实际发生的每题次数(采样说明),不读当前策略:策略以后随档位变,旧报告不能跟着变
    return ctx.render(request, "report.html", job=job, runs_per_prompt=job.sampling_note().tier.runs_per_prompt,
                      summary=summarize(job.class_reports, ctx.target_name, ctx.api_name),
                      psl_version=PSL_SNAPSHOT_VERSION, stop_texts=stop_texts(ctx, job, lang_of(request)),
                      truncated={(d.surface, d.model) for d in job.stop_details if d.truncated},
                      # 再测(S11.3):本单对照的上一单 + 本单已有的再测,报告页互相链到好对照
                      parent=ctx.registry.get(job.parent_job_id) if job.parent_job_id else None,
                      retests=ctx.registry.retests_of(job.job_id))


@router.get("/jobs/{job_id}/responses/{run_id}", response_class=HTMLResponse)
def response_page(request: Request, job_id: str, run_id: str):
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
def response_download(request: Request, job_id: str, run_id: str):
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
def job_download(request: Request, job_id: str):
    """下载结果文件(S8.4):从库里打包 zip;没完成的任务、报告快照读不动的任务回任务页。"""
    ctx = ctx_of(request)
    job = ctx.registry.get(job_id)
    if job is None:
        return not_found()
    if not job.finished or job.sampling is None:
        return redirect(request, f"/jobs/{job.job_id}")
    summary = summarize(job.class_reports, ctx.target_name, ctx.api_name)
    with Storage(ctx.dsn) as store:
        data = build_bundle(job, store, lang_of(request), ctx.target_name, summary,
                            stop_texts(ctx, job, lang_of(request)))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": content_disposition(job)})
