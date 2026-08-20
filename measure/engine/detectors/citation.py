"""引用判定器 v1:品牌域名与引用集的域名匹配。

匹配规则(v1,升级需提版本号并全量重算):
- 双方均小写、剥离前缀 www. 后比较;
- 相等,或引用域名是品牌域名的子域(endswith "." + 品牌域名)→ 命中。
  注意 "acme.com.evil.net" 不会命中 "acme.com"(后缀必须是完整域名段)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.models import BrandSpec, CitationObservation, Outcome, SurfaceRun

DETECTOR_NAME = "citation_domain_match"
DETECTOR_VERSION = 1


def _normalize(host: str) -> str:
    return host.lower().removeprefix("www.")


def _matches(citation_domain: str, brand_domain: str) -> bool:
    cited, brand = _normalize(citation_domain), _normalize(brand_domain)
    return cited == brand or cited.endswith("." + brand)


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
            if any(_matches(citation.domain, domain) for domain in brand.domains)
        ]
        return CitationObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            cited=bool(positions),
            positions=positions,
        )
