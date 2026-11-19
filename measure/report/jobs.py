"""L4 任务模型与登记处(第 16.3 章):状态机、冻结快照与指纹。
成本预估在 report/estimate.py;从库里恢复与装回报告在 report/archive.py。

DiagnosisJob 是可变对象:进度字段随执行推进。进程内登记处管在跑的单;冻结配置 + 状态的快照每到关键一步就写进
diagnosis_job 表(v0.4.9 幂等续跑),报告另存只追加的 report_snapshot,重启时由 report/archive.py 装回。
采样与判定的原始记录在只追加库里,任务对象只是执行的壳。
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import structlog

from adapters.base import MethodNote, SurfaceAdapter
from diagnosis.access_audit import AccessAuditResult
from diagnosis.diagnose import ClassDiagnosis
from diagnosis.verdict import SurfaceVerdict
from engine.aggregate import NameSpelling
from engine.brand_domains import apply_user_choice
from engine.category_check import CategoryCheck
from engine.drafting import PromptCandidate
from engine.models import (
    AnswerQuote,
    BrandDomainList,
    BrandSpec,
    CompetitorSuggestion,
    FrozenModel,
    Prompt,
    PromptIntent,
    Target,
)
from engine.scheduler import SamplingPolicy, StopDetail
from engine.spellings import SpellingCandidate
from engine.tiers import MentionMethod, ModelSwap, TierChoice, TierConfig, refresh_targets

log = structlog.get_logger("report.jobs")

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)
MAX_UNSURE_JUDGED = 300  # 中档一单最多请裁判判几条说不准的回答:每条约 $0.0004,封顶约 $0.12(BR4)
POLICY = SamplingPolicy()  # 重试 / 熔断参数(13.2);每题次数由档位给(S9.1,DiagnosisJob.policy()),预算上限按单另加
STALL_MARGIN_MINUTES = 5


def stall_minutes(policy: SamplingPolicy) -> int:
    """多少分钟没有任何新样本就在任务页报 JOB_STALLED(v0.4.22)。从策略常数算出来并留余量:一次采样合法的最坏耗时
    = 最多尝试数 × 单次超时 + 中间每次等待(不超过单次等待上限),再加余量。原来写死 10 分钟,正好等于合法最坏耗时,
    健康但慢的一条腿会被提示「重启」(审核 2026-10-27 黄 17)。"""
    worst_s = policy.max_attempts * policy.timeout_s + (policy.max_attempts - 1) * policy.max_wait_s
    return math.ceil(worst_s / 60) + STALL_MARGIN_MINUTES


STALL_MINUTES = stall_minutes(POLICY)


class JobState(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"  # 熔断截断,如实标注
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # 进程中断(重启后自快照恢复),可续跑(v0.4.9)


class SurfaceQuotes(FrozenModel):
    """一个 AI 型号在本题型的 AI 原话(S7,用户定:只进证据区):每道题按提问顺序前 3 次回答,不挑不改。"""

    surface: str
    model: str
    quotes: list[AnswerQuote]


class ClassReport(FrozenModel):
    """一个题型的报告块(v0.4.26 S2:报告按题型分块)。"""

    intent: PromptIntent
    prompt_texts: list[str]  # 本题型的题目原文(与采样同序)
    diagnosis: ClassDiagnosis
    quotes: list[SurfaceQuotes]  # AI 原话(S7)


@dataclass
class LegProgress:
    """一条腿(一个被测对象 × 一种检索模式)问到哪、花了多少:任务页「边跑边重估」用(用户 2026-11-18 定)。
    只在内存里、随执行推进;重启后不在跑就不需要,续跑时执行器按库里重数。"""

    slots: int  # 这条腿一共要问几次
    done: int  # 已有结果的次数(含续跑复用、缓存复制的)
    spent_usd: float  # 这条腿已花的钱
    paid_runs: int  # 其中真花了钱的次数(缓存复制来的不算):这一单自己的均价 = spent_usd ÷ paid_runs
    unit_usd: float  # 开跑前估的单价(问得还不够多时先用它)
    budget_usd: float | None  # 费用上限分给这条腿的那一份;没设上限 = None


class SamplingNote(FrozenModel):
    """报告顶部的采样说明(S8.2,架构书 v0.4.26 §10):这份报告是怎么测出来的,一眼看全。
    数字都是本单实际发生的(不是计划):提问次数含失败与拒答,有效回答 = 成功且正文非空(engine/validity.py 一处定)。"""

    n_prompts: int
    n_targets: int  # 被测对象数(接口 × 型号)
    tier: TierChoice  # 档位与参数(S9.1):档位名 / 每题次数 / 「提到你」怎么认 / 是否自定义
    n_runs: int  # 开卷提问次数(含失败 / 拒答)
    n_valid: int  # 有效回答数
    n_offline_runs: int  # 闭卷对照次数(检索依赖,0 = 没开),单独算
    n_unsure: int = 0  # 说不准的回答条数(BR4):只出现了这个词,看不出是不是你,不进提及率分母
    n_cached: int  # 其中几条回答来自回答缓存(别的单里 cache_hours 小时内问过的,S11.2),不算本单花费
    cache_hours: int
    fresh: bool  # 「修好后再测」:一律重新提问、没用缓存(S11.3)
    started_at: datetime | None
    finished_at: datetime | None
    targets: list[Target]  # 实际调用的接口与型号(显示名由报告层命名器给)
    domains_confirmed: int  # 官网名单:已确认几个(含主站)
    domains_unconfirmed: int  # 待用户确认几个
    domains_at: datetime | None  # 名单日期
    method_version: str  # engine/method.py::METHOD_VERSION,生成时冻结
    judge_targets: list[Target]  # 这单实际判过的 AI 裁判(S9.3,中档只判说不准的),没用裁判为空
    n_own_vendor: int = 0  # 其中几条回答,判它的和写它的是同一家 AI(BR6;用户 2026-11-17 定:照样判,报告提醒一句)
    # 该请裁判却名单里一位都用不上时,无法进行 AI 判断的这几家(BR6);非空 = 这单没有裁判。
    # 现有的接口各自都能单独当裁判,只有以后新接的接口没配裁判才会非空
    no_judge: list[str] = []
    # 被测接口各自声明的测法说明(adapters 的 SurfaceMeta.method_notes),生成报告时冻在这里;没有就空
    method_notes: list[MethodNote] = []


class ReportPayload(FrozenModel):
    """报告快照的内容(v0.4.16 起落库)。给它加必填字段 = 旧快照读不动(预期行为:记警告、让用户重新生成,不加兼容层)。"""

    access_result: AccessAuditResult | None
    class_reports: list[ClassReport]
    surface_verdicts: list[SurfaceVerdict]
    finished_at: datetime | None
    sampling: SamplingNote  # 采样说明(S8.2)
    spellings: list[NameSpelling]  # AI 回答里写你的名字的写法与次数(S9.2b)
    stop_details: list[StopDetail]  # 各条腿怎么停的(S10.2):提前停的报告里要说清原因


class PendingDraft(FrozenModel):
    """首页填的、要等品类定下来才用的出题输入(S11.4):品类自动核对判「不是一类」时,先拦一页问品类,选完才出题。"""

    questions: str  # 手填的问题原文(每行一条)
    generate: bool  # 勾了「让系统帮我出题」


class ReportMeta(FrozenModel):
    """报告快照的档案信息(v0.4.16):生成时间、第几版、生成时的程序版本。"""

    created_at: datetime
    versions: int
    code_version: str | None


def current_code_version() -> str | None:
    """生成报告时的程序版本(git 短哈希);拿不到就 None,不编。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class DiagnosisJob:
    def __init__(
        self,
        *,
        brand: BrandSpec,
        website: str,
        category: str,
        locale: str,
        candidates: list[PromptCandidate],
        competitors_suggested: list[CompetitorSuggestion],
    ) -> None:
        self.job_id = uuid4().hex[:12]
        self.created_at = datetime.now(UTC)
        self.brand = brand
        self.website = website
        self.category = category
        self.locale = locale
        self.candidates = candidates
        self.preselected: list[int] = []  # 确认页默认勾选的候选题下标(建任务时由 preselect_candidates 定)
        self.competitors_suggested = competitors_suggested
        self.spellings_suggested: list[SpellingCandidate] = []  # AI 列的常见写法 + 网页里数的次数(S9.2b),只服务确认页
        # 品类自动核对(S11.4):判「不是一类」才留下结果渲染品类页;出题输入先存着,品类定下来再出题;都不进快照
        self.category_check: CategoryCheck | None = None
        self.pending_draft: PendingDraft | None = None
        self.drafting = False  # 品类页之后 AI 正在出题(1–2 分钟):确认页显示「正在出题」,不显示空的候选题(第二轮)
        # 同一单「查状态 + 改状态」必须一步做完:路由跑在线程池里,两个几乎同时到的请求都能过检查、起两个执行器(第二轮)
        self.lock = threading.Lock()

        self.state = JobState.DRAFT
        self.phase = "phase_draft"
        self.prompts: list[Prompt] = []  # 题目集,确认时冻结(每题原样问 tier.runs_per_prompt 次)
        self.competitors: list[BrandSpec] = []
        self.targets: list[Target] = []  # 被测对象(仪器 × 型号),确认时冻结
        self.tier: TierChoice | None = None  # 档位参数(S9.1):型号怎么选 × 每题几次 × 认法,确认时冻结
        self.budget_usd: float | None = None
        self.dependency_enabled = False  # 检索依赖对照(第 9 章挂靠),默认关
        self.fresh = False  # 「修好后再测」的单(S11.3):一律重新提问,不走回答缓存(S11.2)
        self.parent_job_id: str | None = None  # 再测对照的上一单编号(S11.3);不是口径,不进指纹
        # 再测时上一单测过、现在已移出清单的型号换成了什么(用户 2026-11-13 定);被测对象本身已进指纹,这里只为写明
        self.model_swaps: list[ModelSwap] = []
        self.fingerprint = ""

        # 开跑前估的花费(已经花掉的出题等辅助调用 + 提问部分的预估):第一次开跑时定、进快照,续跑不改;
        # 任务页拿它和「照目前的花费预计共约多少」比(边跑边重估,用户 2026-11-18 定)。不是口径,不进指纹
        self.estimate_usd: float | None = None
        self.legs: list[LegProgress] = []  # 各条腿的进度(边跑边重估用),只在内存里
        self.total_runs = 0  # 本单要发的提问次数(题数 × 每题次数 × 腿数)
        self.completed_runs = 0  # 已落库(含续跑复用)的次数
        self.spent_usd = 0.0
        self.access_result: AccessAuditResult | None = None
        self.class_reports: list[ClassReport] = []
        self.surface_verdicts: list[SurfaceVerdict] = []  # 每家 AI 一句话(v0.4.14),与 class_reports 同生命周期
        self.domain_list: BrandDomainList | None = None  # 官网名单(S5):开跑时自动发现一次,随快照冻结,再测沿用
        self.report_meta: ReportMeta | None = None  # 最新一版报告快照的档案信息(v0.4.16)
        self.sampling: SamplingNote | None = None  # 采样说明(S8.2):出报告时算,随报告快照存
        self.spellings: list[NameSpelling] = []  # AI 回答里的写法统计(S9.2b),随报告快照存
        self.stop_details: list[StopDetail] = []  # 各条腿怎么停的(S10.2),随报告快照存
        # 起测时刻:第一次开跑时定、进快照,续跑不改——报告「测试时间」从它算(第三轮审核:原来续跑把它重置成续跑那一刻)
        self.started_at: datetime | None = None
        self.run_started_at: datetime | None = None  # 这一次执行开始的时刻(任务页「已用时」「卡住了」从它算)
        # 这一次提问阶段开始的时刻与那时已有的次数:「预计还需」只按这次新问的速度算,不把续跑 / 缓存复用的算进去(A15)
        self.sampling_started_at: datetime | None = None
        self.sampling_base_runs = 0
        self.last_result_at: datetime | None = None  # 最近一条样本落库的时刻(任务页据此报「卡住了」)
        # 核对网页阶段的进度(S8.5):提问完之后还要开网页(最多 15 分钟),任务页单独显示;不进快照(重启后重跑)
        self.verify_total = 0
        self.verify_done = 0
        self.verify_stage: str | None = None  # fetch / render / archive / done
        self.verify_started_at: datetime | None = None
        self.judge_total = 0  # 高档 AI 裁判复核进度(S9.3):要判几条、判了几条
        self.judge_done = 0
        # 出错说明(v0.4.21):有代码的错误按「代码 + 参数」存、随界面语言渲染;没代码的(意外异常)存原文
        self.error_code: str | None = None
        self.error_params: dict[str, str] = {}
        self.error: str | None = None
        self.finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.state in (JobState.DONE, JobState.PARTIAL)

    @property
    def sampling_pct(self) -> int:
        """提问阶段的真实进度:已问 ÷ 总次数(总数 = 计划数,只进不退)。"""
        if not self.total_runs:
            return 100 if self.finished else 0
        return min(100, int(100 * self.completed_runs / self.total_runs))

    @property
    def overall_pct(self) -> int:
        """总进度(用户 2026-10-14 要求):按阶段分段——官网检查 / 找官网名单 / 提问 / 核对网页 / AI 裁判复核(高档)/ 整理,
        提问段按已问次数走(占大头),核对段按已开网页数走,裁判段按已判条数走;各段权重是经验值,不是时间估计,页面上注明。"""
        if self.finished:
            return 100
        sampling = self.completed_runs / self.total_runs if self.total_runs else 0.0  # 用精确比例,别先取整
        spans = {"phase_draft": 0, "phase_confirmed": 1, "phase_precheck": 2, "phase_access": 3, "phase_discover": 6,
                 "phase_sampling": 8 + int(72 * sampling),
                 "phase_verify": 80 + int(12 * (self.verify_done / self.verify_total if self.verify_total else 0)),
                 "phase_judging": 92 + int(5 * (self.judge_done / self.judge_total if self.judge_total else 0)),
                 "phase_scoring": 97}
        return spans.get(self.phase, 8 + int(72 * sampling))  # 出错 / 中断:停在当时的位置

    def elapsed_minutes(self, now: datetime) -> float | None:
        """这一次运行已用了多少分钟;没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.run_started_at is None:
            return None
        return (now - self.run_started_at).total_seconds() / 60

    def eta_minutes(self, now: datetime) -> float | None:
        """按这一次新问的实际速度算剩余时间(v0.4.17;第三轮审核 A15):提问阶段已用时 ÷ 这次新问的次数 × 未完成次数。
        续跑或缓存复用的旧样本不算进速度(原来复用 250 条后会写「还需 0.1 分钟」);这次还没新问一次 → None。"""
        if self.state is not JobState.RUNNING or self.sampling_started_at is None or not self.total_runs:
            return None
        asked = self.completed_runs - self.sampling_base_runs
        if asked <= 0:
            return None
        elapsed = (now - self.sampling_started_at).total_seconds() / 60
        return elapsed / asked * (self.total_runs - self.completed_runs)

    def verify_remaining_minutes(self, now: datetime, budget_s: float) -> float | None:
        """核对网页最多还要等多久(S8.5):预算 − 已用,有界;还没开始 → None。到时没开完的记「没来得及开」。"""
        if self.verify_started_at is None:
            return None
        return max(0.0, (budget_s - (now - self.verify_started_at).total_seconds()) / 60)

    def quiet_minutes(self, now: datetime) -> float | None:
        """运行中距最近一条新样本(还没有就从这次开始算)过了多少分钟(v0.4.22);没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.run_started_at is None:
            return None
        return (now - (self.last_result_at or self.run_started_at)).total_seconds() / 60

    def freeze(
        self,
        *,
        prompts: list[Prompt],
        competitors: list[BrandSpec],
        targets: list[Target],
        budget_usd: float | None,
        tier: TierChoice,
        dependency_enabled: bool = False,
    ) -> None:
        """确认即冻结:任务快照 + config_fingerprint(16.3)。改题 = 新任务新基线。"""
        self.prompts = prompts
        self.competitors = competitors
        self.targets = targets
        self.tier = tier
        self.budget_usd = budget_usd
        self.dependency_enabled = dependency_enabled
        self._refingerprint()
        self.state = JobState.CONFIRMED
        self.phase = "phase_confirmed"

    def adopt_domain_list(self, domain_list: BrandDomainList) -> None:
        """官网名单(S5)是口径的一部分:计入的域名与官方渠道进 BrandSpec 供判定,指纹随之重算。"""
        self.domain_list = domain_list
        self.brand = self.brand.model_copy(update={
            "domains": domain_list.confirmed_domains, "official_channels": domain_list.channel_prefixes,
        })
        self._refingerprint()

    def apply_domain_choice(self, *, confirm: list[str], remove: list[str]) -> bool:
        """用户确认 B 类 / 去掉 A 类;名单没变返回 False。改了之后按新名单重新生成报告(不重新提问)。"""
        if self.domain_list is None:
            return False
        updated = apply_user_choice(self.domain_list, confirm=confirm, remove=remove)
        if updated == self.domain_list:
            return False
        self.adopt_domain_list(updated)
        return True

    def raise_budget(self, budget_usd: float) -> None:
        """接着跑前提高费用上限(S10.3):只许往上调(预算截断的腿才补得齐);不是口径,不进指纹、编号不变(B8),样本照旧复用。"""
        lower = self.budget_usd is not None and budget_usd < self.budget_usd
        if not math.isfinite(budget_usd) or budget_usd <= 0 or lower:
            raise ValueError("费用上限只能往上调,且必须是正数(nan / inf 不收,第三轮审核 A12)")
        self.budget_usd = budget_usd

    def retest(self, adapters: Mapping[str, SurfaceAdapter], tiers: TierConfig) -> DiagnosisJob:
        """「修好后再测」(S11.3):按这一单冻结的口径新开一单——同样的品牌(含别名与官网域名)、题目、竞品、被测对象、档位、
        费用上限、对照开关与官网名单(产品文档 D12:再测默认沿用同一份名单,保证前后可比)。题目换新编号:槽位按题目编号认,
        沿用旧编号会把上一单的回答当成已问过的;fresh=True 一律重新提问、不走回答缓存;记下上一单编号供两单对照。
        被测型号里已移出清单的(工程原则 9),换成这家同一档现在的型号,换了什么记进 model_swaps(用户 2026-11-13 定 甲)。
        没跑完的单不能再测——那是程序顺序错了,直接报错。"""
        if not self.finished:
            raise RuntimeError("还没跑完的任务不能再测")
        tier = self.frozen_tier()
        spec = tiers.get(tier.name)
        if spec is None:  # 档位配置改了名:不猜该按哪一档挑,直接报错
            raise RuntimeError(f"档位 {tier.name!r} 已不在档位配置里,没法按它换型号")
        targets, swaps = refresh_targets(self.targets, spec, adapters, custom=tier.custom)
        job = DiagnosisJob(brand=self.brand, website=self.website, category=self.category, locale=self.locale,
                           candidates=[], competitors_suggested=[])
        job.fresh = True
        job.parent_job_id = self.job_id
        job.model_swaps = swaps
        job.domain_list = self.domain_list
        job.freeze(prompts=[p.model_copy(update={"prompt_id": str(uuid4())}) for p in self.prompts],
                   competitors=self.competitors, targets=targets, tier=tier,
                   budget_usd=self.budget_usd, dependency_enabled=self.dependency_enabled)
        return job

    def frozen_tier(self) -> TierChoice:
        """确认后才有的档位参数;没冻结就来取是程序顺序错了,直接报错,不给默认值。"""
        if self.tier is None:
            raise RuntimeError("任务还没确认,没有档位参数(tier 为空)")
        return self.tier

    def method_used(self) -> MentionMethod:
        """读已存判定行时用:高档一家裁判都请不到时没有裁判那一行,和中档一样读模糊判定器(BR6)。"""
        method = self.frozen_tier().mention_method
        if method is MentionMethod.RULES_FUZZY_JUDGE and self.sampling is not None and self.sampling.no_judge:
            return MentionMethod.RULES_FUZZY
        return method

    def policy(self) -> SamplingPolicy:
        """本单的采样参数:重试 / 熔断用全局 POLICY,每题次数按档位(S9.1)。"""
        return POLICY.model_copy(update={"runs_per_prompt": self.frozen_tier().runs_per_prompt})

    def _refingerprint(self) -> None:
        snapshot = {
            "brand": self.brand.model_dump(),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "prompts": [{"text": p.text, "intent": p.intent.value} for p in self.prompts],
            "competitors": [c.model_dump() for c in self.competitors],
            "targets": [t.model_dump() for t in self.targets],
            # 费用上限不进指纹(用户 2026-11-12 定 B8):它是钱包的事、不是口径,续跑调高上限后编号不该变
            "dependency_enabled": self.dependency_enabled,
            "fresh": self.fresh,
            "tier": self.frozen_tier().model_dump(mode="json"),
            "policy": self.policy().model_dump(),
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # ── 快照往返(v0.4.9 幂等续跑):冻结配置 + 状态,不含可重算的报告 ────

    def to_snapshot(self) -> dict:
        """落库快照:恢复一个任务所需的全部冻结配置。class_reports/access_result
        不入快照——它们可从只追加的运行档案重算,重算即续跑的收尾。"""
        return {
            "job_id": self.job_id,
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "brand": self.brand.model_dump(mode="json"),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "prompts": [p.model_dump(mode="json") for p in self.prompts],
            "competitors": [c.model_dump(mode="json") for c in self.competitors],
            "targets": [t.model_dump() for t in self.targets],
            "tier": self.frozen_tier().model_dump(mode="json"),
            "budget_usd": self.budget_usd,
            "dependency_enabled": self.dependency_enabled,
            "fresh": self.fresh,
            "parent_job_id": self.parent_job_id,
            "model_swaps": [s.model_dump() for s in self.model_swaps],
            "started_at": self.started_at.isoformat() if self.started_at is not None else None,
            # 进度(第三轮审核:原来不进快照,重启后任务页「已问 0/0」);被中断 / 出错的单重启时由 archive 按库里重数
            "progress": {"total_runs": self.total_runs, "completed_runs": self.completed_runs,
                         "spent_usd": self.spent_usd},
            "estimate_usd": self.estimate_usd,
            "fingerprint": self.fingerprint,
            "domain_list": self.domain_list.model_dump(mode="json") if self.domain_list is not None else None,
            "error": {"code": self.error_code, "params": self.error_params, "text": self.error},
        }

    def sampling_note(self) -> SamplingNote:
        """出完报告才有的采样说明(S8.2):报告页、结果文件、报告快照都从这里取实际发生的数字;
        还没算就来取是程序顺序错了,直接报错,不给默认值。"""
        if self.sampling is None:
            raise RuntimeError("出报告前必须先算采样说明(sampling 为空)")
        return self.sampling

    def report_payload(self) -> dict:
        """报告快照(v0.4.16):派生层的结构化档案,只追加落库;结论以码 + 参数存,
        措辞随当前目录渲染,判定冻结在生成那一刻。"""
        return ReportPayload(
            access_result=self.access_result,
            class_reports=self.class_reports,
            surface_verdicts=self.surface_verdicts,
            finished_at=self.finished_at,
            sampling=self.sampling_note(),
            spellings=self.spellings,
            stop_details=self.stop_details,
        ).model_dump(mode="json")

    def load_report(self, payload: dict) -> None:
        """结构对不上(旧版快照)抛 ValidationError,由 report/archive.py 按 v0.4.17 规矩处理。"""
        report = ReportPayload.model_validate(payload)
        self.access_result = report.access_result
        self.class_reports = report.class_reports
        self.surface_verdicts = report.surface_verdicts
        self.finished_at = report.finished_at
        self.sampling = report.sampling
        self.spellings = report.spellings
        self.stop_details = report.stop_details

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> DiagnosisJob:
        job = cls(
            brand=BrandSpec.model_validate(snapshot["brand"]),
            website=snapshot["website"],
            category=snapshot["category"],
            locale=snapshot["locale"],
            candidates=[],  # 起草候选只服务确认页,冻结后无意义
            competitors_suggested=[],
        )
        job.job_id = snapshot["job_id"]
        job.created_at = datetime.fromisoformat(snapshot["created_at"])
        job.state = JobState(snapshot["state"])
        job.prompts = [Prompt.model_validate(p) for p in snapshot["prompts"]]
        job.competitors = [BrandSpec.model_validate(c) for c in snapshot["competitors"]]
        job.targets = [Target.model_validate(t) for t in snapshot["targets"]]
        job.tier = TierChoice.model_validate(snapshot["tier"])  # S9.1 起才有;更早的快照读不动,由 archive 记警告跳过
        job.budget_usd = snapshot["budget_usd"]
        job.dependency_enabled = snapshot["dependency_enabled"]
        job.fresh = snapshot["fresh"]
        job.parent_job_id = snapshot["parent_job_id"]
        # 2026-11-13 起才写这一项;更早的单那时还没有「再测换型号」这回事,没有这一项就是没换过
        job.model_swaps = [ModelSwap.model_validate(s) for s in snapshot.get("model_swaps", [])]
        # 起测时刻与进度 2026-11-13 起才写;更早的快照没有就是不知道(起测时间报告里另有采样说明的那一份)
        started = snapshot.get("started_at")
        job.started_at = datetime.fromisoformat(started) if started else None
        progress = snapshot.get("progress") or {}
        job.total_runs = progress.get("total_runs", 0)
        job.completed_runs = progress.get("completed_runs", 0)
        job.spent_usd = progress.get("spent_usd", 0.0)
        job.estimate_usd = snapshot.get("estimate_usd")  # 2026-11-18 起才写;更早的单没有就是不知道
        job.fingerprint = snapshot["fingerprint"]
        if snapshot["domain_list"] is not None:
            job.domain_list = BrandDomainList.model_validate(snapshot["domain_list"])
        error = snapshot["error"]  # 每份快照都写(没出错 = 三个值都空);旧格式兜底已删(用户 2026-10-28 定:不留冗余代码)
        job.error_code, job.error_params, job.error = error["code"], error["params"], error["text"]
        job.phase = {
            JobState.DONE: "phase_done",
            JobState.PARTIAL: "phase_partial",
            JobState.FAILED: "phase_failed",
        }.get(job.state, "phase_interrupted")
        return job


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, DiagnosisJob] = {}
        self._lock = threading.Lock()

    def add(self, job: DiagnosisJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def discard(self, job_id: str) -> None:
        """撤掉一单(只用于还没确认、出题没成的草稿;确认过的任务有快照,不走这里)。"""
        with self._lock:
            self._jobs.pop(job_id, None)

    def get(self, job_id: str) -> DiagnosisJob | None:
        return self._jobs.get(job_id)

    def _snapshot(self) -> list[DiagnosisJob]:
        """先在锁里拷一份再遍历:别的请求同时登记或撤单时不会「dictionary changed size during iteration」(第二轮)。"""
        with self._lock:
            return list(self._jobs.values())

    def retests_of(self, job_id: str) -> list[DiagnosisJob]:
        """这一单的全部再测(S11.3),旧的在前。"""
        return sorted((j for j in self._snapshot() if j.parent_job_id == job_id), key=lambda j: j.created_at)

    def recent(self, limit: int = 20) -> list[DiagnosisJob]:
        jobs = sorted(self._snapshot(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]
