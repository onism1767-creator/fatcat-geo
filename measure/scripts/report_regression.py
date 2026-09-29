"""手动工具:重构前后,报告是否逐字节不变(2026-09-29 P1 起用)。

在正式库的副本上,用库里已存的样本把指定任务的报告重算一遍,全程断网:
- 任何真实提问 → 立刻中止;
- 官网抓取审计 → 用正式库里该单最新报告存的审计结果(不抓网站);
- 引用核验 → 只读库里已有的核验记录(不限时效),任何抓取 → 立刻中止。
输出报告快照内容(去掉生成时间)+ 任务状态 + 花费,写成排好序的 JSON,重构前后逐字节比较。

只适用于「行为不该变」的改动;有意改变报告内容的改动,比较结果当然会不同。

用法(复制数据库时正式库不能有别的连接;8600 服务只在处理请求时连库,一般不用停——提示复制失败再停):
  .venv\\Scripts\\python scripts\\report_regression.py baseline <目录> [任务号 ...]   # 改代码之前
  .venv\\Scripts\\python scripts\\report_regression.py compare  <目录> [任务号 ...]   # 改代码之后
不给任务号就取库里全部已完成的任务。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diagnosis.access_audit as access_audit_mod  # noqa: E402
import diagnosis.citation_verify as verify_mod  # noqa: E402
import engine.storage as storage_mod  # noqa: E402
from diagnosis.access_audit import AccessAuditResult  # noqa: E402
from diagnosis.recommender import load_evidence_registry  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from report import runner  # noqa: E402
from report.jobs import DiagnosisJob  # noqa: E402

SERVER = "postgresql://geo@localhost:5433/postgres"
LIVE_DB = "geo_tool"
COPY_DB = "geo_regression_copy"


class Blocked(Exception):
    """对照过程中有东西想联网:说明这单不能纯靠已存数据重算,对照作废。"""


def _dsn(db: str) -> str:
    return f"postgresql://geo@localhost:5433/{db}"


def _admin(sql: str) -> None:
    with psycopg.connect(SERVER, autocommit=True, connect_timeout=10) as conn:
        conn.execute(sql)


def _seal_network(adapters: dict) -> None:
    for adapter in adapters.values():
        def blocked(request, _surface=adapter.meta().surface):
            raise Blocked(f"{_surface} 被真实提问了:{request.prompt_text[:40]}")
        adapter.ask = blocked

    recent = storage_mod.Storage.recent_citation_evidence
    storage_mod.Storage.recent_citation_evidence = (  # type: ignore[method-assign]
        lambda self, urls, max_age_days=7: recent(self, urls, max_age_days=10**5))

    def no_fetch(self, original, *args, **kwargs):
        raise Blocked(f"核验想抓网页:{original}")
    verify_mod.CitationVerifier._fetch = no_fetch  # type: ignore[method-assign]


def _recompute(job_id: str, access: AccessAuditResult, adapters: dict) -> dict:
    access_audit_mod.audit = lambda url: access  # type: ignore[assignment]
    with storage_mod.Storage(_dsn(COPY_DB)) as copy:
        snapshot = next(s for s in copy.load_job_snapshots() if s["job_id"] == job_id)
    job = DiagnosisJob.from_snapshot(snapshot)
    runner._execute(job, adapters=adapters, dsn=_dsn(COPY_DB), evidence=load_evidence_registry())
    payload = job.report_payload()
    payload.pop("finished_at")
    return {"state": job.state.value, "spent_usd": round(job.spent_usd, 6), "payload": payload}


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("baseline", "compare"):
        raise SystemExit(__doc__)
    mode, out_dir, job_ids = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    load_env_file()
    adapters = build_adapters(os.environ)
    _seal_network(adapters)
    with storage_mod.Storage(_dsn(LIVE_DB)) as live:  # 先从正式库读完要的东西就关掉:复制时不能有连接
        job_ids = job_ids or [s["job_id"] for s in live.load_job_snapshots() if s["state"] in ("DONE", "PARTIAL")]
        access: dict[str, AccessAuditResult] = {}
        for job_id in job_ids:
            stored = live.latest_report_snapshot(job_id)
            if stored is None:
                raise SystemExit(f"{job_id} 在正式库里没有报告快照,不能对照")
            access[job_id] = AccessAuditResult.model_validate(stored["payload"]["access_result"])
    try:
        _admin(f"DROP DATABASE IF EXISTS {COPY_DB}")
        _admin(f"CREATE DATABASE {COPY_DB} TEMPLATE {LIVE_DB}")
    except psycopg.errors.ObjectInUse as exc:
        raise SystemExit("复制数据库失败:正式库这会儿有别的连接"
                         "(比如 8600 服务正在处理请求、或别的窗口在跑),先停掉服务再跑") from exc
    all_same = True
    try:
        for job_id in job_ids:
            result = json.dumps(_recompute(job_id, access[job_id], adapters),
                                ensure_ascii=False, sort_keys=True, indent=1)
            path = out_dir / f"{job_id}.json"
            if mode == "baseline":
                path.write_text(result, encoding="utf-8")
                print(f"{job_id}: 基线已存 → {path}")
            else:
                same = path.exists() and path.read_text(encoding="utf-8") == result
                all_same &= same
                (out_dir / f"{job_id}.after.json").write_text(result, encoding="utf-8")
                print(f"{job_id}: {'逐字节一致' if same else '不一致(对比 .json 与 .after.json)'}")
    finally:
        _admin(f"DROP DATABASE IF EXISTS {COPY_DB}")
    if not all_same:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
