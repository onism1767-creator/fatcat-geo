"""检索词判定器 v2:品牌名/别名是否进入模型自拟检索词(别名表匹配,拉丁字母按词边界)。

生成端检索链路的上游环节:检索词里没有你,
候选集里就不可能因「搜你」而出现你。与提及判定器共用别名匹配原语,
匹配规则变更须两边同步提版本。
v2(v0.4.26 S3):随匹配原语改词边界同步提版;空回答(SUCCESS 但正文为空)不可判定。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.matching import find_first_alias
from engine.models import NOT_OBSERVABLE, BrandSpec, QueryTraceObservation, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "query_alias_match"
DETECTOR_VERSION = 2


class QueryTraceDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> QueryTraceObservation | None:
        """不可判定返回 None:没有有效回答,或检索词不可观测(NOT_OBSERVABLE ≠ 空列表!)。
        检索词为空(未触发检索)是真实负观测,产出 present=False——
        没写检索词本身就意味着品牌不在检索方向里。"""
        if not is_valid_answer(run):
            return None
        queries = run.search_trace.generated_queries
        if queries == NOT_OBSERVABLE:
            return None
        matched_queries: list[str] = []
        matched_alias: str | None = None
        for query in queries:
            found = find_first_alias(query, brand)
            if found is not None:
                matched_queries.append(query)
                if matched_alias is None:
                    matched_alias = found[1]
        return QueryTraceObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            present=bool(matched_queries),
            matched_alias=matched_alias,
            matched_queries=matched_queries,
        )
