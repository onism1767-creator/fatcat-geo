"""检索排名判定器 v1:品牌域名在检索候选集中的位次。

原始层只记录"平台按什么顺序返回了哪些来源"(RetrievedSource.rank);
"目标品牌排到第几"需要域名归属判定,属于本判定器——这是当初把
retrieval_rank 从单值字段改为逐来源记录的全部理由(架构书附录条目 2)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.matching import matches_any_brand_domain
from engine.models import (
    NOT_OBSERVABLE,
    BrandSpec,
    Outcome,
    RetrievalRankObservation,
    SurfaceRun,
)

DETECTOR_NAME = "retrieval_rank_domain_match"
DETECTOR_VERSION = 1


class RetrievalRankDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> RetrievalRankObservation | None:
        """不可判定返回 None:非 SUCCESS、候选集不可观测(NOT_OBSERVABLE
        ≠ 空集!)、或品牌未提供域名。候选集为空是真实负观测,正常产出
        present=False。"""
        if run.outcome is not Outcome.SUCCESS or not brand.domains:
            return None
        sources = run.search_trace.retrieved_sources
        if sources == NOT_OBSERVABLE:
            return None
        matched = [s for s in sources if matches_any_brand_domain(s.domain, brand)]
        observable_ranks = [s.rank for s in matched if s.rank != NOT_OBSERVABLE]
        return RetrievalRankObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            present=bool(matched),
            best_rank=min(observable_ranks) if observable_ranks else None,
            matched_domains=[s.domain for s in matched],
        )
