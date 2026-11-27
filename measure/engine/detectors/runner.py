"""判定器运行与全量重算(第 3 章步骤 6、13.5)。

- observe_runs:对一批 SurfaceRun 跑一组判定器,可判定的写入派生表(同样的判定不重复写,见 Storage.insert_observation)。
- recompute_by_job:判定器升级后对全部历史重算(硬边界 4)——一单一单来,每单用它自己冻结的品牌卡判它自己的回答,
  以新 detector_version 追加新观测行(旧行保留),并落一条 RecomputeEvent 审计。原来拿一张品牌卡判这个品牌所有单的
  回答,而各单冻结的别名、官网名单可能不同,会给别的单追加按错卡判的行(第二轮审核 B7;用户 2026-10-29 定改按单:
  S12 拿掉「重新生成报告」按钮后,判定器升级的全量重算只剩这条路)。只适用于只看回答与品牌卡的判定器;
  还要别的上下文的(竞品名单、跳转还原、网页核实)随单的「重新生成报告」重算。
  没有审计,指标变化时分不清是世界变了还是判定器变了。日常路径的审计在 engine/observe.persist_observations。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

import structlog

from engine.models import BrandSpec, DerivedObservation, SurfaceRun

log = structlog.get_logger("engine.detectors")


class Detector(Protocol):
    name: str
    version: int

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> DerivedObservation | None: ...


class ObservationStore(Protocol):
    def insert_observation(self, observation: DerivedObservation) -> bool:
        """写入一条判定行;同样的判定已在库里就不写,返回这次有没有写。"""
        ...

    def insert_recompute_event(
        self, *, detector_name: str, version_from: int | None, version_to: int,
        rows_affected: int, trigger_kind: str, note: str | None = None,
    ) -> None: ...
    def record_version_first_seen(
        self, *, detector_name: str, version: int, rows_affected: int, trigger_kind: str, note: str | None = None,
    ) -> bool:
        """某判定器的这个版本第一次在库里落判定行时记一条审计,记过就不再记;返回这次有没有记。"""
        ...
    def job_brands_and_runs(
        self, job_ids: Iterable[str] | None = None,
    ) -> list[tuple[str, BrandSpec, list[SurfaceRun]]]: ...
    def max_detector_version(self, detector_name: str) -> int | None: ...


def observe_runs(
    storage: ObservationStore,
    runs: Iterable[SurfaceRun],
    detectors: list[Detector],
    brand: BrandSpec,
) -> int:
    """跑判定器并落派生表。返回实际写入的观测条数(同样的判定已在库里的不算)。
    detect 返回 None(不可判定)时不写任何东西——缺席就是缺席。"""
    written = 0
    for run in runs:
        for detector in detectors:
            observation = detector.detect(run, brand)
            if observation is not None and storage.insert_observation(observation):
                written += 1
    return written


def recompute_by_job(
    storage: ObservationStore,
    detector: Detector,
    *,
    job_ids: Iterable[str] | None = None,
    trigger_kind: str = "manual",
    note: str | None = None,
) -> int:
    """用当前版本的判定器把历史一单一单重算(每单用它自己冻结的品牌卡),落一条审计;返回实际写入的行数。
    job_ids 不给 = 全部单。同样的判定已在库里的不重复写(见 Storage.insert_observation)。"""
    version_from = storage.max_detector_version(detector.name)
    written = 0
    jobs = 0
    for _job_id, brand, runs in storage.job_brands_and_runs(job_ids):
        written += observe_runs(storage, runs, [detector], brand)
        jobs += 1
    storage.insert_recompute_event(
        detector_name=detector.name,
        version_from=version_from,
        version_to=detector.version,
        rows_affected=written,
        trigger_kind=trigger_kind,
        note=note,
    )
    log.info("recompute_completed", detector=detector.name, jobs=jobs,
             version_from=version_from, version_to=detector.version, rows=written)
    return written
