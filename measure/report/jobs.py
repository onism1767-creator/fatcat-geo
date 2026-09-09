"""L4 任务模型与登记处(第 16.3 章):状态机、冻结快照与指纹、成本预估。

DiagnosisJob 是可变对象:进度字段随执行推进。v1 单机进程内保存,不入库
——采样与判定的原始记录早已进只追加库,任务对象只是执行的壳。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from diagnosis.access_audit import AccessAuditResult
from diagnosis.diagnose import NeedDiagnosis
from diagnosis.verdict import SurfaceVerdict
from engine.models import BrandSpec, CompetitorSuggestion, FrozenModel, InformationNeed
from engine.needs import NeedCandidate
from engine.scheduler import SamplingPolicy

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)

# 档位(v0.4.15,用户拍板方案 1):按次数定义——「每问法最多 max_n 次,前 N 次结果完全一样
# 就提前停」。宽度阈值在实际参数下与之等价(混合结果永不提前停),
# tests/unit/test_report_page.py 用 Jeffreys 算术机械核对标签里的数字。
# 预估口径(16.4 / v0.4.6):成本按 surface 分价——单价由各适配器 meta 声明
# (est_cost_per_call_usd,实测均值或推导上界);时间仍用双通道实测均值,
# 留待多 surface 实测后一并校准。
AVG_SECONDS_PER_CALL = 4.0


class JobState(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"  # 熔断截断,如实标注
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # 进程中断(重启后自快照恢复),可续跑(v0.4.9)


class Tier(FrozenModel):
    id: str
    label: str
    policy: SamplingPolicy
    expected_calls_per_plan: float  # 蒙特卡洛/实测的每计划期望采样次数


TIERS: dict[str, Tier] = {
    "standard": Tier(
        id="standard", label="标准档:每个问题最多问 20 次,前 11 次结果完全一样就提前停",
        policy=SamplingPolicy(), expected_calls_per_plan=18.0,
    ),
    "quick": Tier(
        id="quick", label="快扫档:每个问题最多问 8 次,前 6 次结果完全一样就提前停",
        policy=SamplingPolicy(min_n=5, max_n=8, ci_width_threshold=0.35),
        expected_calls_per_plan=7.5,
    ),
}


class Estimate(FrozenModel):
    calls: int
    cost_usd: float
    minutes: float


def estimate_job(
    n_needs: int,
    variants_per_need: int,
    surface_costs: list[float],
    tier_id: str,
    dependency_costs: list[float] | None = None,
) -> Estimate:
    """surface_costs = 所选各 surface 的单次调用期望成本(适配器 meta 声明);
    dependency_costs = 启用检索依赖对照时各支持 surface 的闭卷腿单价
    (闭卷腿与开卷同采样策略,故每腿期望调用数相同)。"""
    dependency_costs = dependency_costs or []
    per_leg_calls = n_needs * variants_per_need * TIERS[tier_id].expected_calls_per_plan
    calls = per_leg_calls * (len(surface_costs) + len(dependency_costs))
    return Estimate(
        calls=round(calls),
        cost_usd=per_leg_calls * (sum(surface_costs) + sum(dependency_costs)),
        minutes=calls * AVG_SECONDS_PER_CALL / 60 / PLAN_CONCURRENCY,
    )


class NeedReport(FrozenModel):
    need: InformationNeed
    variant_texts: list[str]
    diagnosis: NeedDiagnosis


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
        self.competitors_suggested = competitors_suggested

        self.state = JobState.DRAFT
        self.phase = "phase_draft"
        self.needs: list[InformationNeed] = []
        self.competitors: list[BrandSpec] = []
        self.surfaces: list[str] = []
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

    def freeze(
        self,
        *,
        needs: list[InformationNeed],
        competitors: list[BrandSpec],
        surfaces: list[str],
        tier_id: str,
        variants_per_need: int,
        budget_usd: float | None,
        dependency_enabled: bool = False,
    ) -> None:
        """确认即冻结:任务快照 + config_fingerprint(16.3)。改题 = 新任务新基线。"""
        self.needs = needs
        self.competitors = competitors
        self.surfaces = surfaces
        self.tier_id = tier_id
        self.variants_per_need = variants_per_need
        self.budget_usd = budget_usd
        self.dependency_enabled = dependency_enabled
        snapshot = {
            "brand": self.brand.model_dump(),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "needs": [{"statement": n.statement, "intent": n.intent.value} for n in needs],
            "competitors": [c.model_dump() for c in competitors],
            "surfaces": surfaces,
            "tier": tier_id,
            "variants_per_need": variants_per_need,
            "budget_usd": budget_usd,
            "dependency_enabled": dependency_enabled,
            "policy": TIERS[tier_id].policy.model_dump(),
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        self.state = JobState.CONFIRMED
        self.phase = "phase_confirmed"

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
            "surfaces": self.surfaces,
            "tier_id": self.tier_id,
            "variants_per_need": self.variants_per_need,
            "budget_usd": self.budget_usd,
            "dependency_enabled": self.dependency_enabled,
            "fingerprint": self.fingerprint,
        }

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
        job.surfaces = list(snapshot["surfaces"])
        job.tier_id = snapshot["tier_id"]
        job.variants_per_need = snapshot["variants_per_need"]
        job.budget_usd = snapshot["budget_usd"]
        job.dependency_enabled = snapshot["dependency_enabled"]
        job.fingerprint = snapshot["fingerprint"]
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


def restore_registry(dsn: str) -> JobRegistry:
    """启动恢复(v0.4.9):从库中快照重建任务登记处。
    上次进程死亡时正在跑或待跑的任务(CONFIRMED/RUNNING)标为 INTERRUPTED,
    等用户点「续跑」——花钱的动作永远由用户触发。"""
    from engine.storage import Storage

    registry = JobRegistry()
    with Storage(dsn) as storage:
        storage.ensure_schema()
        for snapshot in storage.load_job_snapshots():
            job = DiagnosisJob.from_snapshot(snapshot)
            if job.state in (JobState.CONFIRMED, JobState.RUNNING):
                job.state = JobState.INTERRUPTED
                job.phase = "phase_interrupted"
                storage.upsert_job_snapshot(job.to_snapshot())
            registry.add(job)
    return registry
