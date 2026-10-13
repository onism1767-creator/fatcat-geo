"""L3 组装:一个题型上全部被测对象的指标 → 各对象的四阶段诊断块 + 每家 AI 一句话的原料
(架构书附录 v0.4.24 第 5 节 ④;v0.4.26 S2 起按题型聚合)。

检索阶段只认本仪器自己暴露的检索链路(能力 supports_query_trace);不暴露的如实记不可观测,
不从别处借数据、不用引用反推(铁律 7;产品文档 15.3,v0.4.26 S1)。
本模块不含任何 provider/surface 名字符串。
"""

from __future__ import annotations

from diagnosis.citation_diag import diagnose_citation
from diagnosis.citation_verify import summarize_verification
from diagnosis.diagnose import SurfaceDiagnosis, diagnose_surface
from diagnosis.findings import StageFinding
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.recommender import EvidenceEntry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_dependency import diagnose_retrieval_dependency
from diagnosis.retrieval_diag import retrieval_not_observable
from diagnosis.verdict import VerdictInput, verdict_input
from engine.aggregate import ClassSurfaceMetric, TargetMeasurement
from engine.models import CitationEvidence, PromptIntent, Target


def diagnose_class_targets(
    *,
    intent: PromptIntent,
    access: StageFinding,
    measurements: list[TargetMeasurement],
    closed_book: dict[Target, ClassSurfaceMetric],
    evidence_map: dict[str, CitationEvidence],
    brand_name: str,
    registry: list[EvidenceEntry],
) -> tuple[list[SurfaceDiagnosis], dict[Target, VerdictInput]]:
    blocks: list[SurfaceDiagnosis] = []
    verdict_inputs: dict[Target, VerdictInput] = {}
    for m in measurements:
        if m.generative_retrieval is not None:
            g = m.generative_retrieval
            retrieval = diagnose_generative_retrieval(g.presence, g.best_ranks, g.query, g.cited, g.triggered)
        else:
            retrieval = retrieval_not_observable()
        citation = diagnose_citation(
            m.citation, m.competitor_citations,
            verification=summarize_verification(m.brand_cited_urls, evidence_map, brand_name),
            primary=m.primary_citation, by_domain=m.citations_by_domain, unresolved=m.unresolved_citations,
        )
        closed = closed_book.get(m.target)
        blocks.append(diagnose_surface(
            provider=m.provider,
            surface=m.target.surface,
            model=m.target.model,
            access=access,
            retrieval=retrieval,
            citation=citation,
            representation=diagnose_representation(intent, m.mention, m.sentiment_counts),
            intent=intent,
            registry=registry,
            retrieval_dependency=diagnose_retrieval_dependency(m.mention, closed) if closed is not None else None,
        ))
        verdict_inputs[m.target] = verdict_input(intent, m.mention, retrieval, citation)
    return blocks, verdict_inputs
