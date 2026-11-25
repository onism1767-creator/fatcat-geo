"""读 Butter 一次运行写下的文件,和它的批次事件。只读。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fatcat.modules import ModuleError

_RUN_FILES = {
    "run": "run.json",
    "factbase": "factbase.json",
    "audit": "audit.json",
    "changes": "change-package/manifest.json",
}


def load_run(run_dir: Path) -> dict[str, Any]:
    """一次运行的四个文件。缺哪个就明说缺哪个,不拿空的顶。"""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ModuleError(f"找不到 Butter 的运行文件夹:{run_dir}")
    loaded: dict[str, Any] = {"dir": str(run_dir)}
    for key, name in _RUN_FILES.items():
        path = run_dir / name
        if not path.is_file():
            raise ModuleError(f"Butter 的运行文件夹里没有 {name}:{run_dir}")
        try:
            loaded[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModuleError(f"读不了 Butter 的 {name}:{exc}") from exc
    loaded["files"] = sorted(str(p.relative_to(run_dir)).replace("\\", "/")
                             for p in run_dir.rglob("*") if p.is_file() and "snapshot" not in p.parts)
    return loaded


def find_runs(out_dir: Path) -> list[dict[str, Any]]:
    """`out/<站点>/<运行号>/run.json` 下的每次运行,新的在前。"""
    out_dir = Path(out_dir)
    runs = []
    for path in out_dir.glob("*/*/run.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append({"run_id": run.get("run_id"), "host": run.get("host"), "site_url": run.get("site_url"),
                     "generated_at": run.get("generated_at"), "change_items": run.get("change_items"),
                     "dir": str(path.parent)})
    return sorted(runs, key=lambda r: str(r["generated_at"]), reverse=True)


def batches_for(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    """这次运行名下的批次,和每个批次发生过哪些事(按事件类型计数)。

    批次的事实只在事件日志里(`events/cards.jsonl`,只追加)。这里只数事件,不重算状态:状态怎么从事件推出来是
    Butter 的事。没有工作区或没有批次,返回空列表。
    """
    log = Path(workspace) / "events" / "cards.jsonl"
    if not log.is_file():
        return []
    batches: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "batch_created" and payload.get("run_id") == run_id:
            batches[event["batch_id"]] = {
                "batch_id": event["batch_id"], "created_at": event.get("at"),
                "cards": len(payload.get("card_ids") or []), "events": Counter(),
            }
    for event in events:
        batch = batches.get(event.get("batch_id") or "")
        if batch is not None:
            batch["events"][event.get("type") or "unknown"] += 1
    return [dict(b, events=dict(b["events"])) for b in batches.values()]
