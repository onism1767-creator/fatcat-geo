"""L3 组装:一道题上全部被测对象的指标 → 各对象的四阶段诊断块 + 每家 AI 一句话的原料
(架构书附录 v0.4.24 第 5 节 ④)。

检索视角的借用规则(v0.4.8 双通道)在这里定:能回答的对象优先用本仪器的生成端检索链路;
本仪器不暴露检索链路时,借同一家的裸检索通道;没被借走的裸检索通道单独成块(只有检索视角)。
本模块不含任何 provider/surface 名字符串:「同一家」按运行时的 provider 值配对。
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
from diagnosis.retrieval_diag import diagnose_retrieval
from diagnosis.verdict import VerdictInput, verdict_input
from engine.aggregate import BareRetrieval, NeedSurfaceMetric, TargetMeasurement
from engine.models import CitationEvidence, NeedIntent, Target


def diagnose_need_targets(
    *,
    intent: NeedIntent,
    access: StageFinding,
    measurements: list[TargetMeasurement],
    bare_channels: list[BareRetrieval],
    closed_book: dict[Target, NeedSurfaceMetric],
    evidence_map: dict[str, CitationEvidence],
    brand_name: str,
    registry: list[EvidenceEntry],
) -> tuple[list[SurfaceDiagnosis], dict[Target, VerdictInput]]:
    bare_by_provider: dict[str, BareRetrieval] = {}
    for bare in bare_channels:
        if bare.provider in bare_by_provider:  # 单例槽位:一家只认一台裸检索仪器,多了不静默覆盖
            raise ValueError(
                f"{bare.provider} 选了不止一个裸检索对象({bare_by_provider[bare.provider].target.key}、"
                f"{bare.target.key}):借用关系无法确定,请只保留一个"
            )
        bare_by_provider[bare.provider] = bare

    blocks: list[SurfaceDiagnosis] = []
    verdict_inputs: dict[Target, VerdictInput] = {}
    absorbed: set[Target] = set()
    for m in measurements:
        if m.generative_retrieval is not None:
            g = m.generative_retrieval
            retrieval = diagnose_generative_retrieval(g.presence, g.best_ranks, g.query, g.cited, g.triggered)
        elif m.provider in bare_by_provider:
            borrowed = bare_by_provider[m.provider]
            retrieval = diagnose_retrieval(borrowed.metric, borrowed.best_ranks)
            absorbed.add(borrowed.target)
        else:
            retrieval = diagnose_retrieval(None, [])
        citation = diagnose_citation(
            m.citation, m.competitor_citations,
            verification=summarize_verification(m.brand_cited_urls, evidence_map, brand_name),
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

    for bare in bare_by_provider.values():
        if bare.target in absorbed:
            continue
        blocks.append(diagnose_surface(
            provider=bare.provider,
            surface=bare.target.surface,
            model=bare.target.model,
            access=access,
            retrieval=diagnose_retrieval(bare.metric, bare.best_ranks),
            citation=diagnose_citation(None, [], verification=None),
            representation=diagnose_representation(intent, None, {}),
            intent=intent,
            registry=registry,
        ))
    return blocks, verdict_inputs
