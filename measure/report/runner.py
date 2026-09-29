"""L4 任务执行器:只做编排(架构书附录 v0.4.24 第 5 节 ⑥),跑在后台线程,进度写回 job。

流程:ACCESS 审计(秒级先出)→ 准备问法 → 采样(计划间并发、计划内严格串行;psycopg 连接
非线程安全,每计划独立连接)→ 引用独立核验 → 逐题:观测(L2 engine.observe)→ 聚合
(L2 engine.aggregate)→ 诊断(L3 diagnosis.assemble)→ 落档。任何未预期异常 → FAILED,页面如实展示。

算数在 L2、下结论在 L3;本模块不含判定规则,也不含任何 surface 名字符串。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import structlog

from adapters.base import SurfaceAdapter
from diagnosis import access_audit
from diagnosis.access_audit import PathProbe, with_path_probes
from diagnosis.assemble import diagnose_need_targets
from diagnosis.citation_verify import CitationVerifier
from diagnosis.diagnose import access_finding, diagnose_need
from diagnosis.recommender import EvidenceEntry
from diagnosis.verdict import VerdictInput, surface_verdict
from engine.aggregate import (
    BareRetrieval,
    NeedSurfaceMetric,
    TargetMeasurement,
    measure_bare,
    measure_closed_book,
    measure_target,
)
from engine.models import (
    CitationEvidence,
    InformationNeed,
    PromptOrigin,
    PromptVariant,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import RunObservations, convergence_hit, observe, persist_observations
from engine.scheduler import AccountProblem, SamplingResult, SequentialSampler, reinterpret_account_errors
from engine.storage import Storage, StorageUnavailable
from engine.variants import generate_variants
from report.jobs import (
    PLAN_CONCURRENCY,
    TIERS,
    DiagnosisJob,
    JobState,
    NeedReport,
    attach_latest_report,
    current_code_version,
)

log = structlog.get_logger("report.runner")

_TRUNCATION_REASONS = (
    StopReason.BUDGET_EXHAUSTED,
    StopReason.FAILURE_RATE_EXCEEDED,
    StopReason.SURFACE_DISABLED,
)

PlanKey = tuple[str, Target, SearchMode]  # (需求, 被测对象, 检索模式)
RunsKey = tuple[str, Target]  # (需求, 被测对象)


class _ProgressSink:
    """包一层存储:每落一条样本就给任务盖个时间戳,任务页据此显示「最近一次新结果在几分钟前」,
    超过阈值就报 JOB_STALLED(v0.4.22)。"""

    def __init__(self, storage: Storage, job: DiagnosisJob, lock: threading.Lock) -> None:
        self._storage, self._job, self._lock = storage, job, lock

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self._storage.insert_surface_run(run)
        with self._lock:
            self._job.last_result_at = datetime.now(UTC)


def execute_job(
    *,
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
) -> None:
    try:
        _execute(job, adapters=adapters, dsn=dsn, evidence=evidence)
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
) -> None:
    _reset_progress(job)
    job.access_result = access_audit.audit(job.website)
    with Storage(dsn) as storage:
        storage.ensure_schema()
        storage.upsert_job_snapshot(job.to_snapshot())
        # 被测对象 = 仪器 × 型号(v0.4.19):型号是口径的一部分,计划、样本、指标、诊断块一律按对象分开
        adapter_of = {target: adapters[target.surface] for target in job.targets}
        variants_by_need = _prepare_variants(job, storage, adapter_of)
        results = _sample(job, dsn, adapter_of, variants_by_need)

        job.phase = "phase_verify"
        runs_by, offline_runs_by = _load_runs(job, storage, variants_by_need)
        observations = {
            key: [observe(run, job.brand, job.competitors) for run in runs] for key, runs in runs_by.items()
        }
        evidence_map = _verify_citations(job, storage, observations)

        job.phase = "phase_scoring"
        access = access_finding(job.access_result)
        verdict_inputs: dict[Target, list[VerdictInput]] = {}  # 被测对象 → 逐题原料(每个型号一句话)
        for need in job.needs:
            measurements, bare_channels, closed_book = _measure_need(
                job, need, adapter_of, results, observations, variants_by_need)
            blocks, need_verdicts = diagnose_need_targets(
                intent=need.intent, access=access, measurements=measurements, bare_channels=bare_channels,
                closed_book=closed_book, evidence_map=evidence_map, brand_name=job.brand.name,
                registry=evidence,
            )
            for target, verdict in need_verdicts.items():
                verdict_inputs.setdefault(target, []).append(verdict)
            persist_observations(storage, [
                run for target in adapter_of
                for run in runs_by[(need.need_id, target)] + offline_runs_by[(need.need_id, target)]
            ], job.brand)
            job.need_reports.append(NeedReport(
                need=need,
                variant_texts=[v.prompt_text for v in variants_by_need[need.need_id]],
                diagnosis=diagnose_need(need, access=access, surfaces=blocks),
            ))
        job.surface_verdicts = [
            surface_verdict(target.surface, target.model, inputs) for target, inputs in verdict_inputs.items()
        ]
        truncated = any(r.stop_reason in _TRUNCATION_REASONS for plan in results.values() for r in plan)
        _finish(job, storage, truncated=truncated)


def _reset_progress(job: DiagnosisJob) -> None:
    """可重入(v0.4.9 幂等续跑):进度字段清零重算;已存的问法与运行按幂等键复用。"""
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.completed_plans = 0
    job.spent_usd = 0.0
    job.need_reports = []
    job.surface_verdicts = []
    job.started_at = datetime.now(UTC)
    job.last_result_at = None
    job.error_code, job.error_params, job.error = None, {}, None


def _prepare_variants(
    job: DiagnosisJob, storage: Storage, adapter_of: dict[Target, SurfaceAdapter]
) -> dict[str, list[PromptVariant]]:
    """每道题的问法:续跑复用库里已有的(不重烧起草费),不够数再让 AI 改写补齐。"""
    generation_adapter = next(
        (a for a in adapter_of.values() if a.meta().capabilities.supports_citations), None
    )
    job.phase = "phase_variants"
    variants_by_need: dict[str, list[PromptVariant]] = {}
    for need in job.needs:
        storage.insert_information_need(need)
        variants = storage.variants_for_need(need.need_id)
        if not variants:
            variants = [
                PromptVariant(need_id=need.need_id, prompt_text=need.statement, origin=PromptOrigin.USER)
            ]
            storage.insert_prompt_variant(variants[0])
        extra = job.variants_per_need - len(variants)
        if extra > 0 and generation_adapter is not None:
            fresh = generate_variants(
                generation_adapter, need, count=extra, locale=job.locale,
                ledger=lambda run: storage.insert_aux_call(job_id=job.job_id, task="变体生成", run=run),
            )
            for variant in fresh:
                storage.insert_prompt_variant(variant)
            variants += fresh
        variants_by_need[need.need_id] = variants
    job.spent_usd = storage.aux_spent_usd(job.job_id)  # 辅助调用(出题、改写问法)全部记在账本里,续跑也不丢
    return variants_by_need


def _sample(
    job: DiagnosisJob,
    dsn: str,
    adapter_of: dict[Target, SurfaceAdapter],
    variants_by_need: dict[str, list[PromptVariant]],
) -> dict[PlanKey, list[SamplingResult]]:
    # 检索依赖对照(第 9 章挂靠):启用时,为支持 search_disabled 的对象追加闭卷腿计划——
    # 同一批问法、同采样策略,仅检索模式不同(模式即口径)
    dependency_targets = [
        target for target, adapter in adapter_of.items()
        if job.dependency_enabled and adapter.meta().capabilities.supports_search_disabled
    ]
    plans = [
        (need, target, SearchMode.DEFAULT) for need in job.needs for target in adapter_of
    ] + [
        (need, target, SearchMode.SEARCH_DISABLED) for need in job.needs for target in dependency_targets
    ]
    job.total_plans = len(plans)
    per_plan_budget = job.budget_usd / len(plans) if job.budget_usd else None
    policy = TIERS[job.tier_id].policy.model_copy(update={"budget_usd": per_plan_budget})

    job.phase = "phase_sampling"
    progress_lock = threading.Lock()
    halt = threading.Event()  # 任一计划撞上账户问题即举旗:还没开始的计划不再发一次调用
    results: dict[PlanKey, list[SamplingResult]] = {}

    def run_one(plan: tuple[InformationNeed, Target, SearchMode]) -> None:
        if halt.is_set():
            return
        need, target, mode = plan
        adapter = adapter_of[target]
        hit_fn = convergence_hit(adapter.meta().capabilities, mode, job.brand)
        plan_storage = Storage(dsn)  # psycopg 连接非线程安全:每计划独立连接
        try:
            sampler = SequentialSampler(_ProgressSink(plan_storage, job, progress_lock))
            plan_results = []
            for variant in variants_by_need[need.need_id]:
                prior = reinterpret_account_errors([  # 幂等键 (问法 × surface × 型号 × 模式) 下已存的运行,续跑作前缀
                    r for r in plan_storage.runs_for_variant(
                        variant.variant_id, surface=target.surface, model=target.model)
                    if r.search_mode == mode
                ])
                plan_results.append(
                    sampler.run_plan(adapter, need, variant, hit_fn=hit_fn, policy=policy,
                                     search_mode=mode, model=target.model, locale=job.locale,
                                     prior_runs=prior)
                )
        except AccountProblem:
            halt.set()
            raise
        finally:
            plan_storage.close()
        with progress_lock:
            results[(need.need_id, target, mode)] = plan_results
            job.completed_plans += 1
            job.spent_usd += sum(r.total_cost_usd for r in plan_results)

    with ThreadPoolExecutor(max_workers=PLAN_CONCURRENCY) as pool:
        list(pool.map(run_one, plans))
    return results


def _load_runs(
    job: DiagnosisJob, storage: Storage, variants_by_need: dict[str, list[PromptVariant]]
) -> tuple[dict[RunsKey, list[SurfaceRun]], dict[RunsKey, list[SurfaceRun]]]:
    """取回每题每个对象的全部运行;闭卷腿单独归档——检索模式即口径,禁止混入开卷指标的分母。"""
    runs_by: dict[RunsKey, list[SurfaceRun]] = {}
    offline_runs_by: dict[RunsKey, list[SurfaceRun]] = {}
    for need in job.needs:
        variant_ids = [v.variant_id for v in variants_by_need[need.need_id]]
        for target in job.targets:
            all_runs = [
                run
                for vid in variant_ids
                for run in storage.runs_for_variant(vid, surface=target.surface, model=target.model)
            ]
            runs_by[(need.need_id, target)] = [
                run for run in all_runs if run.search_mode != SearchMode.SEARCH_DISABLED
            ]
            offline_runs_by[(need.need_id, target)] = [
                run for run in all_runs if run.search_mode == SearchMode.SEARCH_DISABLED
            ]
    return runs_by, offline_runs_by


def _verify_citations(
    job: DiagnosisJob, storage: Storage, observations: dict[RunsKey, list[RunObservations]]
) -> dict[str, CitationEvidence]:
    """品牌被引 URL 全局去重后统一独立核验(库缓存);按路径的结果并入 ACCESS 的 GENERIC_FETCH 层
    (v0.4.13 条目 6)。"""
    urls = sorted({url for batch in observations.values() for o in batch for url in o.brand_cited_urls})
    verifier = CitationVerifier(storage)
    try:
        evidence_map = verifier.verify(urls, [job.brand])
    finally:
        verifier.close()
    if job.access_result is not None:
        job.access_result = with_path_probes(job.access_result, [
            PathProbe(url=e.normalized_url, http_status=e.http_status, error=e.error)
            for e in evidence_map.values()
        ])
    return evidence_map


def _measure_need(
    job: DiagnosisJob,
    need: InformationNeed,
    adapter_of: dict[Target, SurfaceAdapter],
    results: dict[PlanKey, list[SamplingResult]],
    observations: dict[RunsKey, list[RunObservations]],
    variants_by_need: dict[str, list[PromptVariant]],
) -> tuple[list[TargetMeasurement], list[BareRetrieval], dict[Target, NeedSurfaceMetric]]:
    """按各对象声明的能力分派到 L2 聚合:能回答的出全套指标,有检索排名能力的出裸检索指标,
    跑过闭卷腿的出闭卷提及率。"""
    variant_ids = [v.variant_id for v in variants_by_need[need.need_id]]
    competitor_names = [c.name for c in job.competitors]
    measurements: list[TargetMeasurement] = []
    bare_channels: list[BareRetrieval] = []
    closed_book: dict[Target, NeedSurfaceMetric] = {}
    for target, adapter in adapter_of.items():
        meta = adapter.meta()
        plan_results = results[(need.need_id, target, SearchMode.DEFAULT)]
        target_obs = observations[(need.need_id, target)]
        if meta.capabilities.supports_retrieval_rank:
            bare_channels.append(measure_bare(target, meta.provider, plan_results, target_obs))
        if meta.capabilities.supports_citations:
            measurements.append(measure_target(
                target, meta.provider, plan_results, target_obs, variant_ids,
                competitor_names=competitor_names,
                exposes_retrieval=meta.capabilities.supports_query_trace,
            ))
            closed_key = (need.need_id, target, SearchMode.SEARCH_DISABLED)
            if closed_key in results:
                closed_book[target] = measure_closed_book(results[closed_key])
    return measurements, bare_channels, closed_book


def _finish(job: DiagnosisJob, storage: Storage, *, truncated: bool) -> None:
    job.state = JobState.PARTIAL if truncated else JobState.DONE
    job.phase = "phase_partial" if truncated else "phase_done"
    job.finished_at = datetime.now(UTC)
    storage.upsert_job_snapshot(job.to_snapshot())
    storage.insert_report_snapshot(  # v0.4.16:报告档案只追加,「重新生成报告」即追加新版本
        job_id=job.job_id, fingerprint=job.fingerprint, code_version=current_code_version(),
        spent_usd=job.spent_usd, payload=job.report_payload(),
    )
    attach_latest_report(job, storage)  # 装回档案信息(第几版、生成时间)
    log.info("job_finished", job_id=job.job_id, state=job.state.value,
             plans=job.total_plans, spent_usd=round(job.spent_usd, 4))
