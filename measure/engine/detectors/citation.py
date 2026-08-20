"""引用判定器 v1:品牌域名与引用集的域名匹配。

匹配规则见 engine/detectors/matching.py(与检索排名判定器共享);
规则变更需提版本号并全量重算。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.matching import matches_any_brand_domain
from engine.models import BrandSpec, CitationObservation, Outcome, SurfaceRun

DETECTOR_NAME = "citation_domain_match"
DETECTOR_VERSION = 1


class CitationDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> CitationObservation | None:
        """品牌域名是否进入引用集。以下情形不可判定,返回 None(不产出观测):
        - 运行非 SUCCESS 或无回答;
        - 该 surface 未暴露引用(citations_available=False):
          「引用集为空」与「看不到引用集」是两回事,不得混判为"未被引";
        - 品牌未提供域名(无判定依据)。"""
        if run.outcome is not Outcome.SUCCESS or run.answer is None:
            return None
        if not run.answer.citations_available or not brand.domains:
            return None
        positions = [
            citation.position
            for citation in run.answer.citations
            if matches_any_brand_domain(citation.domain, brand)
        ]
        return CitationObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            cited=bool(positions),
            positions=positions,
        )
