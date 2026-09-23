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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import structlog

from adapters.base import SurfaceAdapter
from adapters.http_support import is_account_problem
from diagnosis import access_audit
from diagnosis.access_audit import PathProbe, with_path_probes
from diagnosis.citation_diag import VerificationSummary, diagnose_citation
from diagnosis.citation_verify import (
    CitationVerifier,
    VerificationOutcome,
    classify,
    normalize_url,
)
from diagnosis.diagnose import SurfaceDiagnosis, access_finding, diagnose_need, diagnose_surface
from diagnosis.findings import DiagnosisThresholds, StageStatus, interval_status
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.recommender import EvidenceEntry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_dependency import diagnose_retrieval_dependency
from diagnosis.retrieval_diag import diagnose_retrieval
from diagnosis.verdict import VerdictInput, band_of, surface_verdict
from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.matching import matches_any_brand_domain
from engine.detectors.query_trace import QueryTraceDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.runner import observe_runs
from engine.detectors.sentiment import SentimentDetector
from engine.metrics import ProportionEstimate, estimate_proportion
from engine.models import (
    UNKNOWN,
    BrandSpec,
    InformationNeed,
    Outcome,
    PromptOrigin,
    PromptVariant,
    RunErrorType,
    SearchMode,
    StopReason,
    SurfaceRun,
)
from engine.scheduler import (
    AccountProblem,
    NeedSurfaceMetric,
    SamplingResult,
    SequentialSampler,
    aggregate_need_surface,
)
from engine.storage import Storage
from engine.variants import generate_variants
from report.jobs import (
    PLAN_CONCURRENCY,
    TIERS,
    DiagnosisJob,
    JobState,
    NeedReport,
    Target,
    attach_latest_report,
    current_code_version,
)

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
        rank_detector = RetrievalRankDetector()

        def presence(run: SurfaceRun) -> bool | None:
            observation = rank_detector.detect(run, brand)
            return None if observation is None else observation.present

        return presence
    if capabilities.supports_citations:
        citation_detector = CitationDetector()

        def cited(run: SurfaceRun) -> bool | None:
            observation = citation_detector.detect(run, brand)
            return None if observation is None else observation.cited

        return cited
    raise ValueError(f"surface {adapter.meta().surface} 未声明可作收敛判据的能力")


def _variant_estimates(
    runs: list[SurfaceRun],
    variant_ids: list[str],
    hit: Callable[[SurfaceRun], bool | None],
) -> list[ProportionEstimate | None]:
    """按问法分组的估计(与问法同序),供需求层指标并列展示问法间分歧(v0.4.13 条目 2)。"""
    hits: dict[str, list[bool]] = {vid: [] for vid in variant_ids}
    for run in runs:
        value = hit(run)
        if value is not None and run.prompt_variant_id in hits:
            hits[run.prompt_variant_id].append(value)
    return [estimate_proportion(sum(v), len(v)) if v else None for v in hits.values()]


def _reinterpret_account_errors(runs: list[SurfaceRun]) -> list[SurfaceRun]:
    """v0.4.21 之前落库的账户问题行带的是 PERMANENT(当时还没有 ACCOUNT 这一类)。原始层
    不可改,读出时按 L1 现行的同一条翻译规则重新解读——只在内存,库里的行原样不动。"""
    reinterpreted = []
    for run in runs:
        error = run.error
        payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        body = payload.get("error_response_text")
        if (
            error is not None and error.error_type is RunErrorType.PERMANENT
            and is_account_problem(error.http_status, body if isinstance(body, str) else None)
        ):
            run = run.model_copy(update={"error": error.model_copy(update={"error_type": RunErrorType.ACCOUNT})})
        reinterpreted.append(run)
    return reinterpreted


def _mention_hit_fn(brand: BrandSpec):
    """闭卷腿(search_disabled)收敛判据:提及——该模式无引用机制,引用判据恒空。"""
    detector = BrandMentionDetector()

    def mentioned(run: SurfaceRun) -> bool | None:
        observation = detector.detect(run, brand)
        return None if observation is None else observation.mentioned

    return mentioned


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
        job.error = (
            f"{display}(型号 {exc.model})的 API 账户额度不足、欠费或 key 无效,检测已经停下,不会再花钱。"
            f"充值或换 key 后点「接着跑」,已经采集的结果不会重跑。平台原话:{exc.message}"
        )
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
    # 可重入(v0.4.9 幂等续跑):进度字段清零重算;已存的问法与运行按幂等键复用
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.completed_plans = 0
    job.spent_usd = 0.0
    job.need_reports = []
    job.surface_verdicts = []
    job.started_at = datetime.now(UTC)
    job.error = None
    job.access_result = access_audit.audit(job.website)

    storage = Storage(dsn)
    storage.ensure_schema()
    storage.upsert_job_snapshot(job.to_snapshot())
    # 被测对象 = 仪器 × 型号(v0.4.19):型号是口径的一部分,计划、样本、指标、诊断块一律按对象分开
    targets = job.targets
    adapter_of = {target: adapters[target.surface] for target in targets}
    generation_adapter = next(
        (a for a in adapter_of.values() if a.meta().capabilities.supports_citations), None
    )

    job.phase = "phase_variants"
    variants_by_need: dict[str, list[PromptVariant]] = {}
    for need in job.needs:
        storage.insert_information_need(need)
        variants = storage.variants_for_need(need.need_id)  # 续跑复用,不重烧起草费
        if not variants:
            variants = [
                PromptVariant(need_id=need.need_id, prompt_text=need.statement,
                              origin=PromptOrigin.USER)
            ]
            storage.insert_prompt_variant(variants[0])
        extra = job.variants_per_need - len(variants)
        if extra > 0 and generation_adapter is not None:
            fresh = generate_variants(generation_adapter, need, count=extra, locale=job.locale)
            for variant in fresh:
                storage.insert_prompt_variant(variant)
            variants += fresh
        variants_by_need[need.need_id] = variants

    # 检索依赖对照(第 9 章挂靠):启用时,为支持 search_disabled 的 surface
    # 追加闭卷腿计划——同一批问法、同采样策略,仅检索模式不同(模式即口径)
    dependency_targets = [
        target for target, adapter in adapter_of.items()
        if job.dependency_enabled and adapter.meta().capabilities.supports_search_disabled
    ]
    plans = [
        (need, target, SearchMode.DEFAULT) for need in job.needs for target in targets
    ] + [
        (need, target, SearchMode.SEARCH_DISABLED)
        for need in job.needs
        for target in dependency_targets
    ]
    job.total_plans = len(plans)
    per_plan_budget = job.budget_usd / len(plans) if job.budget_usd else None
    policy = TIERS[job.tier_id].policy.model_copy(update={"budget_usd": per_plan_budget})

    job.phase = "phase_sampling"
    progress_lock = threading.Lock()
    halt = threading.Event()  # 任一计划撞上账户问题即举旗:还没开始的计划不再发一次调用
    results: dict[tuple[str, Target, SearchMode], list[SamplingResult]] = {}

    def run_one(plan: tuple[InformationNeed, Target, SearchMode]) -> None:
        if halt.is_set():
            return
        need, target, mode = plan
        adapter = adapter_of[target]
        hit_fn = (
            _mention_hit_fn(job.brand)
            if mode is SearchMode.SEARCH_DISABLED
            else _hit_fn_for(adapter, job.brand)
        )
        plan_storage = Storage(dsn)  # psycopg 连接非线程安全:每计划独立连接
        try:
            sampler = SequentialSampler(plan_storage)
            plan_results = []
            for variant in variants_by_need[need.need_id]:
                prior = _reinterpret_account_errors([  # 幂等键 (问法 × surface × 型号 × 模式) 下已存的运行,续跑作前缀
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

    # 预取全部运行,收集品牌被引 URL,统一独立核验(全局去重 + 库缓存)
    job.phase = "phase_verify"
    runs_by: dict[tuple[str, Target], list[SurfaceRun]] = {}
    offline_runs_by: dict[tuple[str, Target], list[SurfaceRun]] = {}
    for need in job.needs:
        variant_ids = [v.variant_id for v in variants_by_need[need.need_id]]
        for target in targets:
            all_runs = [
                run
                for vid in variant_ids
                for run in storage.runs_for_variant(vid, surface=target.surface, model=target.model)
            ]
            # 闭卷腿单独归档:检索模式即口径,禁止混入开卷指标的分母
            runs_by[(need.need_id, target)] = [
                run for run in all_runs if run.search_mode != SearchMode.SEARCH_DISABLED
            ]
            offline_runs_by[(need.need_id, target)] = [
                run for run in all_runs if run.search_mode == SearchMode.SEARCH_DISABLED
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
    if job.access_result is not None:  # 按路径探针并入 ACCESS 的 GENERIC_FETCH 层(v0.4.13 条目 6)
        job.access_result = with_path_probes(job.access_result, [
            PathProbe(url=e.normalized_url, http_status=e.http_status, error=e.error)
            for e in evidence_map.values()
        ])

    job.phase = "phase_scoring"
    access = access_finding(job.access_result)
    mention_det = BrandMentionDetector()
    citation_det = CitationDetector()
    rank_det = RetrievalRankDetector()
    sentiment_det = SentimentDetector()
    query_det = QueryTraceDetector()
    truncated = False
    thresholds = DiagnosisThresholds()
    verdict_inputs: dict[Target, list[VerdictInput]] = {}  # 被测对象 → 逐需求观测(每个型号一句话)

    for need in job.needs:
        need_runs: list[SurfaceRun] = []
        blocks: list[SurfaceDiagnosis] = []
        variant_ids = [v.variant_id for v in variants_by_need[need.need_id]]

        # ── 第一遍:公共收集 + 裸检索类 surface 的检索指标(按 provider 归属)──
        # 裸检索通道是某家 provider 自己的仪器(如 perplexity 双通道),
        # 归入同 provider 的 AI 块作其检索视角;无家可归时独立成块。
        bare_by_provider: dict[str, tuple[Target, NeedSurfaceMetric, list[int]]] = {}
        for target, adapter in adapter_of.items():
            plan_results = results[(need.need_id, target, SearchMode.DEFAULT)]
            truncated |= any(r.stop_reason in _TRUNCATION_REASONS for r in plan_results)
            runs = runs_by[(need.need_id, target)]
            need_runs += runs + offline_runs_by[(need.need_id, target)]
            closed_key = (need.need_id, target, SearchMode.SEARCH_DISABLED)
            if closed_key in results:
                truncated |= any(
                    r.stop_reason in _TRUNCATION_REASONS for r in results[closed_key]
                )
            if adapter.meta().capabilities.supports_retrieval_rank:
                provider = adapter.meta().provider
                if provider in bare_by_provider:  # 单例槽位:一家只认一台裸检索仪器,多了不静默覆盖
                    raise ValueError(
                        f"{provider} 选了不止一个裸检索对象({bare_by_provider[provider][0].key}、"
                        f"{target.key}):借用关系无法确定,请只保留一个"
                    )
                bare_metric = aggregate_need_surface(plan_results, metric_label=rank_det.name)
                bare_ranks = [
                    r_obs.best_rank
                    for run in runs
                    if (r_obs := rank_det.detect(run, job.brand)) and r_obs.best_rank is not None
                ]
                bare_by_provider[provider] = (target, bare_metric, bare_ranks)

        # ── 第二遍:AI 通道逐个对象分诊(v0.4.11 分家;v0.4.19 每个型号各自的漏斗与药方)──
        absorbed_bare: set[Target] = set()
        for target, adapter in adapter_of.items():
            capabilities = adapter.meta().capabilities
            if not capabilities.supports_citations:
                continue
            plan_results = results[(need.need_id, target, SearchMode.DEFAULT)]
            runs = runs_by[(need.need_id, target)]

            citation_metric = aggregate_need_surface(plan_results, metric_label=citation_det.name)
            mention_obs = [
                m_obs for run in runs if (m_obs := mention_det.detect(run, job.brand))
            ]
            k_mention = sum(obs.mentioned for obs in mention_obs)
            mention_metric = citation_metric.model_copy(update={
                "metric_label": mention_det.name,
                "k_hits": k_mention,
                "estimate": estimate_proportion(k_mention, len(mention_obs)) if mention_obs else None,
                "per_variant": [],
                "variant_estimates": _variant_estimates(
                    runs, variant_ids,
                    lambda run: None if (o := mention_det.detect(run, job.brand)) is None else o.mentioned,
                ),
            })
            sentiment_counts = Counter(
                s_obs.sentiment for run in runs if (s_obs := sentiment_det.detect(run, job.brand))
            )
            need_urls = {normalize_url(u) for run in runs for u in brand_cited_urls(run)}
            outcomes = Counter(
                classify(evidence_map[n], job.brand.name) for n in need_urls if n in evidence_map
            )
            verification = VerificationSummary(
                attempted=sum(outcomes.values()),
                verified_match=outcomes[VerificationOutcome.VERIFIED_MATCH],
                content_mismatch=outcomes[VerificationOutcome.CONTENT_MISMATCH],
                fetch_blocked=outcomes[VerificationOutcome.FETCH_BLOCKED],
                unparsed=outcomes[VerificationOutcome.UNPARSED],
                unreachable=outcomes[VerificationOutcome.UNREACHABLE],
            )
            competitor_estimates = []
            for competitor in job.competitors:
                comp_obs = [
                    c_obs for run in runs if (c_obs := citation_det.detect(run, competitor))
                ]
                if comp_obs:
                    competitor_estimates.append((
                        competitor.name,
                        estimate_proportion(sum(o.cited for o in comp_obs), len(comp_obs)),
                    ))

            # RETRIEVAL:优先本仪器的生成端链路(与引用同仪器);
            # 无检索观测的 AI(如 perplexity 生成通道)借同 provider 的裸检索通道
            if capabilities.supports_query_trace:
                presence_obs = [p_obs for run in runs if (p_obs := rank_det.detect(run, job.brand))]
                presence_metric = None
                if presence_obs:
                    k_presence = sum(obs.present for obs in presence_obs)
                    presence_metric = aggregate_need_surface(
                        plan_results, metric_label=rank_det.name
                    ).model_copy(update={  # 开卷腿收敛判据是引用,在场数须按判定器重算
                        "k_hits": k_presence,
                        "estimate": estimate_proportion(k_presence, len(presence_obs)),
                        "per_variant": [],
                        "variant_estimates": _variant_estimates(
                            runs, variant_ids,
                            lambda run: None if (o := rank_det.detect(run, job.brand)) is None else o.present,
                        ),
                    })
                query_obs = [q_obs for run in runs if (q_obs := query_det.detect(run, job.brand))]
                gen_cited_obs = [
                    gc_obs for run in runs if (gc_obs := citation_det.detect(run, job.brand))
                ]
                triggered_flags = [
                    run.search_trace.search_triggered
                    for run in runs
                    if run.outcome is Outcome.SUCCESS and run.search_trace.search_triggered != UNKNOWN
                ]
                retrieval = diagnose_generative_retrieval(
                    presence_metric,
                    [obs.best_rank for obs in presence_obs if obs.best_rank is not None],
                    estimate_proportion(sum(o.present for o in query_obs), len(query_obs))
                    if query_obs else None,
                    estimate_proportion(sum(o.cited for o in gen_cited_obs), len(gen_cited_obs))
                    if gen_cited_obs else None,
                    estimate_proportion(sum(bool(f) for f in triggered_flags), len(triggered_flags))
                    if triggered_flags else None,
                )
            elif adapter.meta().provider in bare_by_provider:
                bare_target, bare_metric, bare_ranks = bare_by_provider[adapter.meta().provider]
                retrieval = diagnose_retrieval(bare_metric, bare_ranks)
                absorbed_bare.add(bare_target)
            else:
                retrieval = diagnose_retrieval(None, [])

            # 检索依赖对照(第 9 章):本对象的开卷/闭卷提及率区间并比
            dependency = None
            closed_key = (need.need_id, target, SearchMode.SEARCH_DISABLED)
            if closed_key in results:
                closed_metric = aggregate_need_surface(
                    results[closed_key], metric_label=mention_det.name
                )
                dependency = diagnose_retrieval_dependency(mention_metric, closed_metric)

            block = diagnose_surface(
                provider=citation_metric.provider,
                surface=citation_metric.surface,
                model=citation_metric.model,
                access=access,
                retrieval=retrieval,
                citation=diagnose_citation(citation_metric, competitor_estimates,
                                           verification=verification),
                representation=diagnose_representation(
                    need.intent, mention_metric, dict(sentiment_counts)
                ),
                intent=need.intent,
                registry=evidence,
                retrieval_dependency=dependency,
            )
            blocks.append(block)
            # 每家 AI 一句话的原料:提及档位 = 提及率区间三态的直接映射;官网被用上 = 检索或引用任一 PASS
            mention_band = None
            if mention_metric.estimate is not None:
                mention_status, _ = interval_status(
                    mention_metric.estimate,
                    low_confidence=mention_metric.accounting.low_confidence,
                    fail_ci_high=thresholds.mention_fail_ci_high,
                    pass_ci_low=thresholds.mention_pass_ci_low,
                )
                mention_band = band_of(mention_status)
            site_findings = [f for f in block.findings[:2] if f.status is not StageStatus.NOT_OBSERVABLE]
            verdict_inputs.setdefault(target, []).append(VerdictInput(
                intent=need.intent,
                mention=mention_band,
                site_used=any(f.status is StageStatus.PASS for f in site_findings) if site_findings else None,
            ))

        # ── 第三遍:未被吸收的裸检索 surface 独立成块(仅检索视角)──────
        for provider, (bare_target, bare_metric, bare_ranks) in bare_by_provider.items():
            if bare_target in absorbed_bare:
                continue
            blocks.append(diagnose_surface(
                provider=provider,
                surface=bare_target.surface,
                model=bare_target.model,
                access=access,
                retrieval=diagnose_retrieval(bare_metric, bare_ranks),
                citation=diagnose_citation(None, [], verification=None),
                representation=diagnose_representation(need.intent, None, {}),
                intent=need.intent,
                registry=evidence,
            ))

        observe_runs(storage, need_runs,
                     [mention_det, citation_det, rank_det, sentiment_det, query_det], job.brand)
        job.need_reports.append(NeedReport(
            need=need,
            variant_texts=[v.prompt_text for v in variants_by_need[need.need_id]],
            diagnosis=diagnose_need(need, access=access, surfaces=blocks),
        ))

    job.surface_verdicts = [
        surface_verdict(target.surface, target.model, inputs) for target, inputs in verdict_inputs.items()
    ]
    job.state = JobState.PARTIAL if truncated else JobState.DONE
    job.phase = "phase_partial" if truncated else "phase_done"
    job.finished_at = datetime.now(UTC)
    storage.upsert_job_snapshot(job.to_snapshot())
    storage.insert_report_snapshot(  # v0.4.16:报告档案只追加,「重新生成报告」即追加新版本
        job_id=job.job_id, fingerprint=job.fingerprint, code_version=current_code_version(),
        spent_usd=job.spent_usd, payload=job.report_payload(),
    )
    attach_latest_report(job, storage)  # 装回档案信息(第几版、生成时间)
    storage.close()
    log.info("job_finished", job_id=job.job_id, state=job.state.value,
             plans=job.total_plans, spent_usd=round(job.spent_usd, 4))
