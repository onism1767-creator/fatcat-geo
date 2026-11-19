"""把过了检查的草稿装进改动包(纯层)。设计 §7.1:同一页同一字段只出一条改动。

- meta 描述、事实块引导句、首屏短答:每条草稿一条改动(来源:模型)。
- 标题、H1 的草稿是「备选写法」:这页已有规则出的标题建议时,备选写在同一条建议里,不另出改动;
  这页没有规则建议时(比如对上的题带夸大词,没照抄),备选单独成一条。
- 作废的、没起草的,写进「没有产出的」,附原因。
"""

from __future__ import annotations

import json

from butter.contracts import ChangeItem, DraftedItem, DraftOutcome, NotGenerated
from butter.registry.ids import ANSWER_FIRST, PRICE_SPECS_COMPARISON, TITLE_QUERY_MATCH
from butter.util import md_cell

MD_FILE = "drafts/drafts.md"
JSON_FILE = "drafts/drafts.json"
ATTEMPTS_FILE = "drafts/attempts.json"
FIELD_LABEL = {"meta_description": "meta 描述", "fact_block_lead": "事实块引导句", "answer_first": "首屏短答",
               "title": "标题(备选写法)", "h1": "H1(备选写法)", "h2": "H2(备选写法)"}
ENTRY = {"meta_description": TITLE_QUERY_MATCH, "fact_block_lead": PRICE_SPECS_COMPARISON, "answer_first": ANSWER_FIRST,
         "title": TITLE_QUERY_MATCH, "h1": TITLE_QUERY_MATCH, "h2": TITLE_QUERY_MATCH}
WHERE = {"meta_description": "<head> 里的 <meta name=\"description\">",
         "fact_block_lead": "价格 / 规格事实块上方,作为一句引导",
         "answer_first": "页面最上面、标题下方,新加一两句"}


def _warnings(item: DraftedItem) -> list[str]:
    return [f"{c.rule}:{c.detail}" for c in item.checks if c.outcome == "警告"]


def model_alternatives(outcome: DraftOutcome | None) -> dict[str, dict[str, str]]:
    """{页面: {title / h1: 备选写法}},给标题建议那一步用。"""
    out: dict[str, dict[str, str]] = {}
    for item in (outcome.items if outcome else []):
        if item.field in ("title", "h1", "h2"):
            out.setdefault(item.page_url, {})[item.field] = item.text
    return out


def model_meta_urls(outcome: DraftOutcome | None) -> frozenset[str]:
    return frozenset(i.page_url for i in (outcome.items if outcome else []) if i.field == "meta_description")


def build_draft_parts(
    outcome: DraftOutcome | None, pages_with_rule_suggestion: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], list[ChangeItem], list[NotGenerated]]:
    if outcome is None:
        return {}, [], []
    items: list[ChangeItem] = []
    missing: list[NotGenerated] = []
    md = ["# 起草的文字", "",
          f"> 由模型起草({outcome.provider}:{outcome.model},提示词 {outcome.prompt_version}),每一条都过了 Butter 的规则检查:"
          "数字、型号、日期必须在你的网站原文或事实库里找得到;不许有夸大和劝说的词;不许有写给 AI 的话。",
          "> 验证态:**假设**。全部是新加或替换短字段,你网站上已有的段落一个字没动。用不用、怎么改,由你定。", ""]
    by_page: dict[str, list[DraftedItem]] = {}
    for item in outcome.items:
        by_page.setdefault(item.page_url, []).append(item)
    for url, drafted in by_page.items():
        md += [f"## {url}", ""]
        alts = [d for d in drafted if d.field in ("title", "h1", "h2")]
        for d in drafted:
            md += [f"### {FIELD_LABEL[d.field]}", "", f"- 现在:{md_cell(d.before, 300) or '(无)'}",
                   f"- 草稿:{md_cell(d.text, 600)}"]
            if d.rewritten:
                md.append("- 这是检查后重写过一次的稿。")
            md += [f"- 提醒:{w}" for w in _warnings(d)]
            md.append("")
            if d.field in ("title", "h1", "h2"):
                continue
            items.append(ChangeItem(
                item_id=f"draft-{len(items) + 1}", kind=d.field, entry_id=ENTRY[d.field],
                action="replace" if d.before else "add", target=f"{url}:{WHERE[d.field]}", files=[MD_FILE, JSON_FILE],
                rationale=f"{FIELD_LABEL[d.field]}:模型按这页已有的事实起草,已过规则检查。",
                validation_state="假设", origin="model", before=d.before, after=d.text, checks=d.checks,
                needs_human=["放上网站前请读一遍:确认说的就是这一页,语气合你的习惯。", *_warnings(d)]))
        if alts and url not in pages_with_rule_suggestion:
            first = alts[0]
            items.append(ChangeItem(
                item_id=f"draft-{len(items) + 1}", kind="heading_suggestion", entry_id=TITLE_QUERY_MATCH, action="review",
                target=f"{url} 的 <title> / H1", files=[MD_FILE, JSON_FILE],
                rationale="标题 / H1 的备选写法:模型按对上这页的题和这页已有的事实起草,已过规则检查。这页没有规则出的标题建议。",
                validation_state="假设", origin="model", before=first.before, after=first.text,
                checks=[c for a in alts for c in a.checks],
                needs_human=["只在这一页确实回答了这道题时采用。", *[w for a in alts for w in _warnings(a)]]))
    if not outcome.items:
        md += ["这次没有一条草稿通过检查。", ""]
    if outcome.voided:
        md += ["## 作废的草稿", ""]
        for v in outcome.voided:
            md.append(f"- {v.page_url} 的 {FIELD_LABEL.get(v.field, v.field)}:{';'.join(v.reasons)}")
            missing.append(NotGenerated(what=f"{v.page_url} 的 {FIELD_LABEL.get(v.field, v.field)}(模型起草)",
                                        reason="没过规则检查,已作废:" + ";".join(v.reasons)))
        md.append("")
    for line in outcome.skipped:
        missing.append(NotGenerated(what="某一页的模型起草", reason=line))
    if outcome.page_warnings:
        md += ["## 要你留意的页面", ""] + [f"- {w}" for w in outcome.page_warnings] + [""]
    md += ["## 花费", "",
           f"- 发了 {outcome.calls} 次请求,共 ${outcome.cost_usd:.4f}(按用量 × 价目表算;每次的明细在 `{ATTEMPTS_FILE}`)。"]
    if outcome.stopped:
        md.append(f"- {outcome.stopped}。")
    files = {
        MD_FILE: "\n".join(md).rstrip() + "\n",
        JSON_FILE: json.dumps({"provider": outcome.provider, "model": outcome.model,
                               "prompt_version": outcome.prompt_version, "cost_usd": outcome.cost_usd,
                               "calls": outcome.calls, "stopped": outcome.stopped,
                               "items": [i.model_dump(mode="json") for i in outcome.items],
                               "voided": [v.model_dump(mode="json") for v in outcome.voided],
                               "skipped": outcome.skipped, "page_warnings": outcome.page_warnings},
                              ensure_ascii=False, indent=2) + "\n",
        ATTEMPTS_FILE: json.dumps([a.model_dump(mode="json") for a in outcome.attempts], ensure_ascii=False,
                                  indent=2) + "\n",
    }
    return files, items, missing
