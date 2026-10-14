"""单条回答页的原料(S8.3,架构书 v0.4.26 §10):一条 SurfaceRun + 库里已存的判定行 → 页面要显示的东西。

只读、只翻译,不做任何新判定:提到没提到、引用了谁、跳转终点,都取出报告时落库的观测(每个判定器取最高版本);
摘录用共用的取句原语。外来文字(题目、回答、链接)一律交给模板自动转义。
"""

from __future__ import annotations

from typing import Any

from engine.detectors import brand_mention, citation, sentiment, source_class
from engine.detectors.matching import mention_sentence
from engine.models import BrandDomainList, FrozenModel, SurfaceRun, Target
from engine.validity import is_valid_answer
from report.jobs import DiagnosisJob


class CitationRow(FrozenModel):
    position: int
    url: str  # 原链接(平台给的)
    final_url: str | None = None  # 跳转终点;与原链接相同或没还原时 None
    kind: str  # SourceClass 的值;没有来源归属记录时 "UNKNOWN"
    competitor: str | None = None
    reason: str | None = None  # 看不到的原因码


class ResponseView(FrozenModel):
    run: SurfaceRun
    prompt_text: str
    valid: bool  # 有效回答(成功且正文非空)
    mentioned: bool | None  # None = 没有有效回答,判不了
    excerpt: str | None  # 第一次提到你的那一句
    tone: str | None  # 词表情感:positive / neutral / negative / mixed
    own_sites: list[tuple[str, str]]  # (命中的官网注册域名, 等级:primary / A / B / unknown)
    citations: list[CitationRow]
    unresolved: int  # 跳转去向查不到的引用条数
    detectors: list[tuple[str, int]]  # 这条回答用到的判定器与版本


def run_belongs_to(job: DiagnosisJob, run: SurfaceRun) -> bool:
    """只给看本单的回答:题目在本单题集里,且仪器 × 型号在本单被测对象里。"""
    return (run.prompt_id in {p.prompt_id for p in job.prompts}
            and Target(surface=run.surface, model=run.model) in job.targets)


def _latest(observations: list[dict[str, Any]], detector_name: str) -> dict[str, Any] | None:
    rows = [o for o in observations if o["detector_name"] == detector_name]
    return max(rows, key=lambda o: o["detector_version"]) if rows else None


def _tier(domain_list: BrandDomainList | None, domain: str) -> str:
    if domain_list is None:
        return "unknown"
    if domain == domain_list.primary:
        return "primary"
    for d in domain_list.domains:
        if d.domain == domain:
            return "A" if d.tier.value.startswith("A") else "B"
    return "unknown"


def _citation_rows(run: SurfaceRun, attribution: dict[str, Any] | None) -> list[CitationRow]:
    if attribution is not None and attribution.get("cited") is not None:
        return [
            CitationRow(
                position=entry.get("position") or i,
                url=entry["url"],
                final_url=entry.get("final_url") if entry.get("final_url") != entry["url"] else None,
                kind=entry["source_class"], competitor=entry.get("competitor"), reason=entry.get("reason"),
            )
            for i, entry in enumerate(attribution["cited"], start=1)
        ]
    if run.answer is None:
        return []
    return [CitationRow(position=c.position, url=c.url, kind="UNKNOWN") for c in run.answer.citations]


def build_response_view(job: DiagnosisJob, run: SurfaceRun, observations: list[dict[str, Any]]) -> ResponseView:
    prompt_text = next((p.text for p in job.prompts if p.prompt_id == run.prompt_id), "")
    mention = _latest(observations, brand_mention.DETECTOR_NAME)
    cite = _latest(observations, citation.DETECTOR_NAME)
    tone = _latest(observations, sentiment.DETECTOR_NAME)
    attribution = _latest(observations, source_class.DETECTOR_NAME)
    mentioned = mention["mentioned"] if mention is not None else None
    excerpt = None
    if mentioned and run.answer is not None and mention is not None and mention.get("first_position") is not None:
        excerpt = mention_sentence(run.answer.answer_text, mention["first_position"]).strip()
    return ResponseView(
        run=run,
        prompt_text=prompt_text,
        valid=is_valid_answer(run),
        mentioned=mentioned,
        excerpt=excerpt,
        tone=tone["sentiment"] if tone is not None else None,
        own_sites=[(d, _tier(job.domain_list, d)) for d in (cite.get("matched_domains", []) if cite else [])],
        citations=_citation_rows(run, attribution),
        unresolved=int(cite.get("unresolved", 0)) if cite else 0,
        detectors=sorted({(o["detector_name"], int(o["detector_version"])) for o in observations}),
    )
