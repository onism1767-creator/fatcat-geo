"""单条回答页的原料(S8.3,架构书 v0.4.26 §10):一条 SurfaceRun + 库里已存的判定行 → 页面要显示的东西。

只读、只翻译,不做任何新判定:提到没提到、引用了谁、跳转终点,都取出报告时落库的观测(每个判定器取最高版本);
摘录用共用的取句原语。外来文字(题目、回答、链接)一律交给模板自动转义。
"""

from __future__ import annotations

from typing import Any

from engine.detectors import citation, source_class
from engine.detectors.brand_mention_judge import DETECTOR_NAME as JUDGE_DETECTOR
from engine.detectors.matching import mention_sentence
from engine.models import BrandDomainList, FrozenModel, SurfaceRun, Target
from engine.observe import mention_detector_name, sentiment_detector_name
from engine.tiers import MentionMethod
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
    mentioned: bool | None  # None = 没有有效回答,判不了,或说不准(unsure)
    unsure: bool  # 只出现了这个词,看不出是不是你(BR4)
    clue: str | None  # 靠什么线索算你(BR4):有风险的名字才有
    excerpt: str | None  # 第一次提到你的那一句
    matched_alias: str | None  # 命中的登记名 / 别名
    matched_text: str | None  # 回答里的命中片段(S9.2,模糊判定器起才有)
    match_kind: str | None  # exact / normalized / fuzzy(规则判定器不填)
    edit_distance: int | None
    tone: str | None  # 词表情感:positive / neutral / negative / mixed
    own_sites: list[tuple[str, str]]  # (命中的官网注册域名, 等级:primary / A / B / unknown)
    citations: list[CitationRow]
    unresolved: int  # 跳转去向查不到的引用条数
    detectors: list[tuple[str, int]]  # 这条回答用到的判定器与版本


def run_belongs_to(job: DiagnosisJob, run: SurfaceRun) -> bool:
    """只给看本单的回答:题目在本单题集里,且仪器 × 型号在本单被测对象里。"""
    return (run.prompt_id in {p.prompt_id for p in job.prompts}
            and Target(surface=run.surface, model=run.model) in job.targets)


def latest_observation(observations: list[dict[str, Any]], detector_name: str) -> dict[str, Any] | None:
    """库里这条回答某个判定器的最新一行观测(版本最高、同版本里最后写入的——判据改了之后重算会追加同版本的新行;
    没有 → None);单条回答页、结果文件打包、装回裁判结论共用。observations 按写入先后排(storage 取判定行一律
    ORDER BY seq 保证)。原来按 payload 里的算出时间比,两行时间一样时随便挑一行(第二轮审核 B5)。"""
    rows = [o for o in observations if o["detector_name"] == detector_name]
    if not rows:
        return None
    top = max(o["detector_version"] for o in rows)
    return [o for o in rows if o["detector_version"] == top][-1]


def mention_row(observations: list[dict[str, Any]], method: MentionMethod) -> dict[str, Any] | None:
    """这条回答算数的那行提及判定:按档位的主判定器;主判定器说不准、裁判判过的,算裁判的(中档,BR4)。"""
    row = latest_observation(observations, mention_detector_name(method))
    if row is not None and row["mentioned"] is None:
        return latest_observation(observations, JUDGE_DETECTOR) or row
    return row


def _tier(domain_list: BrandDomainList | None, domain: str) -> str:
    if domain_list is None:
        return "unknown"
    if domain == domain_list.primary:
        return "primary"
    for d in domain_list.domains:
        if d.domain == domain:
            return "A" if d.tier.value.startswith("A") else "B"
    return "unknown"


def citation_rows(run: SurfaceRun, attribution: dict[str, Any] | None) -> list[CitationRow]:
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
    method = job.frozen_tier().mention_method  # 按档位读已存的判定行,不现算、不需要裁判(第二轮红 3)
    mention = mention_row(observations, method)
    cite = latest_observation(observations, citation.DETECTOR_NAME)
    tone = latest_observation(observations, sentiment_detector_name(method))
    attribution = latest_observation(observations, source_class.DETECTOR_NAME)
    mentioned = mention["mentioned"] if mention is not None else None
    excerpt = None
    if mentioned and run.answer is not None and mention is not None and mention.get("first_position") is not None:
        excerpt = mention_sentence(run.answer.answer_text, mention["first_position"]).strip()
    return ResponseView(
        run=run,
        prompt_text=prompt_text,
        valid=is_valid_answer(run),
        mentioned=mentioned,
        unsure=mention is not None and mentioned is None,
        clue=mention.get("clue") if mention else None,
        excerpt=excerpt,
        matched_alias=mention.get("matched_alias") if mention else None,
        matched_text=mention.get("matched_text") if mention else None,
        match_kind=mention.get("match_kind") if mention else None,
        edit_distance=mention.get("edit_distance") if mention else None,
        tone=tone["sentiment"] if tone is not None else None,
        own_sites=[(d, _tier(job.domain_list, d)) for d in (cite.get("matched_domains", []) if cite else [])],
        citations=citation_rows(run, attribution),
        unresolved=int(cite.get("unresolved", 0)) if cite else 0,
        detectors=sorted({(o["detector_name"], int(o["detector_version"])) for o in observations}),
    )
