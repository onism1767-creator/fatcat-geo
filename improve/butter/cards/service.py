"""卡片库的薄外壳:把纯层接到落盘上。自己不写文件(写在 store.py),不读时钟(时间由调用方给)。

  submit   交一个动作:持锁读日志 → 状态机判定 → 合规就追加事件并更新索引;不合规返回原因,什么都不写。
  load     从事件日志推出整个卡片库的当前样子(准的那一份)。
  rebuild  按事件日志整份重建 SQLite 索引。

Butter 不排日程、不提醒、不自己发起测量:这里没有任何定时的东西,只在有人交动作时才动。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from butter import store
from butter.cards.index import index_tables
from butter.cards.machine import apply, decide, draft, fold
from butter.cards.project import project
from butter.contracts import CardActor, CardDecision, CardEvent, CardEventType, CardsView, Frozen


def submit(workspace: Path, *, at: datetime, actor: CardActor, batch_id: str, type: CardEventType,
           payload: Frozen | dict[str, Any] | None = None, card_id: str | None = None) -> CardDecision:
    def step(events: list[CardEvent], _problems: list[str]):
        ledger = fold(events)
        event = draft(ledger, at=at, actor=actor, batch_id=batch_id, type=type, payload=payload, card_id=card_id)
        decision = decide(ledger, event)
        if not decision.ok:
            return decision, [], None
        apply(ledger, event)
        tables = index_tables(project(ledger), {ledger.seen: event}, only_batch=batch_id)
        return decision, [event], tables

    decision, index_problem = store.with_card_log(workspace, step)
    if index_problem:
        decision = decision.model_copy(update={
            "notes": [*decision.notes, f"事件已记下,但索引没更新上({index_problem});重建索引即可"]})
    return decision


def load(workspace: Path, *, as_of: date | None = None) -> tuple[CardsView, list[str]]:
    """返回(卡片库的当前样子, 日志里读不了的行)。as_of 给了才会把过了建议间隔的批次显示成"待再测"。"""
    events, problems = store.read_card_events(workspace)
    return project(fold(events), as_of=as_of), problems


def rebuild(workspace: Path) -> tuple[CardsView, list[str]]:
    events, problems = store.read_card_events(workspace)
    ledger = fold(events)
    view = project(ledger)
    store.rebuild_card_index(workspace, index_tables(view, ledger.accepted))
    return view, problems
