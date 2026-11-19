"""索引的行(纯层):卡片库的当前样子 → 要写进 SQLite 的行。SQLite 只是查询加速,事件日志才是准的(ADR-B02)。

这里只算出行,不碰数据库;建表、写入、重建都在 store.py。列名要和 store.py 里的建表语句对得上。
索引里的阶段不含"待再测"(那是按当天日期推出来的显示);要用的一方拿 recommended_from 自己比。
"""

from __future__ import annotations

import json

from butter.contracts import CardEvent, CardsView, Frozen


def _json(model: Frozen) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def event_row(seq: int, event: CardEvent) -> dict:
    return {"seq": seq, "event_id": event.event_id, "at": event.at.isoformat(), "actor": event.actor,
            "batch_id": event.batch_id, "card_id": event.card_id, "type": event.type,
            "payload": json.dumps(event.payload, ensure_ascii=False, sort_keys=True)}


def index_tables(view: CardsView, events: dict[int, CardEvent], *, only_batch: str | None = None) -> dict[str, list[dict]]:
    """events:日志里的序号(从 1 起)→ 被接受的事件。only_batch 给了就只出这一批相关的行(增量更新用)。"""
    batches = [b for b in view.batches if only_batch in (None, b.batch.batch_id)]
    cards = [c for c in view.cards if only_batch in (None, c.batch_id)]
    touched = {c.entry_id for c in cards}
    return {
        "events": [event_row(seq, e) for seq, e in sorted(events.items()) if only_batch in (None, e.batch_id)],
        "batches": [{
            "batch_id": b.batch.batch_id, "site_host": b.batch.site_host, "run_id": b.batch.run_id, "phase": b.phase,
            "baseline_day": b.baseline.day if b.baseline else None, "deploy_day": b.deploy.day if b.deploy else None,
            "recommended_from": b.recommended_from, "scored": int(b.scored), "outcome": b.outcome,
            "cards": b.cards_created, "created_at": b.created_at.isoformat(), "updated_at": b.updated_at.isoformat(),
            "state": _json(b),
        } for b in batches],
        "cards": [{
            "card_id": c.card_id, "batch_id": c.batch_id, "entry_id": c.entry_id, "change_item_id": c.change_item_id,
            "deployed": int(c.deployed), "validation": c.validation, "direction": c.direction, "outcome": c.outcome,
            "card": _json(c),
        } for c in cards],
        "methods": [{
            "entry_id": m.entry_id, "more_state": m.more.state, "more_independent": len(m.more.independent),
            "fewer_state": m.fewer.state, "fewer_independent": len(m.fewer.independent), "cards_total": m.cards_total,
            "summary": _json(m),
        } for m in view.methods if only_batch is None or m.entry_id in touched],
    }
