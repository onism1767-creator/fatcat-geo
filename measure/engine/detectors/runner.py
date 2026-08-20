"""判定器运行与全量重算(第 3 章步骤 6、13.5)。

- observe_runs:对一批 SurfaceRun 跑一组判定器,可判定的写入派生表。
- recompute_all:判定器升级后对全部历史重算——以新 detector_version
  追加新观测行(旧行保留),并落一条 RecomputeEvent 审计。
  没有审计,指标变化时分不清是世界变了还是判定器变了。
"""

from __future__ import annotations

from typing import Iterable, Protocol

import structlog

from engine.models import BrandSpec, DerivedObservation, SurfaceRun

log = structlog.get_logger("engine.detectors")


class Detector(Protocol):
    name: str
    version: int

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> DerivedObservation | None: ...


class ObservationStore(Protocol):
    def insert_observation(self, observation: DerivedObservation) -> None: ...
    def insert_recompute_event(
        self, *, detector_name: str, version_from: int | None, version_to: int,
        rows_affected: int, trigger_kind: str, note: str | None = None,
    ) -> None: ...
    def all_runs(self) -> list[SurfaceRun]: ...
    def max_detector_version(self, detector_name: str) -> int | None: ...


def observe_runs(
    storage: ObservationStore,
    runs: Iterable[SurfaceRun],
    detectors: list[Detector],
    brand: BrandSpec,
) -> int:
    """跑判定器并落派生表。返回写入的观测条数。
    detect 返回 None(不可判定)时不写任何东西——缺席就是缺席。"""
    written = 0
    for run in runs:
        for detector in detectors:
            observation = detector.detect(run, brand)
            if observation is not None:
                storage.insert_observation(observation)
                written += 1
    return written


def recompute_all(
    storage: ObservationStore,
    detector: Detector,
    brand: BrandSpec,
    *,
    trigger_kind: str = "manual",
    note: str | None = None,
) -> int:
    """对全部历史 SurfaceRun 用当前版本判定器重算,并落审计。"""
    version_from = storage.max_detector_version(detector.name)
    written = observe_runs(storage, storage.all_runs(), [detector], brand)
    storage.insert_recompute_event(
        detector_name=detector.name,
        version_from=version_from,
        version_to=detector.version,
        rows_affected=written,
        trigger_kind=trigger_kind,
        note=note,
    )
    log.info("recompute_completed", detector=detector.name,
             version_from=version_from, version_to=detector.version, rows=written)
    return written
