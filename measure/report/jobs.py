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
from engine.models import BrandSpec, CompetitorSuggestion, FrozenModel, InformationNeed
from engine.needs import NeedCandidate
from engine.scheduler import SamplingPolicy

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)

# 预估口径(16.4):2026-08 双通道实测均值,随价目/延迟变化校准
AVG_COST_PER_CALL_USD = 0.0055
AVG_SECONDS_PER_CALL = 4.0


class JobState(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"  # 熔断截断,如实标注
    FAILED = "FAILED"


class Tier(FrozenModel):
    id: str
    label: str
    policy: SamplingPolicy
    expected_calls_per_plan: float  # 蒙特卡洛/实测的每计划期望采样次数


TIERS: dict[str, Tier] = {
    "standard": Tier(
        id="standard", label="标准档(精度约 ±10%)",
        policy=SamplingPolicy(), expected_calls_per_plan=18.0,
    ),
    "quick": Tier(
        id="quick", label="快扫档(精度约 ±17%)",
        policy=SamplingPolicy(min_n=5, max_n=8, ci_width_threshold=0.35),
        expected_calls_per_plan=7.5,
    ),
}


class Estimate(FrozenModel):
    calls: int
    cost_usd: float
    minutes: float


def estimate_job(n_needs: int, variants_per_need: int, n_surfaces: int, tier_id: str) -> Estimate:
    calls = n_needs * variants_per_need * n_surfaces * TIERS[tier_id].expected_calls_per_plan
    return Estimate(
        calls=round(calls),
        cost_usd=calls * AVG_COST_PER_CALL_USD,
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
        self.phase = "待确认"
        self.needs: list[InformationNeed] = []
        self.competitors: list[BrandSpec] = []
        self.surfaces: list[str] = []
        self.tier_id = "quick"
        self.variants_per_need = 2
        self.budget_usd: float | None = None
        self.fingerprint = ""

        self.total_plans = 0
        self.completed_plans = 0
        self.spent_usd = 0.0
        self.access_result: AccessAuditResult | None = None
        self.need_reports: list[NeedReport] = []
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
    ) -> None:
        """确认即冻结:任务快照 + config_fingerprint(16.3)。改题 = 新任务新基线。"""
        self.needs = needs
        self.competitors = competitors
        self.surfaces = surfaces
        self.tier_id = tier_id
        self.variants_per_need = variants_per_need
        self.budget_usd = budget_usd
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
            "policy": TIERS[tier_id].policy.model_dump(),
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        self.state = JobState.CONFIRMED
        self.phase = "已确认,等待执行"


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
