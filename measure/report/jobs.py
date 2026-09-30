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
from diagnosis.diagnose import NeedDiagnosis
from diagnosis.verdict import SurfaceVerdict
from engine.models import (
    BrandSpec,
    CompetitorSuggestion,
    FrozenModel,
    InformationNeed,
    Target,
)
from engine.needs import NeedCandidate
from engine.official_domains import DomainCandidate
from engine.scheduler import SamplingPolicy

log = structlog.get_logger("report.jobs")

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)
# 多少分钟没有任何新样本就在任务页报 JOB_STALLED(v0.4.22):一次采样最坏 = 3 次尝试 × 180 s 超时 + 退避 ≈ 10 分钟
STALL_MINUTES = 10


class JobState(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"  # 熔断截断,如实标注
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # 进程中断(重启后自快照恢复),可续跑(v0.4.9)


class Tier(FrozenModel):
    """档位。次数按「每种说法」计(调度器逐问法执行策略);面向用户的说明在 report/i18n.py。"""

    id: str
    policy: SamplingPolicy
    expected_calls_per_plan: float  # 蒙特卡洛/实测的每计划期望采样次数


# 档位(v0.4.15,用户拍板方案 1):按次数定义——「每问法最多 max_n 次,前 N 次结果完全一样
# 就提前停」。宽度阈值在实际参数下与之等价(混合结果永不提前停),
# tests/unit/test_report_page.py 用 Jeffreys 算术机械核对标签里的数字。
TIERS: dict[str, Tier] = {
    "standard": Tier(id="standard", policy=SamplingPolicy(), expected_calls_per_plan=18.0),
    "quick": Tier(
        id="quick", policy=SamplingPolicy(min_n=5, max_n=8, ci_width_threshold=0.35),
        expected_calls_per_plan=7.5,
    ),
}


class NeedReport(FrozenModel):
    need: InformationNeed
    variant_texts: list[str]
    diagnosis: NeedDiagnosis


class ReportPayload(FrozenModel):
    """报告快照的内容(v0.4.16 起落库)。给它加必填字段 = 旧快照读不动(预期行为:记警告、让用户重新生成,不加兼容层)。"""

    access_result: AccessAuditResult | None
    need_reports: list[NeedReport]
    surface_verdicts: list[SurfaceVerdict]
    official_candidates: list[DomainCandidate]  # 数据里出现、可能也是官网的网址(v0.4.24 D4)
    finished_at: datetime | None


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
        candidates: list[NeedCandidate],
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

        self.state = JobState.DRAFT
        self.phase = "phase_draft"
        self.needs: list[InformationNeed] = []
        self.competitors: list[BrandSpec] = []
        self.targets: list[Target] = []  # 被测对象(仪器 × 型号),确认时冻结
        self.tier_id = "quick"
        self.variants_per_need = 3  # v0.4.13 条目 2:问法是语义层独立单位,默认 3
        self.budget_usd: float | None = None
        self.dependency_enabled = False  # 检索依赖对照(第 9 章挂靠),默认关
        self.fingerprint = ""

        self.total_plans = 0
        self.completed_plans = 0
        self.spent_usd = 0.0
        self.access_result: AccessAuditResult | None = None
        self.need_reports: list[NeedReport] = []
        self.surface_verdicts: list[SurfaceVerdict] = []  # 每家 AI 一句话(v0.4.14),与 need_reports 同生命周期
        self.official_candidates: list[DomainCandidate] = []  # 可能也是官网的网址,等用户确认(v0.4.24 D4)
        self.report_meta: ReportMeta | None = None  # 最新一版报告快照的档案信息(v0.4.16)
        self.started_at: datetime | None = None  # 本次执行开始时刻(任务页按实际速度算剩余时间)
        self.last_result_at: datetime | None = None  # 最近一条样本落库的时刻(任务页据此报「卡住了」)
        # 出错说明(v0.4.21):有代码的错误按「代码 + 参数」存、随界面语言渲染;没代码的(意外异常)存原文
        self.error_code: str | None = None
        self.error_params: dict[str, str] = {}
        self.error: str | None = None
        self.finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.state in (JobState.DONE, JobState.PARTIAL)

    @property
    def progress_pct(self) -> int:
        if self.finished:
            return 100
        if not self.total_plans:
            return 0
        return min(99, int(100 * self.completed_plans / self.total_plans))

    def elapsed_minutes(self, now: datetime) -> float | None:
        """运行中已用了多少分钟;没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.started_at is None:
            return None
        return (now - self.started_at).total_seconds() / 60

    def eta_minutes(self, now: datetime) -> float | None:
        """按实际速度算剩余时间(v0.4.17):已用时 ÷ 已完成计划 × 未完成计划;还没完成一个 → None。"""
        elapsed = self.elapsed_minutes(now)
        if elapsed is None or not self.completed_plans or not self.total_plans:
            return None
        return elapsed / self.completed_plans * (self.total_plans - self.completed_plans)

    def quiet_minutes(self, now: datetime) -> float | None:
        """运行中距最近一条新样本(还没有就从开始算)过了多少分钟(v0.4.22);没在跑 → None。"""
        if self.state is not JobState.RUNNING or self.started_at is None:
            return None
        return (now - (self.last_result_at or self.started_at)).total_seconds() / 60

    def freeze(
        self,
        *,
        needs: list[InformationNeed],
        competitors: list[BrandSpec],
        targets: list[Target],
        tier_id: str,
        variants_per_need: int,
        budget_usd: float | None,
        dependency_enabled: bool = False,
    ) -> None:
        """确认即冻结:任务快照 + config_fingerprint(16.3)。改题 = 新任务新基线。"""
        self.needs = needs
        self.competitors = competitors
        self.targets = targets
        self.tier_id = tier_id
        self.variants_per_need = variants_per_need
        self.budget_usd = budget_usd
        self.dependency_enabled = dependency_enabled
        self._refingerprint()
        self.state = JobState.CONFIRMED
        self.phase = "phase_confirmed"

    def update_official_domains(self, domains: list[str]) -> None:
        """用户确认后更新官网名单(架构书附录 v0.4.24 D4):官网名单是口径的一部分,指纹随之重算;
        采样数据不变,之后「重新生成报告」按新名单重算(不重新提问)。"""
        self.brand = self.brand.model_copy(update={"domains": list(dict.fromkeys(domains))})
        self._refingerprint()

    def _refingerprint(self) -> None:
        snapshot = {
            "brand": self.brand.model_dump(),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "needs": [{"statement": n.statement, "intent": n.intent.value} for n in self.needs],
            "competitors": [c.model_dump() for c in self.competitors],
            "targets": [t.model_dump() for t in self.targets],
            "tier": self.tier_id,
            "variants_per_need": self.variants_per_need,
            "budget_usd": self.budget_usd,
            "dependency_enabled": self.dependency_enabled,
            "policy": TIERS[self.tier_id].policy.model_dump(),
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # ── 快照往返(v0.4.9 幂等续跑):冻结配置 + 状态,不含可重算的报告 ────

    def to_snapshot(self) -> dict:
        """落库快照:恢复一个任务所需的全部冻结配置。need_reports/access_result
        不入快照——它们可从只追加的运行档案重算,重算即续跑的收尾。"""
        return {
            "job_id": self.job_id,
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "brand": self.brand.model_dump(mode="json"),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "needs": [n.model_dump(mode="json") for n in self.needs],
            "competitors": [c.model_dump(mode="json") for c in self.competitors],
            "targets": [t.model_dump() for t in self.targets],
            "tier_id": self.tier_id,
            "variants_per_need": self.variants_per_need,
            "budget_usd": self.budget_usd,
            "dependency_enabled": self.dependency_enabled,
            "fingerprint": self.fingerprint,
            "error": {"code": self.error_code, "params": self.error_params, "text": self.error},
        }

    def report_payload(self) -> dict:
        """报告快照(v0.4.16):派生层的结构化档案,只追加落库;结论以码 + 参数存,
        措辞随当前目录渲染,判定冻结在生成那一刻。"""
        return ReportPayload(
            access_result=self.access_result,
            need_reports=self.need_reports,
            surface_verdicts=self.surface_verdicts,
            official_candidates=self.official_candidates,
            finished_at=self.finished_at,
        ).model_dump(mode="json")

    def load_report(self, payload: dict) -> None:
        """结构对不上(旧版快照)抛 ValidationError,由 report/archive.py 按 v0.4.17 规矩处理。"""
        report = ReportPayload.model_validate(payload)
        self.access_result = report.access_result
        self.need_reports = report.need_reports
        self.surface_verdicts = report.surface_verdicts
        self.official_candidates = report.official_candidates
        self.finished_at = report.finished_at

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
        job.needs = [InformationNeed.model_validate(n) for n in snapshot["needs"]]
        job.competitors = [BrandSpec.model_validate(c) for c in snapshot["competitors"]]
        job.targets = [Target.model_validate(t) for t in snapshot["targets"]]
        job.tier_id = snapshot["tier_id"]
        job.variants_per_need = snapshot["variants_per_need"]
        job.budget_usd = snapshot["budget_usd"]
        job.dependency_enabled = snapshot["dependency_enabled"]
        job.fingerprint = snapshot["fingerprint"]
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
