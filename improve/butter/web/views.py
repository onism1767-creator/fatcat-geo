"""视图对象(纯层):把数据整理成页面要显示的样子。同一个对象既渲染网页,也原样当 JSON 输出。

给站长看的字都在这里和模板里,按"初中毕业能读懂"写(P4):
不出现 σ、τ、"单次观测""临时""预注册"这类研究用语(设计 §11、§9.6)。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from butter.compare import stats
from butter.config.core import Defaults
from butter.contracts import (
    BatchState, Card, CardsView, GeneratedQuestion, Job, LegProgress, LegResult, MeasurePlan, Pricing, Reading,
)
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
            "note": plain_note(card.validation_note),
            "effects": [line for r in card.observed_effect for line in r.lines],   # 原来的说法,只在 .json 里,给做研究的人看
            "limitations": card.limitations}


def batches(cards_view: CardsView, problems: list[str]) -> dict[str, Any]:
    rows = [{"batch_id": b.batch.batch_id, "site_host": b.batch.site_host, "run_id": b.batch.run_id,
             "phase_text": PHASE_TEXT.get(b.phase, b.phase), "cards": b.cards_created,
             "updated_at": b.updated_at.isoformat(timespec="minutes")}
            for b in sorted(cards_view.batches, key=lambda b: b.updated_at, reverse=True)]
    return {"title": "改动批次", "batches": rows, "problems": problems}


# ── 对比结果的大白话(设计 §9.6)。卡片和给 Toast 的提案里保留原来的说法,只有界面上换 ──────────

WHAT_TEXT = {"mention": "提到你品牌的回答", "citation": "引用你官网的回答"}
VERDICT_TEXT = {
    "up": "明显变多了", "down": "明显变少了", "within": "变化不大，可能只是正常起伏",
    "drift": "看不出来", "not_comparable": "这次没法比", "incomplete": "这次没法比", "config_changed": "这次没法比",
    "baseline_invalid": "这次没法比", "not_deployed": "这批改动还没放上网站", "not_remeasurable": "这类平台不做再测",
}
SETTING_TEXT = {"题板、品牌表或采样设置": "问的题或设置", "引擎型号": "AI 的型号", "搜索工具版本": "AI 的搜索功能"}
API_NOTE = "我们用 AI 公司开放的接口测，结果可能和你在 App 里看到的不一样。"


def _points(value: float) -> str:
    """比例之差 → 「多了 3.3 个百分点」这样的话。"""
    pp = round(value * 100, 1)
    if pp == 0:
        return "没变"
    return f"{'多了' if pp > 0 else '少了'} {abs(pp):g} 个百分点"


def _why_not(leg: LegResult) -> str:
    """没比成、或者比了但不拿来下结论的原因,换成大白话。认不出来的原因照原样给,不吞掉。"""
    reason = leg.reason
    if not reason:
        return ""
    if "搜索过的回答只占" in reason:
        return "改之前那次，这家 AI 很少上网搜，所以不比「引用了谁」。"
    if "处理组只有" in reason:
        return f"对上被改页面的题只有 {leg.n_T} 道，太少，不够比。"
    if "没有这条腿的波动数据" in reason:
        return "还不知道这一家平时自己会晃多少，所以只列数字，不说变没变。"
    if "低于" in reason and "成功" in reason:
        which = "改之后" if reason.startswith("再测") else "改之前"
        return f"{which}那次没问全，成功拿到的回答太少。"
    if reason.startswith("前后不一致"):
        changed = "、".join(SETTING_TEXT.get(x, x) for x in reason.split(":", 1)[-1].split("、"))
        return f"两次测量用的不是同一套东西（{changed}变了），比不了。"
    if "部署" in reason and "基线" in reason or reason == "没有部署日期":
        return "改之前那次测量和放上网站的日子对不上（隔得太久，或者先放上网站才测的）。"
    if "还没部署" in reason:
        return "这批改动还没放上网站。"
    if "对照组自己也超出" in reason:
        return "没改的那些题自己也变了不少，分不清是不是改动带来的。"
    if "全站改动" in reason:
        return "这批里有全站的改动，没有「没改的题」可以对照，所以这一次只作记录，不拿来下结论。"
    if "对照组只有" in reason:
        return "没改的题太少，没法对照，所以这一次只作记录，不拿来下结论。"
    if "数据有问题" in reason:
        return "数据有问题，没比成。"
    return reason


def result_row(leg: LegResult) -> dict[str, Any]:
    """一家 AI、一个指标的对比结果 → 批次页表格里的一行。数照实给,话按 P4 写;不出现「区间」「对照组」「腿」这类词。"""
    notes: list[str] = []
    if leg.d_C is not None:
        note = f"同一段时间里，没改的那些题{_points(leg.d_C)}。"
        if leg.did is not None and leg.primary == "did":
            note += f"把这部分扣掉，是{_points(leg.did)}"
            if leg.band_did is not None:
                note += f"（什么都不改时通常在 ±{leg.band_did * 100:.1f} 个百分点以内）"
            note += "。"
        notes.append(note)
    why = _why_not(leg)
    if why:
        notes.append(why)
    return {
        "name": _leg_name(leg.provider, leg.lang), "what": WHAT_TEXT.get(leg.metric, leg.metric),
        "pre": leg.pre_T, "post": leg.post_T, "diff": _points(leg.d_T) if leg.d_T is not None else "",
        "band_pp": round(leg.band_T * 100, 1) if leg.band_T is not None else None,
        "verdict": VERDICT_TEXT.get(leg.label, leg.label), "notes": notes,
    }


_NOTE_PATTERNS: list[tuple[str, str]] = [
    (r"^已再测 (\d+) 次,只作记录$", r"改之后测过 \1 次，还没到用来下结论的那一次"),
    (r"^未部署.*$", "这一条没放上网站，不算进这次对比"),
    (r"^同批 (\d+) 条改动都挂这一个条目,合起来算一次$", r"这批的 \1 条改动依据的是同一条做法，合起来算一次"),
    (r"^同批 (\d+) 条一起改\(涉及 \d+ 个条目\),不能单独归因;这一批测到的是(变多|变少)$",
     r"这批 \1 条改动是一起放上去的，分不清是哪一条起的作用；这一批整体测到的是\2"),
    (r"^无可辨变化$", "变化不大，可能只是正常起伏"),
    (r"^各条腿方向不一致:(\d+) 条变多,(\d+) 条变少$", r"有的 AI 那边变多、有的变少（\1 家变多，\2 家变少），下不了结论"),
    (r"^这一批含全站类改动.*$", "这批里有全站的改动，没有「没改的题」可以对照，所以不下结论"),
    (r"^没有改之前的测量.*$", "没测过改之前的情况，只有当时的数，没有对比"),
    (r"^基线无效.*$", "改之前那次测量用不上（没测完整，或者和放上网站的日子对不上），所以不下结论"),
    (r"^在波动内\(无有效对照\).*$", "变化不大；而且没有「没改的题」可以对照，不下结论"),
    (r"^超出波动(上升|下降)\(无有效对照\).*$", "测到了变化，但没有「没改的题」可以对照，不下结论"),
    (r"^同期漂移.*$", "没改的那些题自己也变了不少，分不清是不是改动带来的"),
    (r"^不可比.*$", "这次没法比"),
    (r"^不可再测$", "这类平台不做再测"),
]


def plain_note(note: str) -> str:
    """卡片上「现在的结论」下面那行小字,换成大白话。几条并列的(用、隔开)逐条换;认不出来的照原样给。"""
    if not note:
        return ""
    out = []
    for part in note.split("、"):
        for pattern, plain in _NOTE_PATTERNS:
            if re.match(pattern, part):
                part = re.sub(pattern, plain, part)
                break
        if part not in out:
            out.append(part)
    return "；".join(out)


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


CAN_REMEASURE = ("已部署", "待再测", "已比对")  # 放上网站之后可以测"改之后";测过了还能再测


def _no_compare_note(note: str) -> str:
    """放上网站时记下的"为什么没法和改之前比",换成大白话。能比就是空的。"""
    if not note:
        return ""
    if "没有改之前" in note:
        return "这一批没测过改之前的情况，所以再测时只列出当时的数，没有前后对比。"
    if "不完整" in note:
        return "改之前那次没测完整，所以再测时不和它比，只列出当时的数。"
    if "晚于部署" in note:
        return "你填的放上网站的日子，比改之前那次测量还早，所以再测时不和它比，只列出当时的数。"
    return "改之前那次测量离放上网站隔得太久，所以再测时不和它比，只列出当时的数。"


def _reading_view(reading: Reading) -> dict[str, Any]:
    """一次「改之后」的测量。results 是一行一个结果;rows 把同一家 AI 的两个结果并成一行,页面用它排版(列少,好读)。
    每个结果都带着的同一句提醒(比如「这批里有全站的改动」)只在表格下面说一次。"""
    results = [result_row(leg) for leg in reading.legs]
    shared = [n for n in (results[0]["notes"] if results else []) if all(n in x["notes"] for x in results)]
    rows: dict[str, dict[str, Any]] = {}
    for leg, row in zip(reading.legs, results, strict=True):
        cell = {**row, "notes": [n for n in row["notes"] if n not in shared]}
        rows.setdefault(row["name"], {"name": row["name"], "mention": None, "citation": None})[leg.metric] = cell
    samples = [{"name": _leg_name(leg.provider, leg.lang), "mention_rate": leg.mention_rate,
                "citation_rate": leg.citation_rate, "ok": leg.ok, "expected": leg.expected} for leg in reading.samples]
    for sample in samples:
        if sample["name"] in rows:
            rows[sample["name"]] |= {"ok": sample["ok"], "expected": sample["expected"]}
    return {"day": reading.day, "scoring": reading.scoring, "results": results, "rows": list(rows.values()),
            "shared_notes": shared, "samples": samples}


def _days_between(start: str, end: str) -> int | None:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def batch_detail(state: BatchState, cards_view: CardsView, defaults: Defaults, *, plan: MeasurePlan | None = None,
                 plan_error: str | None = None, legs: list[LegProgress] | None = None, alive: bool = False,
                 replan: MeasurePlan | None = None, replan_error: str | None = None, today: str = "",
                 starting: bool = False) -> dict[str, Any]:
    batch = state.batch
    cards = [_card_row(c) for c in cards_view.cards if c.batch_id == batch.batch_id]
    interval = defaults.entry("remeasure.interval_days")
    measuring = state.phase in ("测基线中", "再测中")
    since = _days_between(state.deploy.day, today) if state.deploy and today else None
    if state.deploy is None or since is None:
        remeasure_note = ""
    elif state.scored:
        remeasure_note = "用来下结论的那一次已经测过了。现在再测，结果会接着列在下面，卡片上的结论不变。"
    elif since < int(interval.value):
        remeasure_note = (f"放上网站才 {since} 天。隔满 {interval.value} 天后的第一次测量才用来下结论；"
                          "现在测，结果只列出来看。")
    else:
        remeasure_note = f"放上网站已经 {since} 天，隔满 {interval.value} 天了。这一次的结果会用来下结论。"
    return {
        "title": f"{batch.site_host} 的一批改动", "batch_id": batch.batch_id, "site_host": batch.site_host,
        "run_id": batch.run_id, "phase_text": PHASE_TEXT.get(state.phase, state.phase), "cards": cards,
        "deployed_on": state.deploy.day if state.deploy else "", "baseline_on": state.baseline.day if state.baseline else "",
        "readings": [_reading_view(r) for r in state.readings],
        "abandoned_reason": state.abandoned_reason,
        "remeasure_hint": f"推荐改完隔 {interval.value} 天测一次，看看改动后的效果。",
        "can_measure": state.phase in CAN_MEASURE, "plan": _plan_view(plan) if plan else None,
        "plan_error": plan_error or "", "measured_before": state.baseline is not None,
        # 第二个按钮:记下"放上网站了"
        "can_deploy": state.phase in CAN_MEASURE, "today": today,
        "deploy_warning": ("" if state.baseline is not None else
                           "你还没测过改之前的情况。现在就记的话，以后再测只能看到当时的数，没法和改之前比。"),
        "deployed_cards": len(state.deploy.card_ids) if state.deploy else 0,
        "no_compare_note": _no_compare_note(state.deploy.baseline_note) if state.deploy else "",
        "can_undeploy": state.deploy is not None and not state.readings and not measuring,
        # 第三个按钮:测"改之后"
        "can_remeasure": state.deploy is not None and state.phase in CAN_REMEASURE,
        "replan": _plan_view(replan) if replan else None, "replan_error": replan_error or "",
        "days_since_deploy": since, "remeasure_note": remeasure_note, "remeasured": bool(state.readings),
        "api_note": API_NOTE,
        "measuring": measuring, "driver_alive": alive, "starting": starting,  # starting = 刚点了开始,进程还在起
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


# ── 展示页(静态导出,设计 §12)。这里的话是给第一次听说 Butter 的人看的 ─────────────

SHOWCASE_QUESTIONS = 24   # 波动范围按多少道题来算给人看(Butter 出题默认每个品类 24 道)
SHOWCASE_STEPS = [
    ("改动包已出", "Butter 读完你的网站，列出事实、查出问题，出了一包改动。这时还什么都没测。"),
    ("改之前测过了", "把选好的问题拿去问 AI，记下它们现在提不提你的品牌、引不引你的网页。"),
    ("已放上网站", "你自己把改动放到网站上，回来记下是哪天放的、放了哪几条。Butter 不会替你改网站。"),
    ("已出对比结果", "隔一段时间，把同样的问题再问一遍，和改之前的数摆在一起比。"),
]
SHOWCASE_DOES = [
    "读你给的那个网站，把名称、商品、价格这些事实列出来，每一条都写明是从哪一页读到的。",
    "查出会让 AI 读不到、读不懂你网站的问题。",
    "出一包改动：能直接照着放上网站的内容，每一条都标着依据。",
    "改之前、改之后各问一遍 AI，把两次的数摆在一起。",
]
SHOWCASE_DOES_NOT = [
    "不打分。",
    "不保证改完之后 AI 就会提到你、引用你。",
    "不替你改网站，放不放由你定。",
    "遇到验证码就停，不绕；不冒充别的爬虫。",
]
SHOWCASE_KNOW = [
    "AI 的回答本来就会变。同样的问题、什么都不改，隔 7 天再问，提到一个品牌的回答所占的比例也会差几个到十几个百分点。"
    "所以变化没超过这个范围时，Butter 只说「在平时的波动范围内」，不说有效，也不说没效。",
    "就算超出了范围，也只能说「变了」，不能说「是这次改动造成的」。同一段时间里，AI 自己也在更新。",
    "波动范围是 2026 年 11 月实测出来的：Claude、OpenAI、Gemini 三家，中文和英文，每种语言 50 道题，隔 7 天。"
    "隔得更久会不会更大，还在测。",
    "没有测国内的 AI。",
    "模型起草的文字都过了检查（比如里面的数字要在你的网页上找得到出处），放上网站之前仍然请你自己读一遍。",
]
NO_CHANGE_LABEL = {"within": "在平时的波动范围内", "up": "超出了波动范围（变多）", "down": "超出了波动范围（变少）"}


def _pp(value: float) -> str:
    """比例之差 → 百分点,带正负号。"""
    return f"{value * 100:+.0f}"


def showcase_home(example: dict[str, Any] | None, no_change: dict[str, Any], defaults: Defaults, excluded: list,
                  ) -> dict[str, Any]:
    """展示页首页。example 是那次示例运行的摘要(名字已经换成代号),没有就只出后面几块。"""
    z = defaults.thresholds().z
    tiers = sorted((t for t in defaults.noise_tiers() if t.metric == "mention"), key=lambda t: (t.lang, t.provider))
    bands = [{"name": _leg_name(t.provider, t.lang), "days": t.days,
              "band_pp": round(stats.band_pp(t.sigma, t.tau, SHOWCASE_QUESTIONS, z) * 100, 1)} for t in tiers]
    widest = max((b["band_pp"] for b in bands), default=0) or 1
    for b in bands:
        b["bar"] = round(100 * b["band_pp"] / widest)
    legs = []
    for leg in no_change.get("legs", []):
        ex = leg["example"]
        legs.append({
            "name": _leg_name(leg["provider"], leg["lang"]), "brands": leg["brands"], "within": leg["within"],
            "changed": leg["up"] + leg["down"],
            "example": (f"{ex['questions']} 道题，第一次有 {ex['pre'] * 100:.0f}% 的回答提到它，"
                        f"{no_change.get('gap_days', 7)} 天后是 {ex['post'] * 100:.0f}%，差 {_pp(ex['diff'])} 个百分点；"
                        f"什么都不改时通常在 ±{ex['band'] * 100:.0f} 个百分点以内。"),
            "verdict": NO_CHANGE_LABEL.get(ex["label"], ex["label"]),
        })
    total, within = no_change.get("brands", 0), no_change.get("within", 0)
    return {
        "title": "Butter 展示页", "example": example,
        "steps": [{"name": name, "what": what} for name, what in SHOWCASE_STEPS],
        "no_change": {"legs": legs, "total": total, "within": within, "changed": total - within,
                      "gap_days": no_change.get("gap_days", 7)},
        "bands": bands, "band_questions": SHOWCASE_QUESTIONS,
        "excluded": [{"vendor": v.vendor, "products": v.products, "reason": v.reason, "checked_on": v.checked_on}
                     for v in excluded],
        "does": SHOWCASE_DOES, "does_not": SHOWCASE_DOES_NOT, "know": SHOWCASE_KNOW,
    }
