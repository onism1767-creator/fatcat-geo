"""L4 任务执行器:只做编排(架构书附录 v0.4.24 第 5 节 ⑥),跑在后台线程,进度写回 job。

流程:ACCESS 审计(秒级先出)→ 题目落库 → 采样(计划 = 被测对象 × 检索模式,跨全部题目;计划间并发、
计划内严格串行;psycopg 连接非线程安全,每计划独立连接)→ 引用独立核验 → 逐题型:观测(L2 engine.observe)
→ 聚合(L2 engine.aggregate)→ 诊断(L3 diagnosis.assemble)→ 落档。任何未预期异常 → FAILED,页面如实展示。

算数在 L2、下结论在 L3;本模块不含判定规则,也不含任何 surface 名字符串。
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import structlog

from adapters.base import SurfaceAdapter
from crawler.reddit_api import RedditFetcher
from crawler.render import PlaywrightRenderer, playwright_available
from crawler.wayback import WaybackClient
from diagnosis import access_audit
from diagnosis.access_audit import PathProbe, with_path_probes
from diagnosis.assemble import diagnose_class_targets
from diagnosis.diagnose import access_finding, diagnose_class
from diagnosis.recommender import EvidenceEntry
from diagnosis.verdict import VerdictInput, surface_verdict
from engine.aggregate import ClassSurfaceMetric, TargetMeasurement, measure_closed_book, measure_target
from engine.brand_domains import BrandDomainDiscovery, load_platforms
from engine.detectors.matching import registrable
from engine.method import METHOD_VERSION
from engine.models import (
    BrandDomainList,
    BrandSpec,
    Prompt,
    PromptIntent,
    RunErrorType,
    SearchMode,
    SourceClass,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import ObserveContext, RunObservations, observe, persist_observations
from engine.official_domains import official_candidates
from engine.page_check import PageChecker, PageIndex, PageRequest, normalize_url
from engine.resolve import ResolutionContext, UrlResolver
from engine.scheduler import (
    AccountProblem,
    RepeatSampler,
    SamplingResult,
    is_account_error,
    reinterpret_account_errors,
)
from engine.storage import Storage, StorageUnavailable
from engine.validity import is_valid_answer
from report.archive import attach_latest_report
from report.jobs import (
    PLAN_CONCURRENCY,
    POLICY,
    ClassReport,
    DiagnosisJob,
    JobState,
    SamplingNote,
    SurfaceQuotes,
    current_code_version,
)

log = structlog.get_logger("report.runner")

_TRUNCATION_REASONS = (
    StopReason.BUDGET_EXHAUSTED,
    StopReason.FAILURE_RATE_EXCEEDED,
    StopReason.SURFACE_DISABLED,
)

PlanKey = tuple[Target, SearchMode]  # 一个采样计划:被测对象 × 检索模式,跨全部题目
RunsKey = tuple[str, Target]  # (题, 被测对象)


class _ProgressSink:
    """包一层存储:每落一条样本就给任务盖个时间戳、进度加一,任务页据此显示进度与
    「最近一次新结果在几分钟前」,超过阈值就报 JOB_STALLED(v0.4.22)。"""

    def __init__(self, storage: Storage, job: DiagnosisJob, lock: threading.Lock) -> None:
        self._storage, self._job, self._lock = storage, job, lock

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self._storage.insert_surface_run(run)
        with self._lock:
            self._job.completed_runs += 1
            self._job.last_result_at = datetime.now(UTC)


def execute_job(
    *,
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
    replay: bool = False,
) -> None:
    """replay=True:重新生成报告——只用库里已有的样本重算,不向 AI 提问(样本不补)。"""
    try:
        _execute(job, adapters=adapters, dsn=dsn, evidence=evidence, replay=replay)
    except AccountProblem as exc:  # v0.4.21:我方账户问题——停下、说人话、等充值后续跑
        log.error("job_halted_account_problem", job_id=job.job_id, surface=exc.surface,
                  model=exc.model, error=exc.message)
        display = adapters[exc.surface].meta().display_name if exc.surface in adapters else exc.surface
        job.state = JobState.FAILED
        job.error_code = {  # 两种账户问题两句话:换 key / 充值(文案在 report/i18n.py,随界面语言渲染)
            RunErrorType.ACCOUNT_AUTH: "job_error.account_auth",
            RunErrorType.ACCOUNT_CREDIT: "job_error.account_credit",
        }[exc.error_type]
        job.error_params = {"ai": display, "model": exc.model, "detail": exc.message}
        job.error = None
        job.phase = "phase_failed"
        _persist_failure(job, dsn)
    except StorageUnavailable as exc:  # v0.4.22:数据库掉线也要有代码和人话
        log.error("job_halted_db_unreachable", job_id=job.job_id, error=str(exc))
        job.state = JobState.FAILED
        job.error_code, job.error_params, job.error = "job_error.db_unreachable", {"detail": str(exc)}, None
        job.phase = "phase_failed"
        _persist_failure(job, dsn)
    except Exception as exc:  # 失败不静默:状态页如实展示
        log.error("job_failed", job_id=job.job_id, error=f"{type(exc).__name__}: {exc}")
        job.state = JobState.FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.phase = "phase_failed"
        _persist_failure(job, dsn)


def _persist_failure(job: DiagnosisJob, dsn: str) -> None:
    """失败态尽力落库供重启后续跑;持久化自身的失败不得掩盖原始错误。"""
    try:
        with Storage(dsn) as failure_store:
            failure_store.upsert_job_snapshot(job.to_snapshot())
    except Exception:
        log.warning("job_state_persist_failed", job_id=job.job_id)


def _execute(
    job: DiagnosisJob,
    *,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
    replay: bool = False,
) -> None:
    _reset_progress(job)
    job.access_result = access_audit.audit(job.website)
    with Storage(dsn) as storage:
        storage.ensure_schema()
        storage.upsert_job_snapshot(job.to_snapshot())
        # 被测对象 = 仪器 × 型号(v0.4.19):型号是口径的一部分,计划、样本、指标、诊断块一律按对象分开
        adapter_of = {target: adapters[target.surface] for target in job.targets}
        for prompt in job.prompts:
            storage.insert_prompt(prompt)  # 幂等:续跑不重复
        job.spent_usd = storage.aux_spent_usd(job.job_id)  # 辅助调用(出题)记在账本里,续跑也不丢
        if job.domain_list is None:  # 官网名单只找一次,随快照冻结;续跑 / 重算沿用同一份(S5)
            job.phase = "phase_discover"
            job.adopt_domain_list(_discover_domains(job))
            storage.upsert_job_snapshot(job.to_snapshot())
        results = _sample(job, dsn, adapter_of, replay=replay)

        job.phase = "phase_verify"
        runs_by, offline_runs_by = _load_runs(job, storage)
        resolution = _resolve_redirects(storage, adapters, [*runs_by.values(), *offline_runs_by.values()])
        all_runs = [run for runs in runs_by.values() for run in runs]
        pages = _check_pages(job, storage, all_runs, resolution)
        context = ObserveContext(competitors=job.competitors, resolution=resolution, pages=pages)
        observations = {
            key: [observe(run, job.brand, context) for run in runs] for key, runs in runs_by.items()
        }
        offline_observations = {
            key: [observe(run, job.brand, context) for run in runs] for key, runs in offline_runs_by.items()
        }

        job.phase = "phase_scoring"
        access = access_finding(job.access_result)
        verdict_inputs: dict[Target, list[VerdictInput]] = {}  # 被测对象 → 逐题型原料(每个型号一句话)
        for intent in PromptIntent:  # 报告按题型分块,顺序即产品文档 15.2 的五类
            prompts = [p for p in job.prompts if p.intent is intent]
            if not prompts:
                continue
            measurements, closed_book = _measure_class(
                job, intent, prompts, adapter_of, results, observations, offline_observations)
            blocks, class_verdicts = diagnose_class_targets(
                intent=intent, access=access, measurements=measurements, closed_book=closed_book,
                pages=pages, brand=job.brand, registry=evidence,
            )
            for target, verdict in class_verdicts.items():
                verdict_inputs.setdefault(target, []).append(verdict)
            job.class_reports.append(ClassReport(
                intent=intent,
                prompt_texts=[p.text for p in prompts],
                diagnosis=diagnose_class(intent, access=access, surfaces=blocks),
                quotes=[SurfaceQuotes(surface=m.target.surface, model=m.target.model, quotes=m.quotes)
                        for m in measurements],
            ))
        persist_observations(storage, [
            run for key in runs_by for run in runs_by[key] + offline_runs_by[key]
        ], job.brand, context)
        job.surface_verdicts = [
            surface_verdict(target.surface, target.model, inputs) for target, inputs in verdict_inputs.items()
        ]
        # 采样后:回答里出现、名单外的同名域名再查一次,标 B1 / B2——只提示,用户确认后才算官网(S5)
        job.adopt_domain_list(_look_alikes(job, all_runs, resolution))
        truncated = any(r.stop_reason in _TRUNCATION_REASONS for r in results.values())
        _finish(job, storage, truncated=truncated, sampling=_sampling_note(job, runs_by, offline_runs_by))


def _sampling_note(
    job: DiagnosisJob, runs_by: dict[RunsKey, list[SurfaceRun]], offline_runs_by: dict[RunsKey, list[SurfaceRun]],
) -> SamplingNote:
    """报告顶部的采样说明(S8.2):本单实际问了多少、有效多少、测了哪些接口与型号、官网名单情况、方法版本。
    结束时刻由 _finish 落定后再填。"""
    runs = [run for batch in runs_by.values() for run in batch]
    dl = job.domain_list
    return SamplingNote(
        n_prompts=len(job.prompts), n_targets=len(job.targets), runs_per_prompt=POLICY.runs_per_prompt,
        n_runs=len(runs), n_valid=sum(is_valid_answer(run) for run in runs),
        n_offline_runs=sum(len(batch) for batch in offline_runs_by.values()),
        started_at=job.started_at, finished_at=None, targets=list(job.targets),
        domains_confirmed=len(dl.confirmed_domains) if dl is not None else 0,
        domains_unconfirmed=len(dl.unconfirmed_domains) if dl is not None else 0,
        domains_at=dl.discovered_at if dl is not None else None,
        method_version=METHOD_VERSION,
    )


def _reset_progress(job: DiagnosisJob) -> None:
    """可重入(v0.4.9 幂等续跑):进度字段清零重算;已存的运行按幂等键复用。"""
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.total_runs = 0
    job.completed_runs = 0
    job.spent_usd = 0.0
    job.class_reports = []
    job.surface_verdicts = []
    job.started_at = datetime.now(UTC)
    job.last_result_at = None
    job.verify_total = job.verify_done = 0
    job.verify_stage = job.verify_started_at = None
    job.error_code, job.error_params, job.error = None, {}, None


def _sample(
    job: DiagnosisJob,
    dsn: str,
    adapter_of: dict[Target, SurfaceAdapter],
    *,
    replay: bool,
) -> dict[PlanKey, SamplingResult]:
    # 检索依赖对照(第 9 章挂靠):启用时,为支持 search_disabled 的对象追加闭卷腿计划——
    # 同一批题目、同采样参数,仅检索模式不同(模式即口径)
    dependency_targets = [
        target for target, adapter in adapter_of.items()
        if job.dependency_enabled and adapter.meta().capabilities.supports_search_disabled
    ]
    plans: list[PlanKey] = [(target, SearchMode.DEFAULT) for target in adapter_of] + [
        (target, SearchMode.SEARCH_DISABLED) for target in dependency_targets
    ]
    per_plan_budget = job.budget_usd / len(plans) if job.budget_usd else None
    policy = POLICY.model_copy(update={"budget_usd": per_plan_budget})
    job.total_runs = len(job.prompts) * policy.runs_per_prompt * len(plans)

    job.phase = "phase_sampling"
    progress_lock = threading.Lock()
    halt = threading.Event()  # 任一计划撞上账户问题即举旗:还没开始的计划不再发一次调用
    results: dict[PlanKey, SamplingResult] = {}

    def run_one(plan: PlanKey) -> None:
        if halt.is_set():
            return
        target, mode = plan
        adapter = adapter_of[target]
        plan_storage = Storage(dsn)  # psycopg 连接非线程安全:每计划独立连接
        try:
            prior = {  # 幂等键 (题 × surface × 型号 × 模式) 下已存的运行,续跑直接复用槽位
                prompt.prompt_id: reinterpret_account_errors([
                    r for r in plan_storage.runs_for_prompt(
                        prompt.prompt_id, surface=target.surface, model=target.model)
                    if r.search_mode == mode
                ])
                for prompt in job.prompts
            }
            with progress_lock:  # 复用的槽位也算进度(每题最多 runs_per_prompt 个)
                job.completed_runs += sum(
                    min(policy.runs_per_prompt, len({r.run_index for r in runs if not is_account_error(r)}))
                    for runs in prior.values()
                )
            sampler = RepeatSampler(_ProgressSink(plan_storage, job, progress_lock))
            result = sampler.run_plan(adapter, job.prompts, policy=policy, search_mode=mode, model=target.model,
                                      locale=job.locale, prior_runs=prior, allow_new_calls=not replay)
        except AccountProblem:
            halt.set()
            raise
        finally:
            plan_storage.close()
        with progress_lock:
            results[plan] = result
            job.spent_usd += result.total_cost_usd

    with ThreadPoolExecutor(max_workers=PLAN_CONCURRENCY) as pool:
        list(pool.map(run_one, plans))
    return results


def _load_runs(
    job: DiagnosisJob, storage: Storage,
) -> tuple[dict[RunsKey, list[SurfaceRun]], dict[RunsKey, list[SurfaceRun]]]:
    """取回每题每个对象的全部运行(旧账户问题行按 L1 现行规则重新解读,与续跑同一条规则);
    闭卷腿单独归档——检索模式即口径,禁止混入开卷指标的分母。"""
    runs_by: dict[RunsKey, list[SurfaceRun]] = {}
    offline_runs_by: dict[RunsKey, list[SurfaceRun]] = {}
    for prompt in job.prompts:
        for target in job.targets:
            all_runs = reinterpret_account_errors(
                storage.runs_for_prompt(prompt.prompt_id, surface=target.surface, model=target.model))
            runs_by[(prompt.prompt_id, target)] = [
                run for run in all_runs if run.search_mode != SearchMode.SEARCH_DISABLED
            ]
            offline_runs_by[(prompt.prompt_id, target)] = [
                run for run in all_runs if run.search_mode == SearchMode.SEARCH_DISABLED
            ]
    return runs_by, offline_runs_by


def _resolve_redirects(
    storage: Storage, adapters: dict[str, SurfaceAdapter], run_batches: list[list[SurfaceRun]],
) -> ResolutionContext:
    """跳转仪器(能力 citation_urls_redirect)的引用链接先还原终点(v0.4.26 S4,C2):按链接去重、库里复用,
    经 crawler/ 只取响应头。判定一律用终点;还原失败的那条引用记看不到。"""
    redirect_surfaces = frozenset(
        surface for surface, adapter in adapters.items() if adapter.meta().capabilities.citation_urls_redirect
    )
    urls = sorted({
        citation.url
        for runs in run_batches for run in runs
        if run.surface in redirect_surfaces and run.answer is not None
        for citation in run.answer.citations
    })
    if not urls:
        return ResolutionContext(redirect_surfaces, {})
    resolver = UrlResolver(storage)
    try:
        resolutions = resolver.resolve(urls)
    finally:
        resolver.close()
    log.info("citations_resolved", urls=len(urls),
             resolved=sum(r.final_url is not None for r in resolutions.values()))
    return ResolutionContext.from_resolutions(redirect_surfaces, resolutions)


def _discover_domains(job: DiagnosisJob) -> BrandDomainList:
    """采样前读主站首页、sitemap 与候选首页,找主站自己承认的地区站与官方渠道(engine/brand_domains.py)。"""
    discovery = BrandDomainDiscovery(load_platforms())
    try:
        return discovery.discover(job.website)
    finally:
        discovery.close()


def _look_alikes(job: DiagnosisJob, runs: list[SurfaceRun], context: ResolutionContext) -> BrandDomainList:
    """采样后:回答里出现、名单外的同名域名 → B1 / B2,只提示。"""
    assert job.domain_list is not None
    candidates = official_candidates(runs, job.brand, context)
    if not candidates:
        return job.domain_list
    discovery = BrandDomainDiscovery(load_platforms())
    try:
        return discovery.look_alikes(job.domain_list, candidates)
    finally:
        discovery.close()


def _page_checker(storage: Storage, progress: Callable[[int, int, str], None] | None = None) -> PageChecker:
    """网页核实器的装配点(S6.2–S6.4):四条退路在这里挑实现,核实器只认协议——渲染 = Playwright(装了才用);
    平台公开嵌入接口来自 config/platforms.yaml;配了 key 的平台官方接口来自环境变量(.env);公开存档 = 互联网档案馆。"""
    platforms = load_platforms()
    oembed = {host: p.oembed for p in platforms if p.oembed for host in p.hosts}
    fetchers = [f for f in [RedditFetcher.from_env(os.environ)] if f is not None]
    return PageChecker(storage, renderer=PlaywrightRenderer() if playwright_available() else None,
                       platform_fetchers=fetchers, oembed=oembed, archive=WaybackClient(), progress=progress)


def _primary_domain(brand: BrandSpec) -> str | None:
    """主站 = 用户在首页填的那个网址(官网名单的第一项);没有域名就没有主站引用率。"""
    return registrable(brand.domains[0])[1] if brand.domains else None


def _check_pages(
    job: DiagnosisJob, storage: Storage, runs: list[SurfaceRun], resolution: ResolutionContext,
) -> PageIndex:
    """打开 AI 引用与搜到的网页(S6.1,边界见 engine/page_check.py):官网的被引网页(独立核验,不看 robots)
    与第三方网页(写没写到你,尊重 robots)一起开;被引的先、搜到的后、同级按出现次数;竞品官网与官方渠道不用开。
    官网网页的结果同时并入 ACCESS 的 GENERIC_FETCH 层(按路径的探针,v0.4.13 条目 6)。"""
    from engine.detectors.source_class import url_host_class

    counts: Counter[str] = Counter()
    priority: dict[str, int] = {}
    own: set[str] = set()
    for run in runs:
        if not is_valid_answer(run):
            continue
        assert run.answer is not None
        sources = run.search_trace.retrieved_sources
        retrieved = [s.url for s in sources] if isinstance(sources, list) else []
        for raw_url, level in [(c.url, 0) for c in run.answer.citations] + [(u, 1) for u in retrieved]:
            final = resolution.final_url(run.surface, raw_url)
            if final is None:
                continue  # 还原失败:看不到,没法开
            known = url_host_class(final, job.brand, job.competitors)
            if known is not None and known[0] is not SourceClass.OWN_SITE:
                continue  # 竞品官网、官方渠道:按域名就定了,不用开
            if known is not None:
                if level:
                    continue  # 官网只核验被引的那些
                own.add(normalize_url(final))
            counts[final] += 1
            priority[final] = min(priority.get(final, level), level)
    requests = [
        PageRequest(url=url, priority=priority[url] * 1_000_000 + max(0, 1_000_000 - counts[url]),
                    respect_robots=normalize_url(url) not in own, allow_archive=normalize_url(url) not in own)
        for url in counts
    ]
    job.verify_started_at = datetime.now(UTC)
    job.verify_total, job.verify_done, job.verify_stage = len(requests), 0, "fetch"

    def progress(done: int, total: int, stage: str) -> None:
        """核实器的进度(可能来自工作线程):只改几个数给任务页看(S8.5);核对网页也是活动,别被当成「卡住了」。"""
        job.verify_done, job.verify_total, job.verify_stage = done, total, stage
        job.last_result_at = datetime.now(UTC)

    checker = _page_checker(storage, progress=progress)
    try:
        pages = checker.check(requests)
    finally:
        checker.close()
    if job.access_result is not None:
        job.access_result = with_path_probes(job.access_result, [
            PathProbe(url=normalized, http_status=check.http_status, error=check.error)
            for normalized, check in pages.checks.items() if normalized in own
        ])
    return pages


def _measure_class(
    job: DiagnosisJob,
    intent: PromptIntent,
    prompts: list[Prompt],
    adapter_of: dict[Target, SurfaceAdapter],
    results: dict[PlanKey, SamplingResult],
    observations: dict[RunsKey, list[RunObservations]],
    offline_observations: dict[RunsKey, list[RunObservations]],
) -> tuple[list[TargetMeasurement], dict[Target, ClassSurfaceMetric]]:
    """按各对象声明的能力分派到 L2 聚合:能回答的出全套指标(检索链路可观测的多一截生成端检索指标),
    跑过闭卷腿的出闭卷提及比例。"""
    prompt_ids = [p.prompt_id for p in prompts]
    competitor_names = [c.name for c in job.competitors]
    measurements: list[TargetMeasurement] = []
    closed_book: dict[Target, ClassSurfaceMetric] = {}
    for target, adapter in adapter_of.items():
        meta = adapter.meta()
        if not meta.capabilities.supports_citations:
            continue
        open_obs = [o for pid in prompt_ids for o in observations[(pid, target)]]
        measurements.append(measure_target(
            target, meta.provider, intent, open_obs, prompt_ids,
            stop_reason=results[(target, SearchMode.DEFAULT)].stop_reason,
            competitor_names=competitor_names,
            exposes_retrieval=meta.capabilities.supports_query_trace,
            primary_domain=_primary_domain(job.brand),
            channels_known=bool(job.brand.official_channels),
        ))
        closed_key = (target, SearchMode.SEARCH_DISABLED)
        if closed_key in results:
            closed_obs = [o for pid in prompt_ids for o in offline_observations[(pid, target)]]
            closed_book[target] = measure_closed_book(
                target, meta.provider, intent, closed_obs, prompt_ids, stop_reason=results[closed_key].stop_reason)
    return measurements, closed_book


def _finish(job: DiagnosisJob, storage: Storage, *, truncated: bool, sampling: SamplingNote) -> None:
    job.state = JobState.PARTIAL if truncated else JobState.DONE
    job.phase = "phase_partial" if truncated else "phase_done"
    job.finished_at = datetime.now(UTC)
    job.sampling = sampling.model_copy(update={"finished_at": job.finished_at})
    storage.upsert_job_snapshot(job.to_snapshot())
    storage.insert_report_snapshot(  # v0.4.16:报告档案只追加,「重新生成报告」即追加新版本
        job_id=job.job_id, fingerprint=job.fingerprint, code_version=current_code_version(),
        spent_usd=job.spent_usd, payload=job.report_payload(),
    )
    attach_latest_report(job, storage)  # 装回档案信息(第几版、生成时间)
    log.info("job_finished", job_id=job.job_id, state=job.state.value,
             runs=job.total_runs, spent_usd=round(job.spent_usd, 4))
