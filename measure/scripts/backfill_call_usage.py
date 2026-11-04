"""把老的回答补上计费用量(2026-11-03 成本调研②):call_usage 是派生表,从 surface_run.raw_payload 用各适配器的 usage_of
读出来;原始表一个字不动。再跑一遍只补还没有的行。不调用任何 AI、不花钱。

    .venv\\Scripts\\python -X utf8 scripts/backfill_call_usage.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from engine.storage import Storage, default_dsn  # noqa: E402


def main() -> int:
    load_env_file()
    adapters = build_adapters(os.environ)  # 只用 usage_of,不发请求;没 key 的仪器补不了,如实列出
    filled: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    with Storage(default_dsn()) as storage:
        storage.ensure_schema()
        run_ids = storage.run_ids_without_usage()
        print(f"成功的回答里还没有用量记录的:{len(run_ids)} 条")
        for run_id in run_ids:
            run = storage.run_by_id(run_id)
            adapter = adapters.get(run.surface) if run is not None else None
            usage = adapter.usage_of(run.raw_payload) if adapter is not None and run is not None else {}
            if not usage:
                skipped[run.surface if run is not None else "?"] += 1
                continue
            storage.insert_call_usage(run_id, usage)
            filled[run.surface] += 1
    for surface, n in sorted(filled.items()):
        print(f"  补上 {surface}: {n} 条")
    for surface, n in sorted(skipped.items()):
        print(f"  补不了 {surface}: {n} 条(没加载这台仪器,或原始存档里读不出用量)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
