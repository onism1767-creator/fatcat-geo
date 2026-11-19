"""投影(纯层):账本 → 批次与卡片的当前样子。产品文档 §20.4 的 11 个字段按设计 §8.2 填。

"待再测"是按日期推出来的:给了 as_of(今天的日期,由调用方传入)且已过"部署日 + 建议间隔",已部署就显示成待再测。
它只是一个状态名,Butter 不因此提醒、不催、不自己测。
"""

from __future__ import annotations

from datetime import date, timedelta

from butter.cards.judge import card_status, deployed_entries, judge_batch, scoring_reading, sitewide_deployed
from butter.cards.machine import Ledger, Rec
from butter.cards.methods import summarize_methods
from butter.cards.text import STAGE_TEXT
from butter.contracts import (
    BatchState, Card, CardSpec, CardsView, Conditions, ControlInfo, EvidenceRef, Intervention,
)

ALWAYS = (
    "测到的是 AI 公司开放接口(API)上的结果,只代表这个引擎这个接口,和 App 里看到的可能不一样",
    "变了,不等于是这次改动造成的(Observed change ≠ proven cause)",
)


def recommended_from(rec: Rec) -> str | None:
    if rec.deploy is None or rec.batch.rules is None:
        return None
    gap = rec.batch.rules.thresholds.recommended_gap_days
    return (date.fromisoformat(rec.deploy.day) + timedelta(days=gap)).isoformat()


def phase_as_of(rec: Rec, as_of: date | None) -> str:
    start = recommended_from(rec)
    if rec.phase == "已部署" and as_of is not None and start is not None and as_of >= date.fromisoformat(start):
        return "待再测"
    return rec.phase


def batch_state(rec: Rec, as_of: date | None = None) -> BatchState:
    entries = deployed_entries(rec)
    judged = judge_batch(rec)
    return BatchState(
        batch=rec.batch, phase=phase_as_of(rec, as_of), created_at=rec.created_at, updated_at=rec.updated_at,
        cards_created=len(rec.specs), baseline=rec.baselines[-1] if rec.baselines else None,
        baselines_done=len(rec.baselines), measure_failures=list(rec.failures), deploy=rec.deploy,
        readings=list(rec.readings), scored=scoring_reading(rec) is not None, recommended_from=recommended_from(rec),
        deployed_entries=entries, single_entry=len(entries) == 1, sitewide=sitewide_deployed(rec),
        outcome=judged.outcome, outcome_reason=judged.reason, abandoned_reason=rec.abandoned_reason)


def _control(rec: Rec) -> ControlInfo:
    batch = rec.batch
    if not batch.frozen or batch.rules is None:
        return ControlInfo(note="题还没定(第一次测量时定下)")
    pages = sorted({batch.question_pages[q] for q in batch.control_q if q in batch.question_pages})
    floor = batch.rules.thresholds.min_control
    if sitewide_deployed(rec):
        return ControlInfo(questions=batch.control_q, pages=pages, valid=False,
                           note="这一批含全站类改动,同站没改的页面也受影响:只做了前后对比")
    if len(batch.control_q) < floor:
        return ControlInfo(questions=batch.control_q, pages=pages, valid=False,
                           note=f"对照的题只有 {len(batch.control_q)} 道,少于 {floor} 道:只做了前后对比")
    return ControlInfo(questions=batch.control_q, pages=pages, valid=True,
                       note=f"同站没改的页面对上的 {len(batch.control_q)} 道题")


def _conditions(rec: Rec, spec: CardSpec) -> Conditions:
    samples = [leg for m in rec.baselines for leg in m.legs] + [leg for r in rec.readings for leg in r.samples]
    days = [m.day for m in rec.baselines] + [r.day for r in rec.readings]
    return Conditions(surfaces=rec.batch.surfaces, tool_versions=sorted({s.tool_version for s in samples if s.tool_version}),
                      panel=rec.batch.panel, page=spec.target, industry=spec.industry, sample_days=days)


def _hypothesis(rec: Rec, spec: CardSpec) -> str:
    n = len(rec.batch.treated_q)
    on = f"对上这一处的 {n} 道题上" if n else "对上这一处的题上(题还没定)"
    return f"在「{spec.target}」做这条改动后,{on}本品牌被提到的比例会上升。条目的假设:{spec.entry_hypothesis}"


def _limitations(rec: Rec, deployed: bool, validation: str) -> list[str]:
    out: list[str] = []
    if rec.deploy is not None and deployed:
        n, entries = len(rec.deploy.card_ids), deployed_entries(rec)
        if len(entries) > 1:
            out.append(f"同批 {n} 条一起改(涉及 {len(entries)} 个条目),不能单独归因")
        elif n > 1:
            out.append(f"同批 {n} 条改动挂同一个条目,结果是这几处合起来的")
        if not rec.deploy.baseline_valid:
            out.append(rec.deploy.baseline_note)
    if sitewide_deployed(rec):
        out.append("这一批含全站类改动(robots、全站 JSON-LD、事实页),同站没改的页面也受影响,没有干净的对照")
    elif rec.batch.frozen and rec.batch.rules is not None and len(rec.batch.control_q) < rec.batch.rules.thresholds.min_control:
        out.append("对照的题不够,只做了前后对比")
    if any(leg.metric == "citation" and leg.label == "not_comparable" for r in rec.readings for leg in r.legs):
        out.append("有的引擎很少联网搜索,这些腿不比「引用了谁」")
    if validation == "单次观测":
        out.append("只测到一次,还没有在另一次独立实验里复现")
    if rec.baselines or rec.readings:
        out.append("「不改动时的波动范围」取自 FatCat GEO 2026 年 11 月对大品牌的实测,小品牌未必适用")
        out.append("改前和改后要在同一个网络位置测,Butter 核不了这一点")
    return out + list(ALWAYS)


def card_view(rec: Rec, spec: CardSpec, as_of: date | None = None) -> Card:
    status, judged = card_status(rec, spec.card_id)
    deployed = rec.deploy is not None and spec.card_id in rec.deploy.card_ids
    reading = scoring_reading(rec) or (rec.readings[-1] if rec.readings else None)
    return Card(
        card_id=spec.card_id, batch_id=spec.batch_id, change_item_id=spec.change_item_id, entry_id=spec.entry_id,
        deployed=deployed, phase=phase_as_of(rec, as_of),
        intervention=Intervention(kind=spec.kind, target=spec.target, before=spec.before, after=spec.after),
        target_stage=STAGE_TEXT[spec.entry_stage], hypothesis=_hypothesis(rec, spec),
        evidence=EvidenceRef(entry_id=spec.entry_id, level=spec.entry_level, sources=spec.entry_sources),
        test_conditions=_conditions(rec, spec),
        baseline=rec.baselines[-1].legs if rec.baselines else [],
        post_change=reading.samples if reading and deployed else [],
        control=_control(rec), observed_effect=list(rec.readings) if deployed else [],
        limitations=_limitations(rec, deployed, status.validation),
        validation=status.validation, direction=status.direction, validation_note=status.note,
        outcome=judged.outcome, outcome_reason=judged.reason,
        batch_deployed_cards=len(rec.deploy.card_ids) if rec.deploy else 0)


def project(ledger: Ledger, *, as_of: date | None = None) -> CardsView:
    batches = [batch_state(rec, as_of) for rec in ledger.batches.values()]
    cards = [card_view(rec, spec, as_of) for rec in ledger.batches.values() for spec in rec.specs.values()]
    return CardsView(batches=batches, cards=cards, methods=summarize_methods(ledger), skipped=list(ledger.skipped),
                     events=ledger.seen)
