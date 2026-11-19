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
from butter.contracts import AuditReport, Completion, DraftOutcome, FactBase, PackageBundle, SiteSnapshot
from butter.draft import build_contexts, build_tasks, draft_site
from butter.extract.html import extract_site
from butter.factbase import build_factbase
from butter.generate import build_package
from butter.generate.headings import assign_questions
from butter.render import audit_md, factbase_md
from butter.util import stop_label


@dataclass(frozen=True)
class DraftSettings:
    """起草用哪家、最多花多少。ask 由外壳给:一次调模型的动作(提示词, 输出格式)→ 回答。"""

    provider: str
    model: str
    ask: Callable[[str, dict], Completion]
    budget_usd: float = 1.0
    max_pages: int = 10


@dataclass(frozen=True)
class RunOptions:
    out_root: Path
    brands_path: Path | None = None
    brand_domains_path: Path | None = None
    questions_path: Path | None = None
    training: str = "keep"  # keep = 训练类爬虫不改动;optout = 写 Disallow
    draft: DraftSettings | None = None  # None = 不起草(不调模型、不花钱)


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    snapshot: SiteSnapshot
    factbase: FactBase
    audit: AuditReport
    bundle: PackageBundle
    warnings: list[str] = field(default_factory=list)
    drafts: DraftOutcome | None = None


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
    drafts: DraftOutcome | None = None
    if options.draft is not None and extracts:
        tasks, skipped = build_tasks(factbase, assign_questions(factbase.pages, board),
                                     max_pages=options.draft.max_pages)
        drafts = draft_site(tasks, build_contexts(factbase, tasks, extracts, brands), options.draft.ask,
                            budget_usd=options.draft.budget_usd, skipped=skipped,
                            provider=options.draft.provider, model=options.draft.model)
    bundle = build_package(snapshot, extracts, factbase, ua_table=sources.load_ua_table(),
                           excluded=sources.load_excluded_vendors(), platforms=sources.load_platforms(), board=board,
                           training=options.training, now=moment, drafts=drafts)

    warnings: list[str] = []
    if not extracts:
        warnings.append("没有拿到任何可分析的页面:事实库只有网址和本机文件里的值,五项审计都记「查不了」,"
                        "改动包不含 JSON-LD 和事实页。请看 run.json 里的抓取备注。")
    if snapshot.stop_reason != "completed":
        warnings.append(f"抓取没有走完(原因:{stop_label(snapshot.stop_reason)}),产出只基于已抓到的部分。")

    run_dir = store.new_run_dir(options.out_root, snapshot.host, run_id)
    store.save_snapshot(run_dir, snapshot)
    counts = {level: sum(1 for f in audit.findings if f.level == level)
              for level in ("problem", "notice", "pass", "not_checkable")}
    run_meta = {
        "run_id": run_id, "tool": "butter", "tool_version": butter.__version__, "site_url": snapshot.site_url,
        "host": snapshot.host, "generated_at": moment.isoformat(timespec="seconds"),
        "snapshot_source": snapshot_source, "source": snapshot.source,
        "fetched_at": snapshot.fetched_at.isoformat(timespec="seconds"),
        "user_agent": snapshot.user_agent, "stop_reason": snapshot.stop_reason, "fetch_notes": snapshot.notes,
        "pages": [{"url": p.url, "final_url": p.final_url, "role": p.role, "status": p.status,
                   "html_chars": len(p.html), "error": p.error, "local_file": p.local_file or None,
                   "rendered_dom": p.rendered_dom} for p in snapshot.pages],
        "robots": {"url": snapshot.robots.url, "state": snapshot.robots.state, "status": snapshot.robots.status,
                   "note": snapshot.robots.note},
        "sitemaps_read": snapshot.sitemap_urls, "sitemap_entries": len(snapshot.sitemap_entries),
        "inputs": {"brands": str(options.brands_path) if options.brands_path else None,
                   "brand_domains": str(options.brand_domains_path) if options.brand_domains_path else None,
                   "questions": str(options.questions_path) if options.questions_path else None,
                   "training": options.training},
        "models_called": drafts.calls if drafts else 0, "cost_usd": drafts.cost_usd if drafts else 0,
        "draft": ({"provider": drafts.provider, "model": drafts.model, "prompt_version": drafts.prompt_version,
                   "accepted": len(drafts.items), "voided": len(drafts.voided), "stopped": drafts.stopped}
                  if drafts else None),
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
                     warnings=warnings, drafts=drafts)
