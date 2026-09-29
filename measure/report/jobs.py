"""L4 任务模型与登记处(第 16.3 章):状态机、冻结快照与指纹、成本预估。

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
from pydantic import ValidationError

from diagnosis.access_audit import AccessAuditResult
from diagnosis.diagnose import NeedDiagnosis
from diagnosis.verdict import SurfaceVerdict
from engine.detectors.matching import find_first_alias
from engine.models import (
    BrandSpec,
    CompetitorSuggestion,
    FrozenModel,
    InformationNeed,
    NeedIntent,
    Target,
)
from engine.needs import NeedCandidate
from engine.scheduler import SamplingPolicy

log = structlog.get_logger("report.jobs")

PLAN_CONCURRENCY = 3  # 采样计划并发路数(16.1)

# 档位(v0.4.15,用户拍板方案 1):按次数定义——「每问法最多 max_n 次,前 N 次结果完全一样
# 就提前停」。宽度阈值在实际参数下与之等价(混合结果永不提前停),
# tests/unit/test_report_page.py 用 Jeffreys 算术机械核对标签里的数字。
# 预估口径(16.4 / v0.4.6):成本按 surface 分价——单价由各适配器 meta 声明
# (est_cost_per_call_usd,实测均值或推导上界);时间仍用双通道实测均值,
# 留待多 surface 实测后一并校准。
# 兜底值(v0.4.17):库里没有足量历史时用;有历史即按 surface 实测均值(香奈儿首单加权均值 20.2 s)
AVG_SECONDS_PER_CALL = 20.0


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


TIERS: dict[str, Tier] = {
    "standard": Tier(id="standard", policy=SamplingPolicy(), expected_calls_per_plan=18.0),
    "quick": Tier(
        id="quick", policy=SamplingPolicy(min_n=5, max_n=8, ci_width_threshold=0.35),
        expected_calls_per_plan=7.5,
    ),
}


def manual_intent(text: str, brand: BrandSpec) -> NeedIntent:
    """用户手填的问题归哪个场景:点了品牌名或别名的就是「直接询问品牌」,否则按「没有点名品牌」。
    点名的问题里品牌提及率天然接近 100%,混进「没点名」场景会把「AI 会主动推荐你」的结论虚高。"""
    return NeedIntent.BRAND_PROBE if find_first_alias(text, brand) is not None else NeedIntent.DISCOVERY


def preselect_candidates(candidates: list[NeedCandidate], *, n_manual: int) -> list[int]:
    """确认页默认勾哪几道题(v0.4.18):用户自己填的(列表末尾 n_manual 道)全勾;
    一道没填就只勾 2 道——第一个不点名品牌的题 + 第一个直接问品牌的题。
    默认报价必须便宜,想多测由用户自己勾(预估实时更新)。"""
    if n_manual:
        return list(range(len(candidates) - n_manual, len(candidates)))
    first_open = next((i for i, c in enumerate(candidates) if c.intent is not NeedIntent.BRAND_PROBE), None)
    first_brand = next((i for i, c in enumerate(candidates) if c.intent is NeedIntent.BRAND_PROBE), None)
    return [i for i in (first_open, first_brand) if i is not None]


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
    seconds_per_call: float = AVG_SECONDS_PER_CALL,
) -> Estimate:
    """surface_costs = 所选各 surface 的单次调用期望成本(有历史按实测均值,否则适配器声明);
    dependency_costs = 启用检索依赖对照时各支持 surface 的闭卷腿单价
    (闭卷腿与开卷同采样策略,故每腿期望调用数相同);seconds_per_call 同理按实测。"""
    dependency_costs = dependency_costs or []
    per_leg_calls = n_needs * variants_per_need * TIERS[tier_id].expected_calls_per_plan
    calls = per_leg_calls * (len(surface_costs) + len(dependency_costs))
    return Estimate(
        calls=round(calls),
        cost_usd=per_leg_calls * (sum(surface_costs) + sum(dependency_costs)),
        minutes=calls * seconds_per_call / 60 / PLAN_CONCURRENCY,
    )


class NeedReport(FrozenModel):
    need: InformationNeed
    variant_texts: list[str]
    diagnosis: NeedDiagnosis


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
        snapshot = {
            "brand": self.brand.model_dump(),
            "website": self.website,
            "category": self.category,
            "locale": self.locale,
            "needs": [{"statement": n.statement, "intent": n.intent.value} for n in needs],
            "competitors": [c.model_dump() for c in competitors],
            "targets": [t.model_dump() for t in targets],
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
        return {
            "access_result": (
                self.access_result.model_dump(mode="json") if self.access_result is not None else None
            ),
            "need_reports": [r.model_dump(mode="json") for r in self.need_reports],
            "surface_verdicts": [v.model_dump(mode="json") for v in self.surface_verdicts],
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def load_report(self, payload: dict) -> None:
        self.access_result = (
            AccessAuditResult.model_validate(payload["access_result"])
            if payload["access_result"] is not None else None
        )
        self.need_reports = [NeedReport.model_validate(r) for r in payload["need_reports"]]
        self.surface_verdicts = [SurfaceVerdict.model_validate(v) for v in payload["surface_verdicts"]]
        self.finished_at = (
            datetime.fromisoformat(payload["finished_at"]) if payload["finished_at"] else None
        )

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


def restore_registry(dsn: str) -> JobRegistry:
    """启动恢复(v0.4.9):从库中快照重建任务登记处。
    上次进程死亡时正在跑或待跑的任务(CONFIRMED/RUNNING)标为 INTERRUPTED,
    等用户点「接着跑」——花钱的动作永远由用户触发。
    已完成的任务顺带装回最新一版报告快照(v0.4.16),重启后报告直接可看。"""
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
            elif job.finished:
                attach_latest_report(job, storage)
            registry.add(job)
    return registry


def attach_latest_report(job: DiagnosisJob, storage) -> None:
    """把库里最新一版报告快照装回任务;没有快照就保持空报告(首页显示「重新生成报告」)。

    报告结构随代码演进,旧快照可能读不动(v0.4.17 首次发生:建议新增 verify 字段):
    这时不加兼容层、不猜——记警告,花费照常装回,报告留空让用户「重新生成报告」
    (追加新版本,旧快照仍留档)。"""
    row = storage.latest_report_snapshot(job.job_id)
    if row is None:
        return
    job.spent_usd = row["spent_usd"]
    try:
        job.load_report(row["payload"])
    except ValidationError as exc:
        log.warning("report_snapshot_unreadable", job_id=job.job_id,
                    code_version=row["code_version"], errors=exc.error_count())
        job.need_reports = []
        job.surface_verdicts = []
        return
    job.report_meta = ReportMeta(
        created_at=row["created_at"], versions=row["versions"], code_version=row["code_version"],
    )
