"""L4 任务执行器:只做编排(架构书附录 v0.4.24 第 5 节 ⑥),跑在后台线程,进度写回 job。

流程:ACCESS 审计(秒级先出)→ 题目落库 → 采样(计划 = 被测对象 × 检索模式,跨全部题目;计划间并发、
计划内严格串行;psycopg 连接非线程安全,每计划独立连接)→ 引用独立核验 → 逐题型:观测(L2 engine.observe)
→ 聚合(L2 engine.aggregate)→ 诊断(L3 diagnosis.assemble)→ 落档。任何未预期异常 → FAILED,页面如实展示。

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
from diagnosis.assemble import diagnose_class_targets
from diagnosis.citation_verify import CitationVerifier
from diagnosis.diagnose import access_finding, diagnose_class
from diagnosis.recommender import EvidenceEntry
from diagnosis.verdict import VerdictInput, surface_verdict
from engine.aggregate import ClassSurfaceMetric, TargetMeasurement, measure_closed_book, measure_target
from engine.models import (
    CitationEvidence,
    Prompt,
    PromptIntent,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import RunObservations, observe, persist_observations
from engine.official_domains import official_candidates
from engine.scheduler import (
    AccountProblem,
    RepeatSampler,
    SamplingResult,
    is_account_error,
    reinterpret_account_errors,
)
from engine.storage import Storage, StorageUnavailable
from report.archive import attach_latest_report
from report.jobs import (
    PLAN_CONCURRENCY,
    POLICY,
    ClassReport,
    DiagnosisJob,
    JobState,
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
        results = _sample(job, dsn, adapter_of, replay=replay)

        job.phase = "phase_verify"
        runs_by, offline_runs_by = _load_runs(job, storage)
        observations = {
            key: [observe(run, job.brand, job.competitors) for run in runs] for key, runs in runs_by.items()
        }
        offline_observations = {
            key: [observe(run, job.brand, job.competitors) for run in runs] for key, runs in offline_runs_by.items()
        }
        evidence_map = _verify_citations(job, storage, observations)

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
                evidence_map=evidence_map, brand_name=job.brand.name, registry=evidence,
            )
            for target, verdict in class_verdicts.items():
                verdict_inputs.setdefault(target, []).append(verdict)
            job.class_reports.append(ClassReport(
                intent=intent,
                prompt_texts=[p.text for p in prompts],
                diagnosis=diagnose_class(intent, access=access, surfaces=blocks),
            ))
        persist_observations(storage, [
            run for key in runs_by for run in runs_by[key] + offline_runs_by[key]
        ], job.brand)
        job.surface_verdicts = [
            surface_verdict(target.surface, target.model, inputs) for target, inputs in verdict_inputs.items()
        ]
        # 数据里出现、可能也是官网的网址(如各地区官网):只提示,用户确认后才算官网(v0.4.24 D4)
        job.official_candidates = official_candidates([run for runs in runs_by.values() for run in runs], job.brand)
        truncated = any(r.stop_reason in _TRUNCATION_REASONS for r in results.values())
        _finish(job, storage, truncated=truncated)


def _reset_progress(job: DiagnosisJob) -> None:
    """可重入(v0.4.9 幂等续跑):进度字段清零重算;已存的运行按幂等键复用。"""
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.total_runs = 0
    job.completed_runs = 0
    job.spent_usd = 0.0
    job.class_reports = []
    job.surface_verdicts = []
    job.official_candidates = []
    job.started_at = datetime.now(UTC)
    job.last_result_at = None
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
        ))
        closed_key = (target, SearchMode.SEARCH_DISABLED)
        if closed_key in results:
            closed_obs = [o for pid in prompt_ids for o in offline_observations[(pid, target)]]
            closed_book[target] = measure_closed_book(
                target, meta.provider, intent, closed_obs, prompt_ids, stop_reason=results[closed_key].stop_reason)
    return measurements, closed_book


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
             runs=job.total_runs, spent_usd=round(job.spent_usd, 4))
