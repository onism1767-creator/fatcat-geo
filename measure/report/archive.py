"""L4 存档:服务启动时从库里恢复任务登记处,并给已完成的任务装回最新一版报告快照(v0.4.9 / v0.4.16)。

库表与 SQL 在 engine/storage.py(对 L2 而言任务快照与报告快照都是不透明的 payload);
payload 的结构与「读不动怎么办」的规矩由这里(L4)定。
"""

from __future__ import annotations

from collections.abc import Collection

import structlog
from pydantic import ValidationError

from engine.storage import Storage
from report.jobs import DiagnosisJob, JobRegistry, JobState, ReportMeta

log = structlog.get_logger("report.archive")


def retired_surfaces(job: DiagnosisJob, known_surfaces: Collection[str]) -> list[str]:
    """任务的被测对象里已不在仪器清单(config/surfaces.yaml)上的仪器。清单上有、只是缺 key 的不算——
    那种任务照常装回,补上 key 就能接着跑。"""
    return sorted({t.surface for t in job.targets if t.surface not in known_surfaces})


def restore_registry(storage: Storage, known_surfaces: Collection[str]) -> JobRegistry:
    """启动恢复(v0.4.9):从库中快照重建任务登记处。
    上次进程死亡时正在跑或待跑的任务(CONFIRMED/RUNNING)标为 INTERRUPTED,
    等用户点「接着跑」——花钱的动作永远由用户触发。
    已完成的任务顺带装回最新一版报告快照(v0.4.16),重启后报告直接可看。
    被测对象含已下线仪器的任务不装回(v0.4.26 S1):程序已不认识那台仪器,报告也无法再算;
    库里的快照与样本原样保留,下线前的报告页存在 docs/报告留档/。"""
    registry = JobRegistry()
    storage.ensure_schema()
    for snapshot in storage.load_job_snapshots():
        try:
            job = DiagnosisJob.from_snapshot(snapshot)
        except (KeyError, ValueError) as exc:  # 快照结构随代码演进(S9.1 加档位):旧快照读不动就记警告跳过,不猜默认值
            log.warning("job_snapshot_unreadable", job_id=snapshot.get("job_id"), error=f"{type(exc).__name__}: {exc}")
            continue
        if retired := retired_surfaces(job, known_surfaces):
            log.warning("job_not_restored_retired_surface", job_id=job.job_id, surfaces=retired)
            continue
        if job.state in (JobState.CONFIRMED, JobState.RUNNING):
            job.state = JobState.INTERRUPTED
            job.phase = "phase_interrupted"
        if job.finished:
            attach_latest_report(job, storage)
        elif job.state is not JobState.DRAFT:
            # 被中断 / 出错的单:快照里的进度停在最后一次写快照那刻,按库里重数问到哪、花了多少
            # (第三轮审核:原来任务页「已问 0/0」、首页「已花费 $0」)
            asked, cost = storage.job_progress([p.prompt_id for p in job.prompts], job.frozen_tier().runs_per_prompt)
            job.completed_runs, job.spent_usd = asked, cost + storage.aux_spent_usd(job.job_id)
            storage.upsert_job_snapshot(job.to_snapshot())
        registry.add(job)
    return registry


def attach_latest_report(job: DiagnosisJob, storage: Storage) -> None:
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
        job.class_reports = []
        job.surface_verdicts = []
        job.sampling = None
        return
    job.report_meta = ReportMeta(
        created_at=row["created_at"], versions=row["versions"], code_version=row["code_version"],
    )
