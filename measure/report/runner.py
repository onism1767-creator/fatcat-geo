"""L4 任务执行器:只做编排(架构书附录 v0.4.24 第 5 节 ⑥),跑在后台线程,进度写回 job。

流程:ACCESS 审计(秒级先出)→ 题目落库 → 采样(计划 = 被测对象 × 检索模式,跨全部题目;计划间并发、
计划内严格串行;psycopg 连接非线程安全,每计划独立连接)→ 引用独立核验 → 逐题型:观测(L2 engine.observe)
→ 聚合(L2 engine.aggregate)→ 诊断(L3 diagnosis.assemble)→ 落档。任何未预期异常 → FAILED,页面如实展示。

算数在 L2、下结论在 L3;本模块不含判定规则,也不含任何 surface 名字符串。
"""

from __future__ import annotations

import os
import random
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import structlog

from adapters.base import AdapterAccountError, AdapterPermanentError, SurfaceAdapter
from crawler.reddit_api import RedditFetcher
from crawler.render import PlaywrightRenderer, playwright_available
from crawler.wayback import WaybackClient
from diagnosis import access_audit
from diagnosis.access_audit import PathProbe, with_path_probes
from diagnosis.assemble import diagnose_class_targets
from diagnosis.diagnose import access_finding, diagnose_class
from diagnosis.recommender import EvidenceEntry
from diagnosis.verdict import VerdictInput, surface_verdict
from engine.aggregate import (
    ClassSurfaceMetric,
    TargetMeasurement,
    measure_closed_book,
    measure_target,
    tally_spellings,
)
from engine.brand_domains import BrandDomainDiscovery, load_platforms
from engine.cache import CACHE_HOURS, fill_from_cache
from engine.detectors.brand_mention_judge import DETECTOR_NAME as JUDGE_DETECTOR
from engine.detectors.brand_mention_judge import SENTIMENT_DETECTOR_NAME as JUDGE_SENTIMENT
from engine.detectors.matching import registrable
from engine.hard_cases import calibration_prompt_ids
from engine.judge import (
    JudgeConfig,
    JudgeVerdict,
    MentionJudge,
    cannot_judge,
    load_judges,
    maker_of,
    pick_judge,
    usable_judges,
)
from engine.method import METHOD_VERSION
from engine.models import (
    BrandDomainList,
    BrandMentionObservation,
    BrandSpec,
    Prompt,
    PromptIntent,
    SearchMode,
    Sentiment,
    SourceClass,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import (
    ObserveContext,
    RunObservations,
    is_unsure_for_fuzzy,
    mention_context,
    observe,
    persist_observations,
)
from engine.official_domains import official_candidates
from engine.page_check import PageChecker, PageIndex, PageRequest, normalize_url
from engine.resolve import ResolutionContext, UrlResolver
from engine.scheduler import (
    AccountProblem,
    RepeatSampler,
    SamplingResult,
    StopDetail,
    is_account_error,
    reinterpret_account_errors,
)
from engine.storage import STORAGE_DOWN_ERRORS, Storage
from engine.tiers import MentionMethod
from engine.validity import is_valid_answer
from report.archive import attach_latest_report
from report.estimate import UnitPrices, split_budget, unit_prices
from report.jobs import (
    MAX_UNSURE_JUDGED,
    PLAN_CONCURRENCY,
    ClassReport,
    DiagnosisJob,
    JobState,
    LegProgress,
    SamplingNote,
    SurfaceQuotes,
    current_code_version,
)
from report.response_view import latest_observation

log = structlog.get_logger("report.runner")


PlanKey = tuple[Target, SearchMode]  # 一个采样计划:被测对象 × 检索模式,跨全部题目
RunsKey = tuple[str, Target]  # (题, 被测对象)


class _ProgressSink:
    """包一层存储:每落一条样本就给任务盖个时间戳、进度加一、花费加上这条的钱,任务页据此显示进度、已花费与
    「最近一次新结果在几分钟前」,超过阈值就报 JOB_STALLED(v0.4.22)。原来花费要等一条腿跑完才整笔加,
    页面看着像只花了一分钱(小米真跑 2026-11-03)。"""

    def __init__(self, storage: Storage, job: DiagnosisJob, lock: threading.Lock, leg: LegProgress) -> None:
        self._storage, self._job, self._lock, self._leg = storage, job, lock, leg

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self._storage.insert_surface_run(run)
        with self._lock:
            self._job.completed_runs += 1
            self._job.spent_usd += run.cost_usd
            self._job.last_result_at = datetime.now(UTC)
            self._leg.done += 1  # 这条腿自己的进度与花费:任务页边跑边重估用
            self._leg.spent_usd += run.cost_usd
            # 真花了钱的才算(和开场从库里数的同一个口径):原来每落一条都加 1,$0 的失败行把这一单的均价拉低,
            # 同一份历史一口气跑完和续跑后的「预计共约」不一样(第四轮审核)
            self._leg.paid_runs += run.cost_usd > 0

    def insert_call_usage(self, run_id: str, usage: dict[str, float]) -> None:
        self._storage.insert_call_usage(run_id, usage)


# 执行器会写进 job.error_code 的码(S10.1):都在 config/errors.yaml 里(测试把守),文案由任务页从库里取
RUNNER_CODES = ("ACCOUNT_AUTH", "ACCOUNT_CREDIT", "ACCOUNT_REGION", "ACCOUNT_RESTRICTED", "ACCOUNT_QUOTA",
                "DB_UNREACHABLE", "JOB_CRASHED", "PRECHECK_FAILED", "PRECHECK_REJECTED")


class PrecheckFailed(Exception):
    """开跑前预检(S11.1):某家接口有问题(不是账户问题),一次采样都没发;names = 出问题的 AI 产品名。
    code = PRECHECK_FAILED(接口不通、限流:稍后重试)或 PRECHECK_REJECTED(最小请求被当非法请求拒绝:型号下线、
    接口地址变了,重试没用,要改适配器;第三轮审核 A23)。"""

    def __init__(self, names: list[str], *, code: str = "PRECHECK_FAILED") -> None:
        super().__init__(", ".join(names))
        self.names, self.code = names, code


def execute_job(
    *,
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    dsn: str,
    evidence: list[EvidenceEntry],
    replay: bool = False,
    judges: JudgeConfig | None = None,  # 高档裁判配置(S9.3);None = 读 config/judges.yaml
) -> None:
    """replay=True:重新生成报告——只用库里已有的样本重算,不向 AI 提问(样本不补;裁判判定也从库里装回)。"""
    progress_before = (job.total_runs, job.completed_runs)  # 只重算没成时原样放回(_keep_previous_report)
    try:
        _execute(job, adapters=adapters, dsn=dsn, evidence=evidence, replay=replay, judges=judges)
    except AccountProblem as exc:  # v0.4.21:我方账户问题——停下、说人话、等充值后续跑
        log.error("job_halted_account_problem", job_id=job.job_id, surface=exc.surface,
                  model=exc.model, error=exc.message)
        display = adapters[exc.surface].meta().display_name if exc.surface in adapters else exc.surface
        job.state = JobState.FAILED
        # 每种账户问题一句话、一个下一步(换 key / 充值 / 地区不支持 / 账户受限 / 额度到期);错误库的码与账户类
        # error_type 同名,文案在 config/errors.yaml,随界面语言渲染
        job.error_code = exc.error_type.value
        job.error_params = {"ai": display, "model": exc.model, "detail": exc.message}
        job.error = None
        job.phase = "phase_failed"
        _persist_failure(job, dsn)
    except STORAGE_DOWN_ERRORS as exc:  # v0.4.22:数据库掉线也要有代码和人话
        # 一开始就连不上、查到一半断线都算(第四轮审核:原来只认前一种,跑到一半断线报成「程序错误」,不提示去起数据库)
        log.error("job_halted_db_unreachable", job_id=job.job_id, error=str(exc))
        job.state = JobState.FAILED
        job.error_code, job.error_params, job.error = "DB_UNREACHABLE", {"detail": str(exc)}, None
        job.phase = "phase_failed"
        _persist_failure(job, dsn)
    except PrecheckFailed as exc:  # S11.1:开跑前接口不通 / 请求被拒,没花钱就停
        log.error("job_halted_precheck_failed", job_id=job.job_id, code=exc.code, names=exc.names)
        job.state = JobState.FAILED
        job.error_code, job.error_params, job.error = exc.code, {"names": ", ".join(exc.names)}, None
        job.phase = "phase_failed"
        _persist_failure(job, dsn)
    except Exception as exc:  # 失败不静默:状态页如实展示,也给码(S10.1)
        log.error("job_failed", job_id=job.job_id, error=f"{type(exc).__name__}: {exc}")
        job.error_code, job.error_params = "JOB_CRASHED", {"detail": f"{type(exc).__name__}: {exc}"}
        job.error = None
        if replay and _keep_previous_report(job, dsn, progress_before):
            return
        job.state = JobState.FAILED
        job.phase = "phase_failed"
        _persist_failure(job, dsn)


def _keep_previous_report(job: DiagnosisJob, dsn: str, progress: tuple[int, int]) -> bool:
    """只重算(重新生成报告 / 确认官网名单)没成:上一版报告还在库里,装回它,任务回到跑完的状态,出错说明留在任务页。
    原来一律标成「出错」:报告页只给跑完的单看,一次重算失败就再也打不开那份报告,重启后也不装回(第四轮审核)。
    没有读得动的上一版(或库连不上)→ False,照旧记失败。"""
    try:
        with Storage(dsn) as store:
            attach_latest_report(job, store)
            if job.sampling is None:
                return False
            job.total_runs, job.completed_runs = progress
            # 和出报告时同一个判法:有提前停的腿,或上一版重算时还有空槽位
            partial = any(d.truncated or d.stop_reason is StopReason.NOT_RESAMPLED for d in job.stop_details)
            job.state = JobState.PARTIAL if partial else JobState.DONE
            job.phase = "phase_partial" if partial else "phase_done"
            store.upsert_job_snapshot(job.to_snapshot())
    except Exception:
        log.warning("previous_report_restore_failed", job_id=job.job_id)
        return False
    log.warning("replay_failed_previous_report_kept", job_id=job.job_id, state=job.state.value)
    return True


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
    judges: JudgeConfig | None = None,
) -> None:
    # 只重算(replay)不改写「各条腿怎么停的」:沿用上一版的,截断照它算(第三轮审核 A3:原来重算会把部分完成的单改成「完成」,
    # 提前停的提示、弹窗、型号旁标签全没了)。上一版读不动(报告快照旧格式)时,重算里还有空槽位的腿就是当初没问完的。
    previous_stops = list(job.stop_details) if replay else []
    _reset_progress(job)
    job.stop_details = previous_stops  # 重算的整个过程里任务页照旧显示上一版「提前停的型号」
    # 先落一次快照再审计官网:审计(十来次探测,要时开一次真浏览器)可能一两分钟,原来这期间进程死了库里没有这一单,
    # 出题花的钱只剩辅助账本里一个找不到单的编号(第四轮审核);库连不上也在这里先报,不用白等审计
    with Storage(dsn) as storage:
        storage.ensure_schema()
        storage.upsert_job_snapshot(job.to_snapshot())
    job.access_result = _audit_site(job.website)
    with Storage(dsn) as storage:
        # 被测对象 = 仪器 × 型号(v0.4.19):型号是口径的一部分,计划、样本、指标、诊断块一律按对象分开
        adapter_of = {target: adapters[target.surface] for target in job.targets}
        if not replay:
            _precheck(job, adapters, adapter_of, judges)  # 开跑前预检(S11.1):key / 额度 / 接口可用,一次采样都没发
        for prompt in job.prompts:
            storage.insert_prompt(prompt)  # 幂等:续跑不重复
        job.spent_usd = storage.aux_spent_usd(job.job_id)  # 辅助调用(出题)记在账本里,续跑也不丢
        if job.domain_list is None:  # 官网名单只找一次,随快照冻结;续跑 / 重算沿用同一份(S5)
            job.phase = "phase_discover"
            job.adopt_domain_list(_discover_domains(job))
            storage.upsert_job_snapshot(job.to_snapshot())
        if not replay and not job.fresh:
            _fill_from_cache(job, storage, adapter_of)  # 回答缓存(S11.2):别的单 12 小时内同题同型号的回答先填进空槽位
        # 单价和确认页同一份:同品牌各单(含本单已问的)的实测优先,再按这单语言的库里实测(成本调研②)。
        # 只重算不发任何提问,不用单价:单价表只有现在清单里的型号,原来重算也照查,测过已移出清单型号的旧单
        # 一点「重新生成报告」/ 确认官网名单就 KeyError、完成的单被写成「出错」(第四轮审核)
        prices = None if replay else unit_prices(
            {t.surface: a for t, a in adapter_of.items()},
            storage.call_stats(exclude_prompt_ids=calibration_prompt_ids()),  # 校准题不当实测
            locale=job.locale,
            brand_stats=storage.call_stats(storage.brand_prompt_ids(job.brand.name)))
        results = _sample(job, dsn, adapter_of, prices=prices)
        fresh_stops = [StopDetail.from_result(r) for r in results.values()]  # 熔断要说清原因(S10.2)
        job.stop_details = previous_stops or fresh_stops
        # 口径行里「这条腿怎么停的」和「提前停的型号」同一个来源:只重算时沿用上一版的(原来写成重算那次的
        # not_resampled,和上面那句对不上;第四轮审核)
        stop_reasons = {plan: result.stop_reason for plan, result in results.items()}
        stop_reasons.update({(Target(surface=d.surface, model=d.model), d.search_mode): d.stop_reason
                             for d in job.stop_details})

        job.phase = "phase_verify"
        runs_by, offline_runs_by = _load_runs(job, storage)
        resolution = _resolve_redirects(storage, adapters, [*runs_by.values(), *offline_runs_by.values()],
                                        heartbeat=lambda: setattr(job, "last_result_at", datetime.now(UTC)))
        all_runs = [run for runs in runs_by.values() for run in runs]
        pages = _check_pages(job, storage, all_runs, resolution)
        closed_book_runs = [run for runs in offline_runs_by.values() for run in runs]
        judge = _judge_for(job, adapters, storage, [*all_runs, *closed_book_runs], judges,  # 装配裁判、并行预判
                           offline=replay)  # 只重算时不再请裁判(第二轮)
        context = ObserveContext(competitors=job.competitors, resolution=resolution, pages=pages,
                                 mention_method=job.frozen_tier().mention_method, judge=judge, category=job.category)
        observations = {
            key: [observe(run, job.brand, context) for run in runs] for key, runs in runs_by.items()
        }
        offline_observations = {
            key: [observe(run, job.brand, context) for run in runs] for key, runs in offline_runs_by.items()
        }
        # 算完判定就存(observe() 的判定行,不重跑;判定器版本首次出现落审计):原来等算完分、组完报告才存,
        # 中间崩了裁判费已进账本、判定却没存,续跑要重判再花一次钱(第三轮审核 B10)
        persist_observations(storage, [
            o for key in runs_by for o in observations[key] + offline_observations[key]
        ], note=job.job_id)

        job.phase = "phase_scoring"
        access = access_finding(job.access_result)
        verdict_inputs: dict[Target, list[VerdictInput]] = {}  # 被测对象 → 逐题型原料(每个型号一句话)
        for intent in PromptIntent:  # 报告按题型分块,顺序即产品文档 15.2 的五类
            prompts = [p for p in job.prompts if p.intent is intent]
            if not prompts:
                continue
            measurements, closed_book = _measure_class(
                job, intent, prompts, adapter_of, stop_reasons, observations, offline_observations)
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
        job.surface_verdicts = [
            surface_verdict(target.surface, target.model, inputs) for target, inputs in verdict_inputs.items()
        ]
        # AI 回答里写你的名字的写法与次数(S9.2b):开卷 + 闭卷全部回答,按主判定器的命中片段数
        job.spellings = tally_spellings(
            [o for batch in [*observations.values(), *offline_observations.values()] for o in batch], job.brand,
        )
        # 采样后:回答里出现、名单外的同名域名再查一次,标 B1 / B2——只提示,用户确认后才算官网(S5)
        job.adopt_domain_list(_look_alikes(job, all_runs, resolution))
        truncated = any(d.truncated for d in job.stop_details) or any(
            r.stop_reason is StopReason.NOT_RESAMPLED for r in results.values())  # 只重算时还有空槽位 = 当初没问完
        mentions = [o.mention for batch in observations.values() for o in batch if o.mention is not None]
        sampling = _sampling_note(job, runs_by, offline_runs_by, judge, storage, mentions, adapters)
        _finish(job, storage, truncated=truncated, sampling=sampling)


def _precheck(
    job: DiagnosisJob, adapters: dict[str, SurfaceAdapter], adapter_of: dict[Target, SurfaceAdapter],
    judges: JudgeConfig | None,
) -> None:
    """开跑前预检(S11.1,用户 2026-10-12「开跑前自动查余额」):每家被测仪器(高档再加这单会请到的裁判)各打一次最小请求——
    账户问题(key 无效 / 额度不足)按原有的换 key / 充值流程停单;接口不通给 PRECHECK_FAILED。重算(replay)不预检。"""
    job.phase = "phase_precheck"
    to_check: dict[str, SurfaceAdapter] = {}
    for target, adapter in adapter_of.items():
        to_check.setdefault(target.surface, adapter)
    if job.frozen_tier().mention_method is MentionMethod.RULES_FUZZY_JUDGE:
        usable = usable_judges(judges or load_judges(), adapters)
        for target, adapter in adapter_of.items():
            picked = pick_judge(usable, maker_of(adapter.meta(), target.model))
            if picked is not None:
                to_check.setdefault(picked.spec.surface, picked.adapter)
    unavailable: list[str] = []
    rejected: list[str] = []  # 最小请求被当非法请求拒绝(4xx):型号下线、接口地址变了,重试没用(第三轮审核 A23)
    for surface, adapter in to_check.items():
        meta = adapter.meta()
        try:
            healthy = adapter.healthcheck()
        except AdapterAccountError as exc:
            raise AccountProblem(provider=meta.provider, surface=surface, model=meta.default_model,
                                 error_type=exc.error_type, message=str(exc)) from exc
        except AdapterPermanentError as exc:
            log.error("precheck_rejected", job_id=job.job_id, surface=surface, http_status=exc.http_status,
                      error=str(exc))
            rejected.append(meta.display_name)
            continue
        if not healthy:
            unavailable.append(meta.display_name)
    if rejected:
        raise PrecheckFailed(rejected, code="PRECHECK_REJECTED")
    if unavailable:
        raise PrecheckFailed(unavailable)
    log.info("precheck_passed", job_id=job.job_id, surfaces=sorted(to_check))


def _fill_from_cache(job: DiagnosisJob, storage: Storage, adapter_of: dict[Target, SurfaceAdapter]) -> int:
    """开卷腿(启用对照时闭卷腿也算)每题每对象空着的槽位,先用同品牌别的单里 12 小时内的有效回答填
    (复制成本单的运行,花费 0,
    复用关系进 cache_reuse);填不上的才向 AI 提问。返回填进去的条数。"""
    now = datetime.now(UTC)
    filled = 0
    for target, adapter in adapter_of.items():
        modes = [SearchMode.DEFAULT]
        if job.dependency_enabled and adapter.meta().capabilities.supports_search_disabled:
            modes.append(SearchMode.SEARCH_DISABLED)
        for prompt in job.prompts:
            existing = reinterpret_account_errors(
                storage.runs_for_prompt(prompt.prompt_id, surface=target.surface, model=target.model))
            for mode in modes:
                taken = {r.run_index for r in existing if r.search_mode is mode and not is_account_error(r)}
                missing = [i for i in range(1, job.frozen_tier().runs_per_prompt + 1) if i not in taken]
                filled += len(fill_from_cache(
                    storage, job_id=job.job_id, brand_name=job.brand.name, prompt=prompt, target=target,
                    search_mode=mode, missing_indices=missing, now=now,
                    region=None,  # 与采样一致:run_plan 目前不钉地区;哪天钉了,这里和采样一起改(必填,忘传就报错)
                ))
    if filled:
        log.info("answers_reused_from_cache", job_id=job.job_id, n=filled, hours=CACHE_HOURS)
    return filled


def _sampling_note(
    job: DiagnosisJob, runs_by: dict[RunsKey, list[SurfaceRun]], offline_runs_by: dict[RunsKey, list[SurfaceRun]],
    judge: MentionJudge | None, storage: Storage, mentions: list[BrandMentionObservation],
    adapters: dict[str, SurfaceAdapter],
) -> SamplingNote:
    """报告顶部的采样说明(S8.2):本单实际问了多少、有效多少、测了哪些接口与型号、官网名单情况、方法版本。
    裁判写实际判过的那几家;判它的和写它的是同一家的条数单独提醒一句(BR6)。结束时刻由 _finish 落定后再填。"""
    runs = [run for batch in runs_by.values() for run in batch]
    dl = job.domain_list
    n_unsure = sum(m.mentioned is None for m in mentions)
    method = job.frozen_tier().mention_method
    missing_judge = judge is None and (method is MentionMethod.RULES_FUZZY_JUDGE
                                       or (method is MentionMethod.RULES_FUZZY and n_unsure > 0))
    return SamplingNote(
        n_cached=storage.cache_reuse_count(job.job_id), cache_hours=CACHE_HOURS, fresh=job.fresh,
        n_prompts=len(job.prompts), n_targets=len(job.targets), tier=job.frozen_tier(),
        n_runs=len(runs), n_valid=sum(is_valid_answer(run) for run in runs),
        n_offline_runs=sum(len(batch) for batch in offline_runs_by.values()), n_unsure=n_unsure,
        started_at=job.started_at, finished_at=None, targets=list(job.targets),
        domains_confirmed=len(dl.confirmed_domains) if dl is not None else 0,
        domains_unconfirmed=len(dl.unconfirmed_domains) if dl is not None else 0,
        domains_at=dl.discovered_at if dl is not None else None,
        method_version=METHOD_VERSION,
        judge_targets=[Target.parse(key) for key in dict.fromkeys(m.judged_by for m in mentions if m.judged_by)],
        n_own_vendor=sum(m.own_vendor for m in mentions),
        no_judge=cannot_judge(adapters) if missing_judge else [],
        method_notes=[note for surface in dict.fromkeys(target.surface for target in job.targets)
                      if surface in adapters for note in adapters[surface].meta().method_notes
                      # 只对某几个型号说的:这一单测了其中一个才写
                      if not note.models or any(t.surface == surface and t.model in note.models for t in job.targets)],
    )


def _judge_for(
    job: DiagnosisJob, adapters: dict[str, SurfaceAdapter], storage: Storage, runs: list[SurfaceRun],
    judges: JudgeConfig | None,
    *,
    offline: bool = False,
) -> MentionJudge | None:
    """高档每条有效回答都判(S9.3);中档只判模糊判定器说不准的,最多 MAX_UNSURE_JUDGED 条,一条都没有就不装配
    (BR4,用户定 Q3)。runs 含闭卷回答:它们也要判,判过的钱也要进账本。
    按 config/judges.yaml 装配裁判:一家都请不到、库里也没判过 → 不停单,判不清的记说不准,报告写明(BR6,用户定);
    库里已有的裁判判定先装回(续跑 / 重算不花钱);其余并行预判,进度给任务页;
    裁判花费记进辅助调用账本(主线程落库:psycopg 连接非线程安全)。"""
    method = job.frozen_tier().mention_method
    if method is MentionMethod.RULES:
        return None
    every_answer = method is MentionMethod.RULES_FUZZY_JUDGE
    clues = mention_context(job.category, job.competitors)
    wanted = [run for run in runs
              if is_valid_answer(run) and (every_answer or is_unsure_for_fuzzy(run, job.brand, clues))]
    if not every_answer and not wanted:
        return None
    ledger_lock = threading.Lock()
    ledger_runs: list[SurfaceRun] = []

    def ledger(run: SurfaceRun) -> None:
        with ledger_lock:
            ledger_runs.append(run)
            job.spent_usd += run.cost_usd
            job.last_result_at = datetime.now(UTC)

    judge = MentionJudge(judges or load_judges(), adapters, ledger=ledger, locale=job.locale,
                         competitors=[c.name for c in job.competitors], offline=offline)
    stored = _stored_verdicts(storage, runs)
    if not judge.available and not stored:
        log.warning("no_judge_available", job_id=job.job_id, wanted=len(wanted))
        return None
    judge.preload(stored)
    pending = [run for run in wanted if not judge.has(run.run_id)]
    if not every_answer:  # 超过上限的抽一批判:按单号定随机数,续跑抽到的还是这批
        room = max(0, MAX_UNSURE_JUDGED - (len(wanted) - len(pending)))
        pending = random.Random(job.job_id).sample(pending, min(room, len(pending)))
    job.phase = "phase_judging"
    job.judge_total, job.judge_done = len(pending), 0

    def progress(done: int, total: int) -> None:
        job.judge_done, job.judge_total = done, total
        job.last_result_at = datetime.now(UTC)

    try:
        judge.warm(pending, job.brand, concurrency=PLAN_CONCURRENCY, progress=progress)
    finally:
        for run in ledger_runs:
            storage.insert_aux_call(job_id=job.job_id, task="裁判", run=run)
    if not every_answer:
        judge.stop_asking()
    job.phase = "phase_scoring"
    log.info("judging_done", job_id=job.job_id, judged=len(pending), unavailable=judge.unavailable,
             calls=len(ledger_runs))
    return judge


def _stored_verdicts(storage: Storage, runs: list[SurfaceRun]) -> dict[str, JudgeVerdict]:
    """库里已有的裁判判定行(每条回答取最新一行:版本最高、同版本里最后写入的)翻回裁判结论;态度取裁判的态度行。
    原来同版本有好几行时取到的是最旧那行(第二轮审核 B5)。"""
    rows_by_run = storage.observations_for_runs([run.run_id for run in runs])
    verdicts: dict[str, JudgeVerdict] = {}
    for run_id, rows in rows_by_run.items():
        row = latest_observation(rows, JUDGE_DETECTOR)
        if row is None:
            continue
        tone_row = latest_observation(rows, JUDGE_SENTIMENT)
        tone = tone_row["sentiment"] if tone_row is not None else None
        verdicts[run_id] = JudgeVerdict(
            mentioned=bool(row["mentioned"]), spans=[row["matched_text"]] if row.get("matched_text") else [],
            first_position=row.get("first_position"), confidence=float(row.get("confidence") or 0.0),
            sentiment=Sentiment(tone) if tone else None, reason=row.get("reason") or "",
            reason_code=row.get("reason_code"), new_spellings=row.get("new_spellings") or [],
            judged_by=row.get("judged_by"), own_vendor=bool(row.get("own_vendor")),
        )
    return verdicts


def _reset_progress(job: DiagnosisJob) -> None:
    """可重入(v0.4.9 幂等续跑):进度字段清零重算;已存的运行按幂等键复用。"""
    job.state = JobState.RUNNING
    job.phase = "phase_access"
    job.total_runs = 0
    job.completed_runs = 0
    job.spent_usd = 0.0
    job.legs = []
    job.stop_requested = False
    job.class_reports = []
    job.surface_verdicts = []
    job.stop_details = []
    job.run_started_at = datetime.now(UTC)
    if job.started_at is None:  # 起测时刻只在第一次开跑时定;续跑 / 重算不改(第三轮审核:原来报告测试时间只剩续跑那段)
        job.started_at = job.run_started_at
    job.sampling_started_at, job.sampling_base_runs = None, 0
    job.last_result_at = None
    job.verify_total = job.verify_done = 0
    job.verify_stage = job.verify_started_at = None
    job.error_code, job.error_params, job.error = None, {}, None


def _sample(
    job: DiagnosisJob,
    dsn: str,
    adapter_of: dict[Target, SurfaceAdapter],
    *,
    prices: UnitPrices | None,  # None = 只重算:不发提问,不用单价、不分上限
) -> dict[PlanKey, SamplingResult]:
    replay = prices is None
    # 检索依赖对照(第 9 章挂靠):启用时,为支持 search_disabled 的对象追加闭卷腿计划——
    # 同一批题目、同采样参数,仅检索模式不同(模式即口径)
    dependency_targets = [
        target for target, adapter in adapter_of.items()
        if job.dependency_enabled and adapter.meta().capabilities.supports_search_disabled
    ]
    plans: list[PlanKey] = [(target, SearchMode.DEFAULT) for target in adapter_of] + [
        (target, SearchMode.SEARCH_DISABLED) for target in dependency_targets
    ]
    policy = job.policy()  # 每题次数按档位(S9.1)
    calls = len(job.prompts) * policy.runs_per_prompt
    with Storage(dsn) as store:  # 幂等键 (题 × surface × 型号 × 模式) 下已存的运行,续跑直接复用槽位
        prior = {plan: _prior_runs(store, job, plan) for plan in plans}
    taken = {plan: sum(min(policy.runs_per_prompt, len({r.run_index for r in runs if not is_account_error(r)}))
                       for runs in prior[plan].values()) for plan in plans}
    spent = {plan: sum(r.cost_usd for runs in prior[plan].values() for r in runs) for plan in plans}
    job.completed_runs += sum(taken.values())  # 复用的槽位也算进度,它们的钱也算已花费
    job.spent_usd += sum(spent.values())

    def unit(plan: PlanKey) -> float:
        """这条腿开跑前估的单价(确认页同一份);只重算不提问,记 0。"""
        return 0.0 if prices is None else prices.cost(plan[0].key, plan[1])

    def weight(plan: PlanKey) -> float:
        """这条腿还要花的预估(还要问的次数 × 单价):费用上限按它的比例分(用户 2026-11-03 定)。"""
        return (calls - taken[plan]) * unit(plan)

    # 只重算一分不花,不设上限:没问完的腿照旧落「重算不补采」,不会因为没有额度被记成「到了费用上限」
    budgets: dict[PlanKey, float | None] = (
        dict.fromkeys(plans) if replay else split_budget(job.budget_usd, {plan: weight(plan) for plan in plans}, spent))
    job.total_runs = calls * len(plans)
    # 边跑边重估(用户 2026-11-18 定):记下每条腿问到哪、花了多少、开跑前估的单价与它那份上限;
    # 开跑前的预估只在第一次真开跑时定(续跑不改;只重算的不定)
    legs = {plan: LegProgress(
        slots=calls, done=taken[plan], spent_usd=spent[plan],
        paid_runs=sum(run.cost_usd > 0 for runs in prior[plan].values() for run in runs),
        unit_usd=unit(plan), budget_usd=budgets[plan]) for plan in plans}
    job.legs = list(legs.values())
    if job.estimate_usd is None and not replay:
        job.estimate_usd = job.spent_usd + sum(weight(plan) for plan in plans)
    # 「预计还需」只按这次新问的速度算(第三轮审核 A15);进度写进快照,中途断了重启也知道问到哪
    job.sampling_started_at, job.sampling_base_runs = datetime.now(UTC), job.completed_runs
    with Storage(dsn) as store:
        store.upsert_job_snapshot(job.to_snapshot())

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
            sampler = RepeatSampler(_ProgressSink(plan_storage, job, progress_lock, legs[plan]))
            plan_policy = policy.model_copy(update={"budget_usd": budgets[plan]})
            # 上限读这条腿的现值(任务页中途提高上限时当场改);用户点了「现在停下」就在下一次提问前停
            result = sampler.run_plan(adapter, job.prompts, policy=plan_policy, search_mode=mode, model=target.model,
                                      locale=job.locale, prior_runs=prior[plan], allow_new_calls=not replay,
                                      live_budget=lambda: legs[plan].budget_usd,
                                      stop_requested=lambda: job.stop_requested)
        except AccountProblem:
            halt.set()
            raise
        finally:
            legs[plan].finished = True
            plan_storage.close()
        with progress_lock:
            results[plan] = result
            # 一条腿一停就上任务页(用户 2026-11-03 定:撞上限要立刻通知),不等别的腿跑完;只重算时不改写(第三轮 A3)
            if not replay:
                job.stop_details = [StopDetail.from_result(r) for r in results.values()]

    with ThreadPoolExecutor(max_workers=PLAN_CONCURRENCY) as pool:
        list(pool.map(run_one, plans))
    return results


def _prior_runs(storage: Storage, job: DiagnosisJob, plan: PlanKey) -> dict[str, list[SurfaceRun]]:
    """这条腿每道题已存的运行(账户问题的行按现在的解释重标),续跑直接复用槽位、算进度和已花费。"""
    target, mode = plan
    return {
        prompt.prompt_id: reinterpret_account_errors([
            r for r in storage.runs_for_prompt(prompt.prompt_id, surface=target.surface, model=target.model)
            if r.search_mode == mode
        ])
        for prompt in job.prompts
    }


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
    *, heartbeat: Callable[[], None] | None = None,
) -> ResolutionContext:
    """跳转仪器(能力 citation_urls_redirect)的引用链接先还原终点(v0.4.26 S4,C2):按链接去重、库里复用,
    经 crawler/ 只取响应头。判定一律用终点;还原失败的那条引用记看不到。heartbeat 每还原一条叫一下,
    任务页的「卡住了」判断据此知道还在动(审核 2026-10-27 黄 17)。"""
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
        resolutions = resolver.resolve(urls, heartbeat=heartbeat)
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


def _audit_site(website: str) -> access_audit.AccessAuditResult:
    """官网审计;首页原始网页字太少、分不清被拦还是靠脚本加载时,用真浏览器打开一次首页对照(用户 2026-10-29 定)。
    本机没装浏览器内核就不对照,审计照实记看不到。"""
    renderer = PlaywrightRenderer() if playwright_available() else None
    try:
        return access_audit.audit(website, renderer=renderer)
    finally:
        if renderer is not None:
            renderer.close()


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
            PathProbe(url=normalized, http_status=check.http_status, error=check.error, text_chars=check.text_chars)
            for normalized, check in pages.checks.items() if normalized in own
        ])
    return pages


def _measure_class(
    job: DiagnosisJob,
    intent: PromptIntent,
    prompts: list[Prompt],
    adapter_of: dict[Target, SurfaceAdapter],
    stop_reasons: dict[PlanKey, StopReason],
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
            stop_reason=stop_reasons[(target, SearchMode.DEFAULT)],
            competitor_names=competitor_names,
            exposes_retrieval=meta.capabilities.supports_query_trace,
            primary_domain=_primary_domain(job.brand),
            channels_known=bool(job.brand.official_channels),
        ))
        closed_key = (target, SearchMode.SEARCH_DISABLED)
        if closed_key in stop_reasons:
            closed_obs = [o for pid in prompt_ids for o in offline_observations[(pid, target)]]
            closed_book[target] = measure_closed_book(
                target, meta.provider, intent, closed_obs, prompt_ids, stop_reason=stop_reasons[closed_key])
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
