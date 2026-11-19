"""卡片状态机(纯层)。设计 §8.3:状态全部由事件推出;没有"改状态"这种操作,撤销也是追加一条事件。

    已出包 ─测基线─► 测基线中 ─完成─► 基线就绪 ─我已部署─► 已部署 ─(按日期推出)─► 待再测
                        │失败(可重测)                               │再测并比对
                        ▼                                           ▼
                   回到原来的状态                              再测中 ─► 已比对
    任何状态都可"放弃" ─► 已放弃(之后什么都不收)

  check(ledger, event)   这条事件在当前状态下合不合规。不合规返回原因,不抛异常。
  apply(ledger, event)   把一条事件记进账本(先 check,不合规就跳过并记下原因)。
  fold(events)           从头重放整份日志。
  decide(ledger, event)  给外壳用:合规 → 接受(带事件和提醒);不合规 → 拒绝(带原因)。

几条写死的规矩:
- 测量只能由站长发起(actor = owner):Butter 不排日程、不提醒、不自己发起测量(设计 §8.4)。
- 第一次测量开始时冻结题板、引擎、处理组与对照组、判读规则;之后要改就是新批次。
- 基线没就绪也能标部署,但记"基线无效:只看时序,不作比对"。
- 过了建议间隔后的第一次再测是唯一计分的读点;别的时间测的按时间追加,不改卡片结局。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from butter.cards.text import effect_line
from butter.contracts import (
    Batch, BatchFreeze, CardActor, CardDecision, CardEvent, CardEventType, CardSpec, Deployment, DeployMark,
    Frozen, MeasureDone, MeasureFailed, Measurement, MeasureStart, OwnerNote, Reading, SkippedEvent,
)

OWNER_ONLY = frozenset({"baseline_started", "remeasure_started", "deployed", "deploy_reverted", "abandoned"})
PAYLOAD: dict[str, type[Frozen]] = {
    "batch_created": Batch, "card_created": CardSpec,
    "baseline_started": MeasureStart, "remeasure_started": MeasureStart,
    "baseline_finished": MeasureDone, "remeasure_finished": MeasureDone,
    "baseline_failed": MeasureFailed, "remeasure_failed": MeasureFailed,
    "deployed": DeployMark, "deploy_reverted": OwnerNote, "abandoned": OwnerNote,
}
FREEZE_FIELDS = ("panel", "brands", "surfaces", "plan", "treated_q", "control_q", "question_pages", "rules")


@dataclass
class Rec:
    """一个批次在账本里的记录。phase 是存下来的阶段;"待再测"只是按日期另算的显示,不存。"""

    batch: Batch
    created_at: datetime
    updated_at: datetime
    specs: dict[str, CardSpec] = field(default_factory=dict)
    phase: str = "已出包"
    back: str = "已出包"  # 测量失败后回到哪
    baselines: list[Measurement] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    deploy: Deployment | None = None
    readings: list[Reading] = field(default_factory=list)
    abandoned_reason: str = ""


@dataclass
class Ledger:
    batches: dict[str, Rec] = field(default_factory=dict)
    card_owner: dict[str, str] = field(default_factory=dict)  # 卡号 → 批次号(批次登记时就占住)
    event_ids: set[str] = field(default_factory=set)
    accepted: dict[int, CardEvent] = field(default_factory=dict)  # 日志里的序号(从 1 起)→ 被接受的事件
    seen: int = 0
    skipped: list[SkippedEvent] = field(default_factory=list)


class Verdict(NamedTuple):
    reason: str | None  # None = 合规
    payload: Any = None
    notes: tuple[str, ...] = ()


def _no(reason: str) -> Verdict:
    return Verdict(reason)


def parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _parse_payload(event: CardEvent) -> tuple[Any, str | None]:
    try:
        return PAYLOAD[event.type].model_validate(event.payload), None
    except ValueError as exc:  # pydantic 的 ValidationError 是 ValueError 的子类
        errors = getattr(exc, "errors", None)
        first = errors()[0] if callable(errors) and errors() else None
        where = ".".join(str(p) for p in first["loc"]) if first else ""
        return None, f"事件内容不合规:{where + ':' if where else ''}{first['msg'] if first else exc}"


def _freeze_problem(freeze: BatchFreeze) -> str | None:
    if not freeze.panel:
        return "冻结参数里没有题板"
    if not freeze.surfaces:
        return "冻结参数里没有要测的引擎"
    if freeze.plan.runs_per_question < 1:
        return "每题至少要跑 1 次"
    both = sorted(set(freeze.treated_q) & set(freeze.control_q))
    if both:
        return f"同一道题不能既在处理组又在对照组:{both[:3]}"
    return None


def _same_freeze(batch: Batch, freeze: BatchFreeze) -> bool:
    return all(getattr(batch, name) == getattr(freeze, name) for name in FREEZE_FIELDS)


def min_success_rate(rec: Rec) -> float:
    return rec.batch.rules.thresholds.min_success_rate if rec.batch.rules else 0.9


def baseline_status(rec: Rec, deploy_day: date) -> tuple[bool, str]:
    """设计 §8.3"我已部署"一行:要有完整基线,且 基线日 ≤ 部署日 ≤ 基线日 + 有效天数。不满足也让标,但记基线无效。"""
    if not rec.baselines:
        return False, "没有改之前的测量:再测时只有当时的情况,没有对比"
    current = rec.baselines[-1]
    if not current.complete:
        return False, "基线无效:改之前那次测量不完整,只看时序,不作比对"
    lead = (deploy_day - date.fromisoformat(current.day)).days
    valid_days = rec.batch.rules.thresholds.baseline_valid_days if rec.batch.rules else 7
    if lead < 0:
        return False, "基线无效:基线晚于部署,只看时序,不作比对"
    if lead > valid_days:
        return False, f"基线无效:部署在基线之后 {lead} 天,超过 {valid_days} 天,只看时序,不作比对"
    return True, ""


def _check_start(rec: Rec, payload: MeasureStart) -> Verdict:
    missing = [c for c in rec.batch.card_ids if c not in rec.specs]
    if missing:
        return _no(f"批次里还有 {len(missing)} 张卡没建好:{missing[:3]}")
    if not rec.batch.frozen:
        if payload.freeze is None:
            return _no("第一次测量要带上冻结参数(题板、引擎、处理组与对照组、判读规则)")
        problem = _freeze_problem(payload.freeze)
        return _no(problem) if problem else Verdict(None, payload)
    if payload.freeze is not None and not _same_freeze(rec.batch, payload.freeze):
        return _no("题板、引擎、分组和判读规则在第一次测量时就定下了,不能改;要改请新建一个批次")
    return Verdict(None, payload)


def _check_done(rec: Rec, payload: MeasureDone) -> str | None:
    if parse_day(payload.day) is None:
        return f"日期读不懂:{payload.day!r}(要写成 2026-11-19 这样)"
    if not payload.legs:
        return "测量结果里一条腿都没有"
    if any(leg.expected < 0 or leg.ok < 0 or leg.ok > max(leg.expected, 0) for leg in payload.legs):
        return "测量结果里的成功数、应有数对不上"
    return None


def check(ledger: Ledger, event: CardEvent) -> Verdict:
    """这条事件在当前状态下合不合规。总是返回结果,不抛异常。"""
    if event.event_id in ledger.event_ids:
        return _no(f"事件号重复:{event.event_id}")
    payload, problem = _parse_payload(event)
    if problem:
        return _no(problem)
    kind = event.type
    if kind in OWNER_ONLY and event.actor != "owner":
        return _no("这一步只能由站长来点:测不测、部署没部署、放不放弃,都由站长决定,Butter 不替站长做")
    rec = ledger.batches.get(event.batch_id)

    if kind == "batch_created":
        if rec is not None:
            return _no(f"批次 {event.batch_id} 已经有了")
        if payload.batch_id != event.batch_id:
            return _no("事件上的批次号和内容里的批次号不一样")
        if not payload.card_ids:
            return _no("批次里一张卡都没有")
        if len(set(payload.card_ids)) != len(payload.card_ids):
            return _no("批次里的卡号有重复")
        taken = [c for c in payload.card_ids if c in ledger.card_owner]
        if taken:
            return _no(f"卡号已经被别的批次用了:{taken[:3]}")
        if payload.frozen:
            return _no("新建的批次还没测过,不能标成已冻结")
        return Verdict(None, payload)

    if rec is None:
        return _no(f"没有这个批次:{event.batch_id}")
    if rec.phase == "已放弃":
        return _no("这一批已经放弃,不再接受任何操作")

    if kind == "card_created":
        if event.card_id != payload.card_id or payload.batch_id != event.batch_id:
            return _no("事件上的卡号、批次号和内容里的不一样")
        if payload.card_id not in rec.batch.card_ids:
            return _no(f"批次 {event.batch_id} 里没有登记卡 {payload.card_id}")
        if payload.card_id in rec.specs:
            return _no(f"卡 {payload.card_id} 已经建过了")
        if rec.phase != "已出包" or rec.batch.frozen:
            return _no("这一批已经开始测量或部署,不能再加卡;要加请新建一个批次")
        return Verdict(None, payload)

    if kind == "baseline_started":
        if rec.phase in ("测基线中", "再测中"):
            return _no("这一批正在测,等这次测完再说")
        if rec.phase not in ("已出包", "基线就绪"):
            return _no("已经标记部署了,改之前的情况测不了了;想看现在的情况请用再测")
        return _check_start(rec, payload)

    if kind in ("baseline_finished", "baseline_failed"):
        if rec.phase != "测基线中":
            return _no("这一批现在没有在测基线")
        if kind == "baseline_finished":
            problem = _check_done(rec, payload)
            if problem:
                return _no(problem)
            if payload.result is not None:
                return _no("基线只是改之前测一次,不带比对结果")
        return Verdict(None, payload)

    if kind == "deployed":
        if rec.phase in ("测基线中", "再测中"):
            return _no("这一批正在测,等测完(或失败)再标记部署")
        if rec.phase not in ("已出包", "基线就绪"):
            return _no("这一批已经标记过部署;要改请先撤销")
        missing = [c for c in rec.batch.card_ids if c not in rec.specs]
        if missing:
            return _no(f"批次里还有 {len(missing)} 张卡没建好:{missing[:3]}")
        if not payload.card_ids:
            return _no("至少要勾一条实际部署了的改动")
        if len(set(payload.card_ids)) != len(payload.card_ids):
            return _no("勾选的卡有重复")
        unknown = [c for c in payload.card_ids if c not in rec.specs]
        if unknown:
            return _no(f"这一批里没有这些卡:{unknown[:3]}")
        day = parse_day(payload.day)
        if day is None:
            return _no(f"日期读不懂:{payload.day!r}(要写成 2026-11-19 这样)")
        if day > event.at.date() + timedelta(days=1):
            return _no(f"部署日期 {payload.day} 还没到,不能填将来的日期")
        valid, note = baseline_status(rec, day)
        return Verdict(None, payload, () if valid else (note,))

    if kind == "deploy_reverted":
        if rec.phase == "再测中":
            return _no("这一批正在再测,等测完再说")
        if rec.deploy is None:
            return _no("这一批还没标记部署,没有可撤销的")
        if rec.readings:
            return _no("这一批已经再测过,部署标记撤不了;不想要这一批可以放弃它")
        return Verdict(None, payload)

    if kind == "remeasure_started":
        if rec.phase in ("测基线中", "再测中"):
            return _no("这一批正在测,等这次测完再说")
        if rec.deploy is None:
            return _no("还没标记部署;没改就想再测一次,请用改之前的那个「测一次」")
        return _check_start(rec, payload)

    if kind in ("remeasure_finished", "remeasure_failed"):
        if rec.phase != "再测中":
            return _no("这一批现在没有在再测")
        if kind == "remeasure_finished":
            problem = _check_done(rec, payload)
            if problem:
                return _no(problem)
            if date.fromisoformat(payload.day) < date.fromisoformat(rec.deploy.day):
                return _no(f"再测日期 {payload.day} 早于部署日期 {rec.deploy.day}")
            if rec.baselines and payload.result is None:
                return _no("这一批有基线,再测完要带上比对结果")
            if not rec.baselines and payload.result is not None:
                return _no("这一批没有基线,没有可比的,不该带比对结果")
            if payload.result is not None and payload.result.batch_id != event.batch_id:
                return _no("比对结果是别的批次的")
        return Verdict(None, payload)

    if kind == "abandoned":
        return Verdict(None, payload)
    return _no(f"不认识的事件类型:{kind}")  # CardEventType 之外的走不到这里,留作保险


def _reading(rec: Rec, done: MeasureDone) -> Reading:
    since = (date.fromisoformat(done.day) - date.fromisoformat(rec.deploy.day)).days
    gap = rec.batch.rules.thresholds.recommended_gap_days if rec.batch.rules else 28
    scoring = since >= gap and not any(r.scoring for r in rec.readings)
    result = done.result
    return Reading(
        day=done.day, days_since_deploy=since, scoring=scoring, samples=done.legs,
        legs=result.legs if result else [], lines=[effect_line(leg) for leg in result.legs] if result else [],
        judged=result.judged if result else 0, exceeded=result.exceeded if result else 0,
        errors=result.errors if result else [])


def _record(ledger: Ledger, event: CardEvent, payload: Any) -> None:
    kind = event.type
    if kind == "batch_created":
        ledger.batches[event.batch_id] = Rec(batch=payload, created_at=event.at, updated_at=event.at)
        for card_id in payload.card_ids:
            ledger.card_owner[card_id] = event.batch_id
        return
    rec = ledger.batches[event.batch_id]
    rec.updated_at = event.at
    if kind == "card_created":
        rec.specs[payload.card_id] = payload
    elif kind in ("baseline_started", "remeasure_started"):
        if not rec.batch.frozen:
            update = {name: getattr(payload.freeze, name) for name in FREEZE_FIELDS}
            rec.batch = rec.batch.model_copy(update={**update, "frozen": True})
        rec.back = rec.phase
        rec.phase = "测基线中" if kind == "baseline_started" else "再测中"
    elif kind == "baseline_finished":
        floor = min_success_rate(rec)
        complete = all(leg.expected > 0 and leg.ok / leg.expected >= floor for leg in payload.legs)
        rec.baselines.append(Measurement(day=payload.day, legs=payload.legs, complete=complete))
        rec.phase = "基线就绪"
    elif kind in ("baseline_failed", "remeasure_failed"):
        what = "基线" if kind == "baseline_failed" else "再测"
        rec.failures.append(f"{event.at.date().isoformat()} {what}没测成:{payload.reason}")
        rec.phase = rec.back
    elif kind == "deployed":
        valid, note = baseline_status(rec, date.fromisoformat(payload.day))
        rec.deploy = Deployment(day=payload.day, card_ids=payload.card_ids, at=event.at, baseline_valid=valid,
                                baseline_note=note)
        rec.phase = "已部署"
    elif kind == "deploy_reverted":
        rec.deploy = None
        rec.phase = "基线就绪" if rec.baselines else "已出包"
    elif kind == "remeasure_finished":
        rec.readings.append(_reading(rec, payload))
        rec.phase = "已比对"
    elif kind == "abandoned":
        rec.abandoned_reason = payload.reason
        rec.phase = "已放弃"


def apply(ledger: Ledger, event: CardEvent) -> Verdict:
    """把一条事件记进账本。不合规的不记,写进 skipped。返回这次的判定。"""
    ledger.seen += 1
    verdict = check(ledger, event)
    if verdict.reason is not None:
        ledger.skipped.append(SkippedEvent(seq=ledger.seen, event_id=event.event_id, type=event.type,
                                           reason=verdict.reason))
        return verdict
    ledger.event_ids.add(event.event_id)
    ledger.accepted[ledger.seen] = event
    _record(ledger, event, verdict.payload)
    return verdict


def fold(events: list[CardEvent]) -> Ledger:
    ledger = Ledger()
    for event in events:
        apply(ledger, event)
    return ledger


def decide(ledger: Ledger, event: CardEvent) -> CardDecision:
    """只判定,不改账本。接受时把事件原样带回,由外壳追加进日志。"""
    verdict = check(ledger, event)
    if verdict.reason is not None:
        return CardDecision(ok=False, reason=verdict.reason)
    return CardDecision(ok=True, event=event, notes=list(verdict.notes))


def draft(ledger: Ledger, *, at: datetime, actor: CardActor, batch_id: str, type: CardEventType,
          payload: Frozen | dict[str, Any] | None = None, card_id: str | None = None) -> CardEvent:
    """起一条待判定的事件。事件号 = 日志里的第几条 + 内容指纹,不用随机数、不读时钟(时间由调用方给)。"""
    body = payload.model_dump(mode="json") if isinstance(payload, Frozen) else dict(payload or {})
    seq = ledger.seen + 1
    digest = hashlib.sha256(json.dumps([seq, at.isoformat(), actor, batch_id, card_id, type, body], sort_keys=True,
                                       ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:8]
    return CardEvent(event_id=f"e{seq:06d}-{digest}", at=at, actor=actor, batch_id=batch_id, card_id=card_id,
                     type=type, payload=body)
