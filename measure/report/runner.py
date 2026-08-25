"""L4 任务执行器:第 3 章走查的产品化执行,跑在后台线程,进度写回 job。

流程:ACCESS 审计(秒级先出)→ 逐需求生成问法变体 → 采样计划线程池并发
(计划内严格串行,计划间并发;psycopg 连接非线程安全,每计划独立连接)
→ 判定与指标 → 逐需求四阶段诊断。任何未预期异常 → FAILED,页面如实展示。

surface 的收敛判据按能力声明泛型分派(检索排名能力 → 在场判定;
引用能力 → 被引判定),本模块不含任何 surface 名字符串。
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import structlog

from adapters.base import SurfaceAdapter
from diagnosis import access_audit
from diagnosis.citation_diag import VerificationSummary, diagnose_citation
from diagnosis.citation_verify import (
    CitationVerifier,
    brand_verified,
    is_soft_miss,
    normalize_url,
)
from diagnosis.diagnose import access_finding, diagnose_need
from diagnosis.recommender import EvidenceEntry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import diagnose_retrieval
from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.matching import matches_any_brand_domain
from engine.detectors.citation import CitationDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import observe_runs
from engine.detectors.sentiment import SentimentDetector
from engine.metrics import estimate_proportion
from engine.models import (
    BrandSpec,
    InformationNeed,
    PromptOrigin,
    PromptVariant,
    StopReason,
    SurfaceRun,
)
from engine.scheduler import (
    NeedSurfaceMetric,
    SamplingResult,
    SequentialSampler,
    aggregate_need_surface,
)
from engine.storage import Storage
from engine.variants import generate_variants
from report.jobs import PLAN_CONCURRENCY, TIERS, DiagnosisJob, JobState, NeedReport

log = structlog.get_logger("report.runner")

_TRUNCATION_REASONS = (
    StopReason.BUDGET_EXHAUSTED,
    StopReason.FAILURE_RATE_EXCEEDED,
    StopReason.SURFACE_DISABLED,
)


def _hit_fn_for(adapter: SurfaceAdapter, brand: BrandSpec):
    """按能力声明选择收敛判据(不认 surface 名,只认能力)。"""
    capabilities = adapter.meta().capabilities
    if capabilities.supports_retrieval_rank:
        detector = RetrievalRankDetector()

        def presence(run: SurfaceRun) -> bool | None:
            observation = detector.detect(run, brand)
            return None if observation is None else observation.present

        return presence
    if capabilities.supports_citations:
        detector = CitationDetector()

        def cited(run: SurfaceRun) -> bool | None:
            observation = detector.detect(run, brand)
            return None if observation is None else observation.cited

        return cited
    raise ValueError(f"surface {adapter.meta().surface} 未声明可作收敛判据的能力")


def execute_job(
    *,
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
) -> None:
    try:
        _execute(job, adapters=adapters, dsn=dsn, evidence=evidence)
    except Exception as exc:  # 失败不静默:状态页如实展示
        log.error("job_failed", job_id=job.job_id, error=f"{type(exc).__name__}: {exc}")
        job.state = JobState.FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.phase = "phase_failed"


def _execute(
    job: DiagnosisJob,
    *,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
) -> None:
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.access_result = access_audit.audit(job.website)

    storage = Storage(dsn)
    storage.ensure_schema()
    selected = {name: adapters[name] for name in job.surfaces}
    generation_adapter = next(
        (a for a in selected.values() if a.meta().capabilities.supports_citations), None
    )

    job.phase = "phase_variants"
    variants_by_need: dict[str, list[PromptVariant]] = {}
    for need in job.needs:
        storage.insert_information_need(need)
        variants = [
            PromptVariant(need_id=need.need_id, prompt_text=need.statement, origin=PromptOrigin.USER)
        ]
        extra = job.variants_per_need - 1
        if extra > 0 and generation_adapter is not None:
            variants += generate_variants(generation_adapter, need, count=extra, locale=job.locale)
        for variant in variants:
            storage.insert_prompt_variant(variant)
        variants_by_need[need.need_id] = variants

    plans = [(need, surface_name) for need in job.needs for surface_name in selected]
    job.total_plans = len(plans)
    per_plan_budget = job.budget_usd / len(plans) if job.budget_usd else None
    policy = TIERS[job.tier_id].policy.model_copy(update={"budget_usd": per_plan_budget})

    job.phase = "phase_sampling"
    progress_lock = threading.Lock()
    results: dict[tuple[str, str], list[SamplingResult]] = {}

    def run_one(plan: tuple[InformationNeed, str]) -> None:
        need, surface_name = plan
        adapter = selected[surface_name]
        hit_fn = _hit_fn_for(adapter, job.brand)
        plan_storage = Storage(dsn)  # psycopg 连接非线程安全:每计划独立连接
        try:
            sampler = SequentialSampler(plan_storage)
            plan_results = [
                sampler.run_plan(adapter, need, variant, hit_fn=hit_fn,
                                 policy=policy, locale=job.locale)
                for variant in variants_by_need[need.need_id]
            ]
        finally:
            plan_storage.close()
        with progress_lock:
            results[(need.need_id, surface_name)] = plan_results
            job.completed_plans += 1
            job.spent_usd += sum(r.total_cost_usd for r in plan_results)

    with ThreadPoolExecutor(max_workers=PLAN_CONCURRENCY) as pool:
        list(pool.map(run_one, plans))

    # 预取全部运行,收集品牌被引 URL,统一独立核验(全局去重 + 库缓存)
    job.phase = "phase_verify"
    runs_by: dict[tuple[str, str], list[SurfaceRun]] = {}
    for need in job.needs:
        variant_ids = [v.variant_id for v in variants_by_need[need.need_id]]
        for surface_name in selected:
            runs_by[(need.need_id, surface_name)] = [
                run
                for vid in variant_ids
                for run in storage.runs_for_variant(vid, surface=surface_name)
            ]

    def brand_cited_urls(run: SurfaceRun) -> list[str]:
        if run.answer is None or not run.answer.citations_available:
            return []
        return [
            c.url for c in run.answer.citations
            if matches_any_brand_domain(c.domain, job.brand)
        ]

    all_cited_urls = sorted({
        url for runs in runs_by.values() for run in runs for url in brand_cited_urls(run)
    })
    verifier = CitationVerifier(storage)
    try:
        evidence_map = verifier.verify(all_cited_urls, [job.brand])
    finally:
        verifier.close()

    job.phase = "phase_scoring"
    access = access_finding(job.access_result)
    mention_det = BrandMentionDetector()
    citation_det = CitationDetector()
    rank_det = RetrievalRankDetector()
    sentiment_det = SentimentDetector()
    truncated = False

    for need in job.needs:
        citation_metric: NeedSurfaceMetric | None = None
        mention_metric: NeedSurfaceMetric | None = None
        retrieval_metric: NeedSurfaceMetric | None = None
        verification: VerificationSummary | None = None
        best_ranks: list[int] = []
        sentiment_counts: Counter = Counter()
        competitor_estimates: list = []
        need_runs: list[SurfaceRun] = []

        for surface_name, adapter in selected.items():
            plan_results = results[(need.need_id, surface_name)]
            truncated |= any(r.stop_reason in _TRUNCATION_REASONS for r in plan_results)
            runs = runs_by[(need.need_id, surface_name)]
            need_runs += runs
            capabilities = adapter.meta().capabilities
            if capabilities.supports_retrieval_rank and retrieval_metric is None:
                retrieval_metric = aggregate_need_surface(plan_results, metric_label=rank_det.name)
                best_ranks = [
                    obs.best_rank
                    for run in runs
                    if (obs := rank_det.detect(run, job.brand)) and obs.best_rank is not None
                ]
            elif capabilities.supports_citations and citation_metric is None:
                citation_metric = aggregate_need_surface(plan_results, metric_label=citation_det.name)
                mention_obs = [
                    obs for run in runs if (obs := mention_det.detect(run, job.brand))
                ]
                k_mention = sum(obs.mentioned for obs in mention_obs)
                mention_metric = NeedSurfaceMetric(
                    information_need_id=need.need_id,
                    provider=citation_metric.provider,
                    surface=citation_metric.surface,
                    model=citation_metric.model,
                    metric_label=mention_det.name,
                    k_hits=k_mention,
                    estimate=estimate_proportion(k_mention, len(mention_obs)) if mention_obs else None,
                    accounting=citation_metric.accounting,
                    stop_reasons=citation_metric.stop_reasons,
                    per_variant=[],
                )
                sentiment_counts = Counter(
                    obs.sentiment for run in runs if (obs := sentiment_det.detect(run, job.brand))
                )
                cited_runs_urls = [(run, brand_cited_urls(run)) for run in runs]
                need_urls = {normalize_url(u) for _, us in cited_runs_urls for u in us}
                k_verified = sum(
                    any(
                        brand_verified(evidence_map[normalize_url(u)], job.brand.name)
                        for u in us
                        if normalize_url(u) in evidence_map
                    )
                    for _, us in cited_runs_urls
                )
                need_evidence = [evidence_map[n] for n in need_urls if n in evidence_map]
                verification = VerificationSummary(
                    estimate=(
                        estimate_proportion(k_verified, citation_metric.estimate.n)
                        if citation_metric.estimate is not None else None
                    ),
                    checked_urls=len(need_evidence),
                    unreachable=sum(not e.reachable for e in need_evidence),
                    soft_misses=sum(is_soft_miss(e, job.brand.name) for e in need_evidence),
                )
                for competitor in job.competitors:
                    comp_obs = [
                        obs for run in runs if (obs := citation_det.detect(run, competitor))
                    ]
                    if comp_obs:
                        competitor_estimates.append((
                            competitor.name,
                            estimate_proportion(sum(o.cited for o in comp_obs), len(comp_obs)),
                        ))

        observe_runs(storage, need_runs, [mention_det, citation_det, rank_det, sentiment_det], job.brand)
        diagnosis = diagnose_need(
            need,
            access=access,
            retrieval=diagnose_retrieval(retrieval_metric, best_ranks),
            citation=diagnose_citation(citation_metric, competitor_estimates,
                                       verification=verification),
            representation=diagnose_representation(
                need.intent, mention_metric, dict(sentiment_counts)
            ),
            registry=evidence,
        )
        job.need_reports.append(NeedReport(
            need=need,
            variant_texts=[v.prompt_text for v in variants_by_need[need.need_id]],
            diagnosis=diagnosis,
        ))

    storage.close()
    job.state = JobState.PARTIAL if truncated else JobState.DONE
    job.phase = "phase_partial" if truncated else "phase_done"
    job.finished_at = datetime.now(UTC)
    log.info("job_finished", job_id=job.job_id, state=job.state.value,
             plans=job.total_plans, spent_usd=round(job.spent_usd, 4))
