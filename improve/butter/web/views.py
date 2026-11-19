"""视图对象(纯层):把数据整理成页面要显示的样子。同一个对象既渲染网页,也原样当 JSON 输出。

给站长看的字都在这里和模板里,按"初中毕业能读懂"写(P4):
不出现 σ、τ、"单次观测""临时""预注册"这类研究用语(设计 §11、§9.6)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from butter.config.core import Defaults
from butter.contracts import BatchState, Card, CardsView, GeneratedQuestion, Job, LegProgress, MeasurePlan, Pricing
from butter.factedit import editable_fields
from butter.registry.core import Registry

LEVEL_TEXT = {"problem": "问题", "notice": "提示", "pass": "通过", "not_checkable": "查不了", "not_applicable": "不适用"}
ACTION_TEXT = {"add": "新增", "replace": "替换现有的", "none": "不用改", "review": "供你参考"}
KIND_TEXT = {
    "robots": "robots.txt", "jsonld_organization": "机构信息（给机器读的）", "jsonld_product": "商品信息（给机器读的）",
    "fact_page": "事实页", "fact_block": "价格和规格表", "heading_suggestion": "标题和小标题建议",
    "meta_description": "页面描述", "fact_block_lead": "价格规格表上面的一句话", "answer_first": "页面开头的一两句回答",
}
ORIGIN_TEXT = {"rule": "规则生成", "model": "模型起草，已过检查"}
PHASE_TEXT = {"已出包": "改动包已出", "测基线中": "正在测（改之前）", "基线就绪": "改之前测过了", "已部署": "已放上网站",
              "待再测": "已放上网站", "再测中": "正在测（改之后）", "已比对": "已出对比结果", "已放弃": "已放弃"}
INTENT_TEXT = {"discovery": "随便问问", "comparison": "横向比较", "local": "具体场景", "brand_defense": "问你的品牌"}
STAGE_TEXT = {"ACCESS": "能不能被读到", "RETRIEVAL": "能不能被搜到", "CITATION": "会不会被引用",
              "REPRESENTATION": "说得对不对"}
KIND_OF_ENTRY = {"technical_fix": "官方文档要求的做法", "geo_method": "还在验证的做法"}
PROVIDER_TEXT = {"anthropic": "Anthropic(Claude）", "openai": "OpenAI", "gemini": "Google(Gemini）", "grok": "xAI(Grok）"}
DEFAULT_TEXT = {
    "remeasure.interval_days": "改完之后，推荐隔多久再测",
    "compare.baseline_valid_days": "改之前测的结果，几天内放上网站才算数",
    "compare.min_treated_questions": "对上改动页面的题，至少要几道才比",
    "compare.min_control_questions": "对上没改页面的题，至少要几道才当对照",
    "measure.runs_per_question": "每道题问几次",
    "compare.min_success_rate": "成功拿到回答的比例，至少要多少",
    "measure.abort_failure_rate": "失败超过多少就自动停",
    "compare.citation_min_searched_share": "AI 上网搜索的回答至少占多少，才比\"引用了谁\"",
}
HIDDEN_DEFAULTS = ("compare.bootstrap_draws", "compare.seed", "compare.z")  # 纯计算参数,不给站长看


def validation_text(validation: str, direction: str | None = None) -> str:
    """验证状态的大白话（设计 §9.6）。"""
    if validation == "单次观测":
        return "测到一次变多" if direction == "变多" else "测到一次变少" if direction == "变少" else "测到一次变化"
    return {"假设": "还没证实", "实验中": "正在试", "已复现": f"两次都测到{direction or '变化'}",
            "可泛化": f"很多行业都测到{direction or '变化'}"}.get(validation, validation)


def _first(field) -> str:
    return field.values[0].value if field.values else ""


def _counts(findings) -> dict[str, int]:
    out = {key: 0 for key in LEVEL_TEXT}
    for f in findings:
        out[f.level] = out.get(f.level, 0) + 1
    return out


JOB_STATE_TEXT = {"DRAFT": "还没开始", "CONFIRMED": "排队中", "RUNNING": "正在做", "DONE": "做完了",
                  "PARTIAL": "做完了，有几处要留意", "FAILED": "没做成", "INTERRUPTED": "中断了"}


def job(job: Job) -> dict[str, Any]:
    return {"title": "任务进度", "job_id": job.job_id, "state": job.state, "state_text": JOB_STATE_TEXT[job.state],
            "active": job.active, "progress": job.progress, "url": job.params.get("url", ""),
            "created_at": job.created_at, "updated_at": job.updated_at, "error": job.error,
            "run_id": job.result.get("run_id", ""), "batch_id": job.result.get("batch_id", ""),
            "pages": job.result.get("pages", 0), "change_items": job.result.get("change_items", 0),
            "notes": job.result.get("notes", []), "cost_usd": job.result.get("cost_usd", 0),
            "drafted": job.result.get("drafted", 0), "draft_model": job.result.get("draft_model", "")}


def home(runs: list[dict], cards_view: CardsView, offer: dict | None = None, active_jobs: list[Job] | None = None,
         ) -> dict[str, Any]:
    sites: dict[str, dict] = {}
    for row in sorted(runs, key=lambda r: r.get("generated_at", ""), reverse=True):
        site = sites.setdefault(row.get("host", ""), {"host": row.get("host", ""), "runs": []})
        counts = row.get("audit_counts") or {}
        site["runs"].append({
            "run_id": row["run_id"], "site_url": row.get("site_url", ""), "generated_at": row.get("generated_at", ""),
            "problems": counts.get("problem", 0), "notices": counts.get("notice", 0),
            "change_items": row.get("change_items", 0),
        })
    return {"title": "Butter", "sites": list(sites.values()), "run_count": len(runs),
            "batch_count": len(cards_view.batches),
            "draft_offer": ({"provider": PROVIDER_TEXT.get(offer["provider"], offer["provider"]), "model": offer["model"],
                             "upper_bound_usd": offer["upper_bound_usd"]} if offer else None),
            "active_jobs": [{"job_id": j.job_id, "url": j.params.get("url", ""), "progress": j.progress}
                            for j in (active_jobs or [])]}


def run_detail(run: dict, registry: Registry, cards_view: CardsView) -> dict[str, Any]:
    fb, audit, package, meta = run["factbase"], run["audit"], run["package"], run["meta"]
    entries = {e.id: e for e in registry.entries}
    facts = [{"label": label, "value": _first(field), "source": field.values[0].source_url if field.values else "",
              "conflict": field.conflict,
              "edited": bool(field.values) and field.values[0].source_kind == "owner_edit"}
             for label, field in (("名称", fb.entity.name), ("别名", fb.entity.aliases), ("品类", fb.entity.category),
                                  ("官网域名", fb.entity.official_domain), ("简介", fb.entity.description))]
    products = [{"name": _first(p.name), "page_url": p.page_url, "price": " ".join(filter(None, [_first(p.price), _first(p.currency)])),
                 "sku": _first(p.sku), "specs": len(p.specs)} for p in fb.products]
    findings = [{"level": f.level, "level_text": LEVEL_TEXT.get(f.level, f.level), "check": f.check,
                 "page_url": f.page_url or "", "message": f.message, "entry_id": f.entry_id}
                for f in sorted(audit.findings, key=lambda f: list(LEVEL_TEXT).index(f.level) if f.level in LEVEL_TEXT else 9)]
    items = []
    for item in package.items:
        entry = entries.get(item.entry_id)
        warnings = [c.detail for c in item.checks if c.outcome == "警告"]
        items.append({
            "item_id": item.item_id, "kind_text": KIND_TEXT.get(item.kind, item.kind), "action_text": ACTION_TEXT[item.action],
            "target": item.target, "origin_text": ORIGIN_TEXT[item.origin], "before": item.before or "",
            "after": item.after or "", "rationale": item.rationale, "files": item.files,
            "needs_human": item.needs_human, "warnings": warnings, "entry_id": item.entry_id,
            "entry_plain": entry.plain.get("zh", "") if entry else "", "entry_level": entry.evidence_level if entry else "",
            "checks_passed": sum(1 for c in item.checks if c.outcome == "通过"), "validation_text": validation_text(
                "假设") if item.validation_state == "假设" else "官方文档要求这样做；做了不保证有效果",
        })
    draft = meta.get("draft") or None
    batch = next((b for b in cards_view.batches if b.batch.run_id == meta["run_id"]), None)
    return {
        "title": f"{fb.host} 的一次运行", "run_id": meta["run_id"], "site_url": fb.site_url, "host": fb.host,
        "generated_at": meta.get("generated_at", ""), "pages": len(fb.pages), "warnings": meta.get("warnings", []),
        "facts": facts, "products": products, "gaps": [g.message for g in fb.gaps],
        "audit_counts": _counts(audit.findings), "findings": findings,
        "changes": items, "not_generated": [{"what": n.what, "reason": n.reason} for n in package.not_generated],
        "draft": ({"provider": draft["provider"], "model": draft["model"], "accepted": draft["accepted"],
                   "voided": draft["voided"], "cost_usd": meta.get("cost_usd", 0), "stopped": draft.get("stopped", "")}
                  if draft else None),
        "batch_id": batch.batch.batch_id if batch else "",
    }


def facts(run: dict) -> dict[str, Any]:
    fb = run["factbase"]
    groups: list[dict] = []
    for row in editable_fields(fb):
        if not groups or groups[-1]["name"] != row["group"]:
            groups.append({"name": row["group"], "rows": []})
        groups[-1]["rows"].append({"dom_id": f"fact-{sum(len(g['rows']) for g in groups) + 1}",
                                   "field": row["field"], "label": row["label"], "value": row["value"],
                                   "edited": row["edited"], "long": len(row["value"]) > 80})
    return {"title": "改事实", "run_id": run["meta"]["run_id"], "host": fb.host, "groups": groups,
            "edited_count": sum(1 for g in groups for r in g["rows"] if r["edited"])}


def questions(run: dict, inputs: dict, generated: list[GeneratedQuestion], matches: dict[str, list[str]],
              panels: list[dict], saved: str) -> dict[str, Any]:
    fb = run["factbase"]
    groups: list[dict] = []
    for q in generated:
        key = (q.category, q.intent)
        if not groups or groups[-1]["key"] != list(key):
            groups.append({"key": list(key), "category": q.category, "intent_text": INTENT_TEXT[q.intent],
                           "needs_edit": q.needs_edit, "names_brand": q.names_brand, "questions": []})
        groups[-1]["questions"].append({"id": q.id, "text": q.text, "matched_pages": len(matches.get(q.id, []))})
    matched = sum(1 for q in generated if matches.get(q.id))
    return {
        "title": "出题和勾选", "run_id": run["meta"]["run_id"], "host": fb.host, "lang": inputs["lang"],
        "categories": inputs["categories"], "categories_text": "\n".join(inputs["categories"] or inputs["from_facts"]),
        "from_facts": inputs["from_facts"], "brand": inputs["brand"], "groups": groups, "total": len(generated),
        "matched": matched, "panels": panels, "saved": saved,
    }


def _card_row(card: Card) -> dict[str, Any]:
    return {"card_id": card.card_id, "target": card.intervention.target,
            "kind_text": KIND_TEXT.get(card.intervention.kind, card.intervention.kind), "entry_id": card.entry_id,
            "deployed": card.deployed, "state_text": validation_text(card.validation, card.direction),
            "note": card.validation_note, "effects": [line for r in card.observed_effect for line in r.lines],
            "limitations": card.limitations}


def batches(cards_view: CardsView, problems: list[str]) -> dict[str, Any]:
    rows = [{"batch_id": b.batch.batch_id, "site_host": b.batch.site_host, "run_id": b.batch.run_id,
             "phase_text": PHASE_TEXT.get(b.phase, b.phase), "cards": b.cards_created,
             "updated_at": b.updated_at.isoformat(timespec="minutes")}
            for b in sorted(cards_view.batches, key=lambda b: b.updated_at, reverse=True)]
    return {"title": "改动批次", "batches": rows, "problems": problems}


LANG_TEXT = {"zh": "中文", "en": "英文"}
CAN_MEASURE = ("已出包", "基线就绪")  # 这两步可以测"改之前"


def _leg_name(provider: str, lang: str) -> str:
    return f"{PROVIDER_TEXT.get(provider, provider)} · {LANG_TEXT.get(lang, lang)}"


def _plan_view(plan: MeasurePlan) -> dict[str, Any]:
    return {
        "typical_usd": plan.typical_usd, "upper_usd": plan.upper_usd, "minutes": int(plan.minutes),
        "n_treated": plan.n_treated, "n_control": plan.n_control, "band_pp": plan.band_pp, "notes": plan.notes,
        "questions": sum(plan.n_questions.values()), "runs": plan.freeze.plan.runs_per_question,
        "legs": [{"name": _leg_name(leg.provider, leg.lang), "model": leg.model, "calls": leg.calls,
                  "typical_usd": leg.typical_usd, "minutes": int(leg.minutes), "judged": leg.judged}
                 for leg in plan.legs],
    }


def batch_detail(state: BatchState, cards_view: CardsView, defaults: Defaults, *, plan: MeasurePlan | None = None,
                 plan_error: str | None = None, legs: list[LegProgress] | None = None, alive: bool = False,
                 ) -> dict[str, Any]:
    batch = state.batch
    cards = [_card_row(c) for c in cards_view.cards if c.batch_id == batch.batch_id]
    interval = defaults.entry("remeasure.interval_days")
    return {
        "title": f"{batch.site_host} 的一批改动", "batch_id": batch.batch_id, "site_host": batch.site_host,
        "run_id": batch.run_id, "phase_text": PHASE_TEXT.get(state.phase, state.phase), "cards": cards,
        "deployed_on": state.deploy.day if state.deploy else "", "baseline_on": state.baseline.day if state.baseline else "",
        "readings": [{"day": r.day, "lines": r.lines} for r in state.readings],
        "abandoned_reason": state.abandoned_reason,
        "remeasure_hint": f"推荐改完隔 {interval.value} 天测一次，看看改动后的效果。",
        "can_measure": state.phase in CAN_MEASURE, "plan": _plan_view(plan) if plan else None,
        "plan_error": plan_error or "", "measured_before": state.baseline is not None,
        "measuring": state.phase in ("测基线中", "再测中"), "driver_alive": alive,
        "legs": [{"name": _leg_name(leg.provider, leg.lang), "state": leg.state, "done": leg.done, "total": leg.total,
                  "failed": leg.failed, "cost_usd": leg.cost_usd, "note": leg.note} for leg in (legs or [])],
        "last_failure": state.measure_failures[-1] if state.measure_failures else "",
        "baseline_legs": [{"name": _leg_name(leg.provider, leg.lang), "mention_rate": leg.mention_rate,
                           "citation_rate": leg.citation_rate, "ok": leg.ok, "expected": leg.expected}
                          for leg in (state.baseline.legs if state.baseline else [])],
    }


def registry(registry: Registry, cards_view: CardsView) -> dict[str, Any]:
    methods = {m.entry_id: m for m in cards_view.methods}
    used: dict[str, int] = {}
    for card in cards_view.cards:
        used[card.entry_id] = used.get(card.entry_id, 0) + 1
    rows = []
    for e in registry.entries:
        m = methods.get(e.id)
        more = validation_text(m.more.state, "变多") if m and m.more.state else ""
        fewer = validation_text(m.fewer.state, "变少") if m and m.fewer.state else ""
        rows.append({
            "id": e.id, "plain": e.plain.get("zh", ""), "level": e.evidence_level,
            "stage_text": STAGE_TEXT.get(e.stage, e.stage), "kind_text": KIND_OF_ENTRY.get(e.kind, e.kind),
            "cards": used.get(e.id, 0), "more": more, "fewer": fewer,
            "more_count": len(m.more.independent) if m else 0, "fewer_count": len(m.fewer.independent) if m else 0,
            "sources": e.sources.get("zh", [])[:3],
        })
    return {"title": "证据表", "entries": rows, "total": len(rows),
            "sources": [{"origin": s.origin, "entries": s.entries, "snapshot_taken_on": getattr(s, "snapshot_taken_on", None)}
                        for s in registry.sources]}


def settings(out_root: Path, workspace: Path, providers: list[str], defaults: Defaults, pricing: Pricing,
             registry: Registry) -> dict[str, Any]:
    keys = [{"name": PROVIDER_TEXT[p], "ready": p in providers} for p in PROVIDER_TEXT]
    shown = [v for v in defaults.all() if not v.key.startswith("noise.") and v.key not in HIDDEN_DEFAULTS]
    numbers = [{"key": v.key, "label": DEFAULT_TEXT.get(v.key, v.key), "value": v.value, "unit": v.unit,
                "source_plain": v.source_plain} for v in shown]
    noise = [v for v in defaults.all() if v.key.startswith("noise.")]
    return {
        "title": "设置", "out_root": str(out_root), "workspace": str(workspace), "keys": keys, "defaults": numbers,
        "noise_count": len(noise), "noise_source_plain": noise[0].source_plain if noise else "",
        "draft_models": [{"provider": PROVIDER_TEXT.get(p, p), "model": pricing.default_models[p]}
                         for p in pricing.provider_order if p in pricing.default_models],
        "registry_sources": [{"origin": s.origin, "entries": s.entries} for s in registry.sources],
    }
