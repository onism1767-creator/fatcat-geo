"""L4 任务模型与登记处(第 16.3 章):状态机、冻结快照与指纹。
成本预估在 report/estimate.py;从库里恢复与装回报告在 report/archive.py。

DiagnosisJob 是可变对象:进度字段随执行推进。v1 单机进程内保存,不入库
——采样与判定的原始记录早已进只追加库,任务对象只是执行的壳。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import structlog

from diagnosis.access_audit import AccessAuditResult
from diagnosis.diagnose import ClassDiagnosis
from diagnosis.verdict import SurfaceVerdict
from engine.aggregate import NameSpelling
from engine.brand_domains import apply_user_choice
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
from engine.tiers import TierChoice

log = structlog.get_logger("report.jobs")

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)
# 多少分钟没有任何新样本就在任务页报 JOB_STALLED(v0.4.22):一次采样最坏 = 3 次尝试 × 180 s 超时 + 退避 ≈ 10 分钟
STALL_MINUTES = 10
POLICY = SamplingPolicy()  # 重试 / 熔断参数(13.2);每题次数由档位给(S9.1,DiagnosisJob.policy()),预算上限按单另加


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


class SamplingNote(FrozenModel):
    """报告顶部的采样说明(S8.2,架构书 v0.4.26 §10):这份报告是怎么测出来的,一眼看全。
    数字都是本单实际发生的(不是计划):提问次数含失败与拒答,有效回答 = 成功且正文非空(engine/validity.py 一处定)。"""

    n_prompts: int
    n_targets: int  # 被测对象数(接口 × 型号)
    tier: TierChoice  # 档位与参数(S9.1):档位名 / 每题次数 / 「提到你」怎么认 / 是否自定义
    n_runs: int  # 开卷提问次数(含失败 / 拒答)
    n_valid: int  # 有效回答数
    n_offline_runs: int  # 闭卷对照次数(检索依赖,0 = 没开),单独算
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
    judge_targets: list[Target]  # 高档的 AI 裁判(主 + 备用;S9.3),没用裁判为空


class ReportPayload(FrozenModel):
    """报告快照的内容(v0.4.16 起落库)。给它加必填字段 = 旧快照读不动(预期行为:记警告、让用户重新生成,不加兼容层)。"""

    access_result: AccessAuditResult | None
    class_reports: list[ClassReport]
    surface_verdicts: list[SurfaceVerdict]
    finished_at: datetime | None
    sampling: SamplingNote  # 采样说明(S8.2)
    spellings: list[NameSpelling]  # AI 回答里写你的名字的写法与次数(S9.2b)
    stop_details: list[StopDetail]  # 各条腿怎么停的(S10.2):提前停的报告里要说清原因


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

        self.state = JobState.DRAFT
        self.phase = "phase_draft"
        self.prompts: list[Prompt] = []  # 题目集,确认时冻结(每题原样问 tier.runs_per_prompt 次)
        self.competitors: list[BrandSpec] = []
        self.targets: list[Target] = []  # 被测对象(仪器 × 型号),确认时冻结
        self.tier: TierChoice | None = None  # 档位参数(S9.1):型号怎么选 × 每题几次 × 认法,确认时冻结
        self.budget_usd: float | None = None
        self.dependency_enabled = False  # 检索依赖对照(第 9 章挂靠),默认关
        self.fresh = False  # 「修好后再测」的单(S11.3):一律重新提问,不走回答缓存(S11.2)
        self.fingerprint = ""

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
        self.started_at: datetime | None = None  # 本次执行开始时刻(任务页按实际速度算剩余时间)
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
        """运行中已用了多少分钟;没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.started_at is None:
            return None
        return (now - self.started_at).total_seconds() / 60

    def eta_minutes(self, now: datetime) -> float | None:
        """按实际速度算剩余时间(v0.4.17):已用时 ÷ 已完成次数 × 未完成次数;还没完成一次 → None。"""
        elapsed = self.elapsed_minutes(now)
        if elapsed is None or not self.completed_runs or not self.total_runs:
            return None
        return elapsed / self.completed_runs * (self.total_runs - self.completed_runs)

    def verify_remaining_minutes(self, now: datetime, budget_s: float) -> float | None:
        """核对网页最多还要等多久(S8.5):预算 − 已用,有界;还没开始 → None。到时没开完的记「没来得及开」。"""
        if self.verify_started_at is None:
            return None
        return max(0.0, (budget_s - (now - self.verify_started_at).total_seconds()) / 60)

    def quiet_minutes(self, now: datetime) -> float | None:
        """运行中距最近一条新样本(还没有就从开始算)过了多少分钟(v0.4.22);没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.started_at is None:
            return None
        return (now - (self.last_result_at or self.started_at)).total_seconds() / 60

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
        """接着跑前提高费用上限(S10.3):只许往上调(预算截断的腿才补得齐),指纹随之更新;不是口径,样本照旧复用。"""
        if budget_usd <= 0 or (self.budget_usd is not None and budget_usd < self.budget_usd):
            raise ValueError("费用上限只能往上调,且必须是正数")
        self.budget_usd = budget_usd
        self._refingerprint()

    def frozen_tier(self) -> TierChoice:
        """确认后才有的档位参数;没冻结就来取是程序顺序错了,直接报错,不给默认值。"""
        if self.tier is None:
            raise RuntimeError("任务还没确认,没有档位参数(tier 为空)")
        return self.tier

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
            "budget_usd": self.budget_usd,
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
        job.fingerprint = snapshot["fingerprint"]
        if snapshot.get("domain_list"):
            job.domain_list = BrandDomainList.model_validate(snapshot["domain_list"])
        error = snapshot.get("error") or {}  # 本键 v0.4.21 起才写;更早的快照没有 = 没有出错说明
        job.error_code, job.error_params, job.error = error.get("code"), error.get("params") or {}, error.get("text")
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

    def get(self, job_id: str) -> DiagnosisJob | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[DiagnosisJob]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]
