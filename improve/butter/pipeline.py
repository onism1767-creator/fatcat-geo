"""编排:抓取 → 抽取 → 事实库 → 审计 → 生成 → 落盘。本身不含业务规则。

先抓后算:抓取结果先存成快照,后面几步只吃快照。所以离线测试、真实运行、拿旧快照重跑,走的是同一条路。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import butter
from butter import sources, store
from butter.audit import run_audit
from butter.contracts import AuditReport, FactBase, PackageBundle, SiteSnapshot
from butter.extract.html import extract_site
from butter.factbase import build_factbase
from butter.generate import build_package
from butter.render import audit_md, factbase_md


@dataclass(frozen=True)
class RunOptions:
    out_root: Path
    brands_path: Path | None = None
    brand_domains_path: Path | None = None
    questions_path: Path | None = None
    training: str = "keep"  # keep = 训练类爬虫不改动;optout = 写 Disallow


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    snapshot: SiteSnapshot
    factbase: FactBase
    audit: AuditReport
    bundle: PackageBundle
    warnings: list[str] = field(default_factory=list)


def new_run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def process_snapshot(
    snapshot: SiteSnapshot, options: RunOptions, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id: str | None = None, snapshot_source: str = "fetched",
) -> RunResult:
    """快照 → 三样产出并落盘。不联网。"""
    moment = now()
    run_id = run_id or new_run_id(moment)
    brands = sources.load_brands_yml(options.brands_path) if options.brands_path else None
    brand_domains = sources.load_brand_domains_json(options.brand_domains_path) if options.brand_domains_path else None
    board = sources.load_questions_yml(options.questions_path) if options.questions_path else None

    extracts = extract_site(snapshot.pages, snapshot.sitemap_entries)
    factbase = build_factbase(snapshot, extracts, platforms=sources.load_platforms(), run_id=run_id, now=moment,
                              brands=brands, brand_domains=brand_domains)
    audit = run_audit(snapshot, extracts, run_id=run_id, now=moment)
    bundle = build_package(snapshot, extracts, factbase, ua_table=sources.load_ua_table(),
                           excluded=sources.load_excluded_vendors(), platforms=sources.load_platforms(), board=board,
                           training=options.training, now=moment)

    warnings: list[str] = []
    if not extracts:
        warnings.append("没有拿到任何可分析的页面:事实库和改动包基本是空的,请看 run.json 里的抓取备注。")
    if snapshot.stop_reason != "completed":
        warnings.append(f"抓取没有走完(原因:{snapshot.stop_reason}),产出只基于已抓到的部分。")

    run_dir = store.new_run_dir(options.out_root, snapshot.host, run_id)
    store.save_snapshot(run_dir, snapshot)
    counts = {level: sum(1 for f in audit.findings if f.level == level)
              for level in ("problem", "notice", "pass", "not_checkable")}
    run_meta = {
        "run_id": run_id, "tool": "butter", "tool_version": butter.__version__, "site_url": snapshot.site_url,
        "host": snapshot.host, "generated_at": moment.isoformat(timespec="seconds"),
        "snapshot_source": snapshot_source, "fetched_at": snapshot.fetched_at.isoformat(timespec="seconds"),
        "user_agent": snapshot.user_agent, "stop_reason": snapshot.stop_reason, "fetch_notes": snapshot.notes,
        "pages": [{"url": p.url, "final_url": p.final_url, "role": p.role, "status": p.status,
                   "html_chars": len(p.html), "error": p.error} for p in snapshot.pages],
        "robots": {"url": snapshot.robots.url, "state": snapshot.robots.state, "status": snapshot.robots.status,
                   "note": snapshot.robots.note},
        "sitemaps_read": snapshot.sitemap_urls, "sitemap_entries": len(snapshot.sitemap_entries),
        "inputs": {"brands": str(options.brands_path) if options.brands_path else None,
                   "brand_domains": str(options.brand_domains_path) if options.brand_domains_path else None,
                   "questions": str(options.questions_path) if options.questions_path else None,
                   "training": options.training},
        "models_called": 0, "cost_usd": 0,
        "audit_counts": counts, "change_items": len(bundle.package.items), "warnings": warnings,
    }
    store.save_outputs(run_dir, factbase=factbase, factbase_md=factbase_md(factbase), audit=audit,
                       audit_md=audit_md(audit, snapshot), bundle=bundle, run_meta=run_meta)
    store.append_run_index(options.out_root, {
        "run_id": run_id, "host": snapshot.host, "site_url": snapshot.site_url,
        "generated_at": moment.isoformat(timespec="seconds"), "run_dir": str(run_dir),
        "stop_reason": snapshot.stop_reason, "audit_counts": counts, "change_items": len(bundle.package.items),
    })
    return RunResult(run_id=run_id, run_dir=run_dir, snapshot=snapshot, factbase=factbase, audit=audit, bundle=bundle,
                     warnings=warnings)
