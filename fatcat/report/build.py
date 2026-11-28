"""把各步骤的结果拼成一份长报告。

这里不产生任何新的结论和数字:Toast 的话原样用,Web Lite 的检查结果原样列,Butter 的事实和改动原样列。
这里只做三件事:决定每样内容放在哪一章(只放一处);按同一条依据把两边的建议合成一张卡;没做的步骤照实写没做。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any
from urllib.parse import urlsplit

from fatcat.modules import ModuleError
from fatcat.report.words import CHAPTERS, SOURCE_LANG, WORDS

MAX_PRODUCT_ROWS = 30
_STAGE_OF = {"retrieval": "RETRIEVAL", "citation": "CITATION", "mention": "REPRESENTATION"}
_ACCESS_CHECK = "meta_robots"  # Butter 的内容检查里属于「读不读得到」的那一项,归第 2 章,不归第 7 章


@dataclass
class Sources:
    """一份报告能用上的东西。哪样没有就是 None,对应的章节写「没做」。"""

    toast: dict[str, Any] | None = None
    butter: dict[str, Any] | None = None
    batches: list[dict[str, Any]] = field(default_factory=list)
    weblite: dict[str, Any] | None = None
    compare: dict[str, Any] | None = None  # 改之前和再测两单的对比(改进工具算的,数来自测量工具存下的判定)


@dataclass
class Block:
    kind: str  # p / note / list / table / card
    text: str = ""
    caption: str = ""
    items: list[str] = field(default_factory=list)
    head: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    sections: list[tuple[str, list[str]]] = field(default_factory=list)  # card:(小标题, 几行)
    lang: str | None = None  # 这一块原文的语言;和报告语言不同时页面上标一句


@dataclass
class Chapter:
    no: int
    key: str
    title: str
    blocks: list[Block]


@dataclass
class Report:
    lang: str
    title: str
    subtitle: str
    warning: str | None
    chapters: list[Chapter]


@dataclass
class Card:
    evidence_id: str
    title: str
    toast: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- 几处结果是不是同一个网站


def _host(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url if "//" in url else "//" + url).hostname
    return host.lower() if host else None


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def site_mismatch(sources: Sources) -> list[str]:
    """各部分说的网站对不上时,返回它们各自的网站;对得上返回空列表。

    有 Toast 时以它认定的官网域名清单为准(那份清单是按公共后缀表算的);没有时只比去掉 www. 之后的主机名。
    """
    hosts: dict[str, str] = {}
    if sources.butter:
        host = sources.butter["run"].get("host") or _host(sources.butter["run"].get("site_url"))
        if host:
            hosts["improve"] = host.lower()
    if sources.weblite:
        host = _host(sources.weblite["manifest"].get("final_url") or sources.weblite["manifest"].get("checked_url"))
        if host:
            hosts["quick check"] = host
    if sources.toast:
        listed = sources.toast.get("domains") or {}
        own = {d["domain"] for d in listed.get("domains", []) if d.get("status") == "confirmed"}
        own |= {listed["primary"]} if listed.get("primary") else set()
        own |= set(sources.toast["job"]["brand"].get("domains") or [])
        toast_host = _host(sources.toast["job"].get("website"))
        if toast_host:
            own.add(toast_host.removeprefix("www."))
        if any(not any(_under(host, domain) for domain in own) for host in hosts.values()):
            return sorted({*hosts.values(), toast_host or next(iter(own), "?")})
        return []
    bare = {host.removeprefix("www.") for host in hosts.values()}
    return sorted(hosts.values()) if len(bare) > 1 else []


# ---------------------------------------------------------------- 建议:同一条依据只出一张卡


def merge_advice(toast_actions: list[dict[str, Any]], butter_items: list[dict[str, Any]]) -> list[Card]:
    """Toast 的建议(evidence_id)和 Butter 的改动(entry_id)用的是同一套证据编号,按编号合并。

    顺序:先按 Toast 排好的优先级,再接只有 Butter 有的。Butter 里「站上已有、无需改动」的不进卡。
    """
    cards: dict[str, Card] = {}
    for action in toast_actions:
        cards.setdefault(action["evidence_id"], Card(action["evidence_id"], action["title"], toast=action))
    for item in butter_items:
        if item.get("action") == "none":
            continue
        card = cards.get(item["entry_id"])
        if card is None:
            card = cards[item["entry_id"]] = Card(item["entry_id"], str(item.get("target") or item["item_id"]))
        card.items.append(item)
    return list(cards.values())


# ---------------------------------------------------------------- 各章


class _Builder:
    def __init__(self, sources: Sources, lang: str) -> None:
        self.s = sources
        self.lang = lang
        self.w = WORDS[lang]
        self.t = sources.toast["text"][lang] if sources.toast else None
        actions = self.t["actions"] if self.t else []
        items = sources.butter["changes"]["items"] if sources.butter else []
        self.cards = merge_advice(actions, items)
        self.already_fine = sum(1 for item in items if item.get("action") == "none")

    def _foreign(self, source: str) -> str | None:
        """这部分原文只有一种语言、又和报告语言不同时,返回它的语言,页面上据此标一句。"""
        return SOURCE_LANG[source] if SOURCE_LANG[source] != self.lang else None

    def _empty(self, key: str) -> list[Block]:
        return [Block("note", text=self.w[key])]

    # 0 一页结论
    def conclusion(self) -> list[Block]:
        w, s, blocks = self.w, self.s, []
        if self.t:
            blocks.append(Block("list", caption=w["c0_ai"], items=list(self.t["ai_lines"])))
            if self.t["wrap_up"]:
                blocks.append(Block("p", text=self.t["wrap_up"]))
        if s.weblite:
            text = self._weblite_counts()
            if self.t and self.t["access"]:  # 两次检查各说各的,读的人会以为矛盾,这里说清是两回事
                text += w["c0_two_checks"]
            blocks.append(Block("p", text=text))
        if s.butter:
            counts = s.butter["run"].get("audit_counts") or {}
            problem, notice = int(counts.get("problem", 0)), int(counts.get("notice", 0))
            pages, changes = len(s.butter["run"].get("pages") or []), sum(len(c.items) for c in self.cards)
            text = w["c0_butter"].format(problem=problem, problem_s=_s(problem), notice=notice, notice_s=_s(notice),
                                         pages=pages, pages_s=_s(pages), changes=changes, changes_s=_s(changes))
            if self.already_fine:
                text += w["c0_already"].format(n=self.already_fine, n_s=_s(self.already_fine))
            blocks.append(Block("p", text=text))
        if self.cards:
            blocks.append(Block("list", caption=w["c0_first"], items=[card.title for card in self.cards[:3]]))
        return blocks

    def _weblite_counts(self) -> str:
        """Quick Check 的一句话:完成了几项、查出几个问题(按严重程度)、剩下的是不适用还是没法检测。"""
        w, findings = self.w, self.s.weblite["findings"]["findings"]
        states = Counter(f["state"] for f in findings)
        severities = Counter(f["severity"] for f in findings if f["state"] == "issue")
        sev = w["list_sep"].join(w["sev_item"].format(n=severities[name], name=w["sev_" + name])
                                 for name in ("critical", "high", "medium", "low") if severities[name])
        done, issues = states["passed"] + states["issue"], states["issue"]
        na, unable = states["not_applicable"], states["unable_to_test"]
        accounted = done + na + unable == len(findings)  # 「其余」只在几样加起来正好是全部时才说
        if na and not unable and accounted:
            rest = w["c0_wl_na" if na > 1 else "c0_wl_na_1"].format(n=na)
        elif unable and not na and accounted:
            rest = w["c0_wl_unable" if unable > 1 else "c0_wl_unable_1"].format(n=unable)
        elif na or unable:
            rest = w["c0_wl_both"].format(na=na, unable=unable)
        else:
            rest = ""
        return w["c0_weblite" if issues else "c0_weblite_clean"].format(
            done=done, total=len(findings), issues=issues, issues_s=_s(issues),
            sev=w["paren"].format(text=sev) if sev else "", rest=rest)

    def _code(self, kind: str, code: Any) -> str:
        """别的工具给的代码(issue、not_applicable、notice 之类)换成这份报告的话;不认识的原样印,不猜。"""
        return self.w.get(f"{kind}_{code}", str(code))

    def _robots_line(self, state: Any, stop: Any) -> str:
        """读网站时 robots.txt 读没读到、为什么停下。改进工具给的是代码;认识的换成话,不认识的原样印。"""
        w = self.w
        if state == "ok" and stop == "completed":  # 最常见的情况单写一句,读着顺
            return w["c1_bt_robots_ok"]
        robots, stopped = w.get(f"robots_{state}"), w.get(f"stop_{stop}")
        if robots and stopped:
            return w["c1_bt_robots"].format(robots=robots, stop=stopped)
        return w["c1_bt_robots_raw"].format(state=state, stop=stop)

    # 1 这次是怎么测的(全报告只此一处)
    def method(self) -> list[Block]:
        w, s, blocks = self.w, self.s, []
        if s.toast:
            items = list(self.t["sampling"])
            if self.t["reminder"]:
                items.append(self.t["reminder"])
            items.append(w["c1_spent"].format(usd=float(s.toast["job"]["spent_usd"] or 0)))
            retired = [target["name"] for target in s.toast["targets"] if not target["still_listed"]]
            if retired:
                items.append(w["c1_retired"].format(names="、".join(retired) if self.lang == "zh" else ", ".join(retired)))
            blocks.append(Block("list", caption=w["c1_ai"], items=items))
        if s.weblite:
            manifest, findings = s.weblite["manifest"], s.weblite["findings"]
            ai_calls = int(findings.get("ai_model_calls") or 0)
            items = [
                w["c1_wl_url"].format(url=manifest.get("checked_url"), final=manifest.get("final_url")),
                w["c1_wl_when"].format(when=_when(findings.get("checked_at") or manifest.get("started_at")),
                                       ruleset=manifest.get("ruleset_version"),
                                       table=manifest.get("crawler_table_version")),
                w["c1_wl_ai" if ai_calls else "c1_wl_ai_none"].format(n=ai_calls, n_s=_s(ai_calls)),
            ]
            if not s.weblite["ruleset_known"]:
                items.append(w["c1_wl_unknown"].format(ruleset=manifest.get("ruleset_version")))
            blocks.append(Block("list", caption=w["c1_weblite"], items=items))
        if s.butter:
            run = s.butter["run"]
            robots = run.get("robots") or {}
            pages, calls = len(run.get("pages") or []), int(run.get("models_called") or 0)
            edits = int(run.get("owner_edits") or 0)
            items = [
                w["c1_bt_site"].format(url=run.get("site_url"), pages=pages, pages_s=_s(pages),
                                       when=_when(run.get("fetched_at"))),
                self._robots_line(robots.get("state"), run.get("stop_reason")),
                w["c1_bt_cost" if calls else "c1_bt_cost_none"].format(calls=calls, calls_s=_s(calls),
                                                                       usd=float(run.get("cost_usd") or 0)),
                *([w["c1_bt_edits"].format(edits=edits)] if edits else []),  # 没改过就不写这一句
            ]
            notes = self._fetch_notes(sitemap=False)
            # 前面几句是这里自己的话;只有带着改进工具的原话时才标「这一部分只有中文」
            blocks.append(Block("list", caption=w["c1_butter"], items=items + notes,
                                lang=self._foreign("butter") if notes else None))
        return blocks or self._empty("not_done")

    def _fetch_notes(self, *, sitemap: bool) -> list[str]:
        """读网站时留下的话。有 Quick Check 时,讲 sitemap 的那几句挪到第 2 章,和 Quick Check 的 sitemap 检查放一起:
        同一个文件的同一个毛病,不在两章各说一遍。没有 Quick Check 时全留在第 1 章。"""
        notes = [str(n) for n in (self.s.butter["run"].get("fetch_notes") or [])] if self.s.butter else []
        if not self.s.weblite:
            return [] if sitemap else notes
        return [n for n in notes if ("sitemap" in n.lower()) == sitemap]

    # 2 读不读得到
    def access(self) -> list[Block]:
        w, s, blocks = self.w, self.s, []
        if s.weblite:
            findings = s.weblite["findings"]["findings"]
            failed = [f for f in findings if f["state"] != "passed"]
            if failed:
                rows = [[f"{f['rule_id']} {f['name']}",
                         self._code("state", f["state"])
                         + (f" · {self._code('sev', f['severity'])}" if f.get("severity") else ""),
                         " · ".join(str(x) for x in (f.get("value"), f.get("observation")) if x),
                         str(f.get("cause_label") or f.get("why") or "")] for f in failed]
                blocks.append(Block("table", caption=w["c2_table"].format(n=len(failed), total=len(findings)),
                                    head=[w["col_rule"], w["col_state"], w["col_seen"], w["col_cause"]], rows=rows,
                                    lang=self._foreign("weblite")))
            else:
                blocks.append(Block("p", text=w["c2_all_passed"].format(total=len(findings))))
            notes = [f"{n['label']}: {n['text']}" for n in s.weblite["findings"].get("informational") or []]
            if notes:
                blocks.append(Block("list", caption=w["c2_info"], items=notes, lang=self._foreign("weblite")))
            if sitemap_notes := self._fetch_notes(sitemap=True):
                blocks.append(Block("list", caption=w["c2_sitemap"], items=sitemap_notes,
                                    lang=self._foreign("butter")))
        if self.t and self.t["access"]:
            access = self.t["access"]
            if s.weblite:  # 同一件事 Quick Check 已经逐项列了,本机那次只留一句结论
                blocks.append(Block("p", text=w["c2_local"].format(status=access["status_label"],
                                                                 headline=access["headline"])))
                if w["c2_toast_refs_if"] in access["headline"].lower():  # 那句话指着测量工具自己的页面时才说明
                    blocks.append(Block("note", text=w["c2_toast_refs"]))
            else:
                blocks.append(Block(
                    "list", caption=f"{w['c2_local_full']}{w['colon']}{access['status_label']} {access['headline']}",
                    items=list(access["evidence"])))
        if s.butter:
            checked = [f for f in s.butter["audit"]["findings"] if f["check"] == _ACCESS_CHECK]
            bad = [f for f in checked if f["level"] in ("problem", "notice")]
            if bad:
                blocks.append(Block(
                    "table", caption=w["c2_pages"].format(n=len(checked), bad=len(bad)),
                    head=[w["col_page"], w["col_level"], w["col_what"]],
                    rows=[[f["page_url"], self._code("level", f["level"]), f["message"]] for f in bad],
                    lang=self._foreign("butter")))
            elif checked:
                blocks.append(Block("p", text=w["c2_pages_ok"].format(n=len(checked), n_s=_s(len(checked)))))
        return blocks or self._empty("not_done")

    # 3 / 4 / 5 检索、引用、提及:Toast 的三步,原话
    def stage(self, key: str) -> list[Block]:
        if not self.t:
            return self._empty("not_done_ai")
        w = self.w
        step = next(s for s in self.t["steps"] if s["stage"] == _STAGE_OF[key])
        blocks: list[Block] = []  # 测量工具给这一步起的名字不印:它只是把章节标题再说一遍
        if key == "citation":
            blocks += self._domains()
        for intent in step["intents"]:
            head = [w["col_ai"], w["col_mention"] if key == "mention" else w["col_own"]]
            rows = []
            for api in intent["apis"]:
                for model in api["models"]:
                    row = [model["name"], f"{model['own']['status_label']} · {model['own']['text']}"]
                    if key != "mention":
                        third = model["third"]
                        row.append(f"{third['status_label']} · {third['text']}" if third else "")
                    rows.append(row)
            if key != "mention":
                head.append(w["col_third"])
            blocks.append(Block("table", caption=w["questions"].format(label=intent["label"], n=intent["n_prompts"]),
                                head=head, rows=rows))
            if key == "mention":
                if intent["compares"]:
                    blocks.append(Block(
                        "table", caption=w["c5_compare"].format(label=intent["label"]),
                        head=[w["col_ai"], w["col_you"], w["col_others"]],
                        rows=[[c["name"], f"{c['brand']['k']}/{c['brand']['n']}",
                               " · ".join(f"{o['name']} {o['k']}/{o['n']}"
                                          + (w["paren"].format(text=o["relation_label"])
                                             if o.get("relation_label") else "")
                                          for o in c["competitors"])] for c in intent["compares"]]))
                if intent["sentiments"]:
                    blocks.append(Block("list", caption=w["c5_tone"].format(label=intent["label"]),
                                        items=[f"{x['name']}{w['colon']}{x['text']}" for x in intent["sentiments"]]))
        if key == "mention" and self.s.toast["spellings"]:
            status = lambda sp: (w["sp_suggested"] if sp["suggested"]  # noqa: E731
                                 else w["sp_registered"] if sp["registered"] else w["sp_other"])
            blocks.append(Block("table", caption=w["c5_spellings"],
                                head=[w["col_spelling"], w["col_count"], w["col_status"]],
                                rows=[[sp["text"], str(sp["count"]), status(sp)] for sp in self.s.toast["spellings"]]))
        return blocks

    def _domains(self) -> list[Block]:
        w, listed = self.w, self.s.toast.get("domains")
        if not listed:
            return []
        def seen(d: dict[str, Any]) -> str:  # 出现过几次照写;内部的档位代码(B1 之类)不印
            n = d.get("seen_count")
            return w["c4_seen" if n != 1 else "c4_seen_1"].format(domain=d["domain"], n=n) if n else d["domain"]

        confirmed = [d["domain"] for d in listed.get("domains", []) if d.get("status") == "confirmed"]
        unconfirmed = [seen(d) for d in listed.get("domains", []) if d.get("status") != "confirmed"]
        items = [w["c4_primary"].format(domain=listed.get("primary"))]
        if confirmed:
            items.append(w["c4_confirmed"].format(domains=w["list_sep"].join(confirmed)))
        if unconfirmed:
            items.append(w["c4_unconfirmed"].format(domains=w["list_sep"].join(unconfirmed)))
        return [Block("list", caption=w["c4_domains"], items=items)]

    # 6 网站上现在写着什么
    def facts(self) -> list[Block]:
        if not self.s.butter:
            return self._empty("not_done_improve")
        w, base, lang = self.w, self.s.butter["factbase"], self._foreign("butter")

        def values(fact: dict[str, Any] | None) -> tuple[str, str]:
            found = (fact or {}).get("values") or []
            return (" · ".join(_clip(str(v.get("value")), 160) for v in found),
                    " · ".join(dict.fromkeys(_where(str(v.get("source_url") or "")) for v in found)))

        entity_rows = [[key, *values(fact)] for key, fact in base.get("entity", {}).items()
                       if isinstance(fact, dict) and fact.get("values")]
        blocks = [Block("table", caption=w["c6_entity"], head=[w["col_field"], w["col_value"], w["col_source"]],
                        rows=entity_rows, lang=lang)]
        products = base.get("products") or []
        if products:
            rows = []
            for product in products[:MAX_PRODUCT_ROWS]:
                price = " ".join(x for x in (values(product.get("price"))[0], values(product.get("currency"))[0]) if x)
                rows.append([values(product.get("name"))[0], price, values(product.get("sku"))[0],
                             values(product.get("availability"))[0], str(product.get("page_url") or "")])
            blocks.append(Block("table", caption=w["c6_products"].format(shown=len(rows), total=len(products)),
                                head=[w["col_product"], w["col_price"], w["col_sku"], w["col_stock"], w["col_page"]],
                                rows=rows, lang=lang))
            if len(products) > MAX_PRODUCT_ROWS:
                blocks.append(Block("p", text=w["c6_more"]))
        gaps = [gap["message"] for gap in base.get("gaps") or []]
        if gaps:
            blocks.append(Block("list", caption=w["c6_gaps"], items=gaps, lang=lang))
        return blocks

    # 7 网站内容查出的问题(「读不读得到」那一项已经在第 2 章)
    def content(self) -> list[Block]:
        if not self.s.butter:
            return self._empty("not_done_improve")
        w = self.w
        findings = [f for f in self.s.butter["audit"]["findings"] if f["check"] != _ACCESS_CHECK]
        shown = [f for f in findings if f["level"] in ("problem", "notice")]
        counts = Counter((f["check"], f["level"]) for f in findings)
        summary = "; ".join(f"{check} {level} {n}" for (check, level), n in sorted(counts.items()))
        blocks = [Block("p", text=w["c7_counts"].format(text=summary))]
        if shown:
            shown.sort(key=lambda f: (f["level"] != "problem", f["check"], f["page_url"]))
            blocks.append(Block("table", caption=w["c7_table"].format(n=len(shown)),
                                head=[w["col_level"], w["col_rule"], w["col_page"], w["col_what"]],
                                rows=[[f["level"], f["check"], f["page_url"], f["message"]] for f in shown],
                                lang=self._foreign("butter")))
        else:
            blocks.append(Block("p", text=w["c7_none"]))
        return blocks

    # 8 建议改什么:一条依据一张卡
    def advice(self) -> list[Block]:
        w = self.w
        if not self.cards and not self.already_fine:
            return self._empty("not_done")
        both = sum(1 for card in self.cards if card.toast and card.items)
        blocks = [Block("p", text=w["c8_stats"].format(
            toast=sum(1 for c in self.cards if c.toast), butter=sum(len(c.items) for c in self.cards),
            cards=len(self.cards), both=both))]
        if self.already_fine:
            blocks.append(Block("p", text=w["c8_already"].format(n=self.already_fine)))
        if any(card.toast for card in self.cards):
            blocks.append(Block("note", text=w["toast_refs"]))
        for card in self.cards:
            sections: list[tuple[str, list[str]]] = []
            if card.toast:
                # Toast 的卡片第一行就是标题那句话,上面已经当标题用了,这里从第二行起
                sections.append((w["card_why"],
                                 [line.strip().removeprefix("- ") for line in card.toast["lines"][1:]]))
                by_ai: dict[str, list[str]] = {}
                for supporter in card.toast["supporters"]:
                    by_ai.setdefault(supporter["ai"], []).append(supporter["intent"])
                sections.append((w["card_who"], [f"{ai}{w['colon']}{w['semi'].join(intents)}"
                                                 for ai, intents in by_ai.items()]))
            if card.items:
                lines = []
                for item in card.items:
                    line = f"{item.get('target')} · {item.get('action')} · {item.get('validation_state')}"
                    if item.get("files"):
                        line += " · " + ", ".join(item["files"])
                    if item.get("needs_human"):
                        line += " · " + w["card_needs_human"].format(n=len(item["needs_human"]))
                    lines.append(line)
                    if item.get("rationale"):
                        lines.append(_clip(str(item["rationale"]), 320))
                sections.append((w["card_what"], lines))
            if not card.toast:  # Toast 的话里已经带着证据编号;只有 Butter 的卡才补这一行
                sections.append((w["card_evidence"].format(id=card.evidence_id), []))
            blocks.append(Block("card", caption=card.title, sections=sections,
                                lang=self._foreign("butter") if card.items else None))
        return blocks

    # 9 改了以后变没变
    def compare(self) -> list[Block]:
        if self.s.compare:
            return self._compared(self.s.compare)
        if not self.s.butter:
            return self._empty("not_done_improve")
        w = self.w
        measured = [b for b in self.s.batches if any("measure" in name for name in b["events"])]
        if not measured:
            blocks = [Block("note", text=w["c9_none"])]
        else:
            blocks = [Block("note", text=w["c9_pending"])]
        for batch in self.s.batches:
            events = ", ".join(f"{name} ×{n}" for name, n in sorted(batch["events"].items()))
            blocks.append(Block("p", text=w["c9_batch"].format(id=batch["batch_id"], cards=batch["cards"],
                                                             events=events)))
        return blocks

    def _compared(self, c: dict[str, Any]) -> list[Block]:
        """改之前和再测两单并排。只列数字:这几个型号平时自己波动多少还没量过,不说变没变。"""
        w = self.w
        groups = c.get("groups") or {}
        blocks = [Block("p", text=w["c9_when"].format(before=c["before"]["day"], after=c["after"]["day"],
                                                     deploy=c.get("deploy_day") or w["c9_no_deploy"]))]
        which = w["c9_all_unprompted" if groups.get("basis") == "all_unprompted" else "c9_given"]
        # 分不出「哪道题对应哪个改过的页面」时,原因在改进工具给的说明里,列在本章末尾
        blocks.append(Block("p", text=which.format(n=len(groups.get("treated") or []),
                                                   control=len(groups.get("control") or []))))
        blocks.append(Block("note", text=w["c9_listed_only"]))

        def cell(leg: dict[str, Any] | None) -> str:
            if leg is None:
                return ""
            (a, b), (x, y) = leg["counts"]["pre"], leg["counts"]["post"]
            rate = lambda k, n: f"{k / n:.0%}" if n else "—"  # noqa: E731
            return w["c9_cell"].format(a=a, b=b, pa=rate(a, b), x=x, y=y, px=rate(x, y))

        by_ai: dict[str, dict[str, dict[str, Any]]] = {}
        for leg in c.get("legs") or []:
            by_ai.setdefault(leg["ai"], {})[leg["metric"]] = leg
        rows, remarks = [], []
        for ai, legs in by_ai.items():
            rows.append([ai, cell(legs.get("mention")), cell(legs.get("citation"))])
            for metric, leg in legs.items():
                if leg.get("reason") and leg["reason"] != _NO_NOISE:  # 「没有波动数据」上面已经统一说过,不逐行重复
                    remarks.append(f"{ai} · {w['c9_metric_' + metric]}{w['colon']}{leg['label_text']} · {leg['reason']}")
        blocks.append(Block("table", caption=w["c9_table"],
                            head=[w["col_ai"], w["c9_col_mention"], w["c9_col_citation"]], rows=rows))
        if remarks:
            blocks.append(Block("list", caption=w["c9_remarks"], items=remarks, lang=self._foreign("butter")))
        extra = list(c.get("notes") or []) + list(c.get("errors") or [])
        if extra:
            blocks.append(Block("list", caption=w["c9_notes"], items=extra, lang=self._foreign("butter")))
        return blocks

    # 10 方法和局限(全报告只此一处)
    def limits(self) -> list[Block]:
        w, s = self.w, self.s
        blocks = [Block("list", caption=w["c10_shared"],
                        items=[w["c10_shared_1"], w["c10_shared_2"], w["c10_shared_3"]])]
        if self.t:
            blocks.append(Block("list", caption=w["c10_ai"], items=[self.t["scope_note"], self.t["api_note"]]))
        if s.weblite and s.weblite.get("methodology"):
            blocks.append(Block("list", caption=w["c10_weblite"],
                                items=list(s.weblite["methodology"].get("known_limitations") or []),
                                lang=self._foreign("weblite")))
        if s.butter:
            blocks.append(Block("list", caption=w["c10_butter"], items=list(s.butter["audit"].get("limitations") or []),
                                lang=self._foreign("butter")))
        return blocks

    # 11 附:证据文件
    def files(self) -> list[Block]:
        w, s, blocks = self.w, self.s, []
        if s.toast:
            blocks.append(Block("list", caption=w["c11_ai"],
                                items=["prompts.csv", "responses.jsonl", "citations.csv", "brand-domains.json",
                                       "summary.md"]))
        if s.weblite:
            blocks.append(Block("list", caption=w["c11_weblite"].format(name=_tail(s.weblite["path"])),
                                items=list(s.weblite["files"])))
        if s.butter:
            blocks.append(Block("list", caption=w["c11_butter"].format(dir=_tail(s.butter["dir"])),
                                items=list(s.butter["files"])))
        return blocks or self._empty("not_done")


_NO_NOISE = "没有这条腿的波动数据"  # 改进工具的原话:这条数照列,只是不判变没变


def _s(n: int) -> str:
    """英文名词的复数尾巴(1 page / 2 pages)。中文模板里没有这个占位,多给的参数会被忽略。"""
    return "" if n == 1 else "s"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tail(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _when(stamp: Any, tz: tzinfo | None = None) -> str:
    """把一个时刻写成「2026-11-24 16:13 UTC-8」,和诊断那一步的写法一样,用这台电脑的时区(tz 只给测试用)。

    三样结果各记各的:诊断记本地时间,Quick Check 和读网站记世界时。原样摆在一起,同一小时里的事看着像隔了一天。
    认不出来的原样返回。"""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return str(stamp) if stamp else ""
    if moment.tzinfo is None:
        return str(stamp)
    local = moment.astimezone(tz)
    minutes = int(local.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
    sign, (hours, rest) = ("-" if minutes < 0 else "+"), divmod(abs(minutes), 60)
    return f"{local:%Y-%m-%d %H:%M} UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def stamp_now() -> str:
    """报告开头「生成于」用的时刻,和正文里的时刻一个写法。"""
    return _when(datetime.now().astimezone().isoformat())


def _where(source: str) -> str:
    """事实是从哪读到的。网址原样;本机文件只留文件名(包里的文件写成「包名 · 里面的文件」)。

    读网站那一步会把本机文件的完整路径记成出处(比如测量结果的下载包)。报告是要拿给别人看的,
    不能把这台电脑上的文件夹和用户名印进去。"""
    if not source or "://" in source or ("/" not in source and "\\" not in source):
        return source
    outer, _, inner = source.partition("!")
    return f"{_tail(outer)} · {inner}" if inner else _tail(outer)


def build(sources: Sources, lang: str, *, generated_at: str, allow_mixed: bool = False) -> Report:
    if lang not in WORDS:
        raise ModuleError(f"报告语言只有 en 和 zh,没有 {lang}")
    if sources.toast is None and sources.butter is None and sources.weblite is None:
        raise ModuleError("一样结果都没有给,出不了报告")
    mismatch = site_mismatch(sources)
    if mismatch and not allow_mixed:
        raise ModuleError("这几份结果说的不是同一个网站:" + "、".join(mismatch)
                          + "。确实要拼在一起看样子,加 --allow-mixed,报告开头会标明。")
    w = WORDS[lang]
    b = _Builder(sources, lang)
    makers = {
        "conclusion": b.conclusion, "method": b.method, "access": b.access,
        "retrieval": lambda: b.stage("retrieval"), "citation": lambda: b.stage("citation"),
        "mention": lambda: b.stage("mention"), "facts": b.facts, "content": b.content, "advice": b.advice,
        "compare": b.compare, "limits": b.limits, "files": b.files,
    }
    chapters = [Chapter(no, key, w["ch_" + key], makers[key]()) for no, key in enumerate(CHAPTERS)]
    if sources.toast:
        title = sources.toast["text"][lang]["title"]
    else:
        site = (sources.butter["run"].get("host") if sources.butter
                else _host(sources.weblite["manifest"].get("final_url")))
        title = f"{site} · {w['report']}"
    return Report(lang=lang, title=title, subtitle=w["generated"].format(when=generated_at),
                  warning=w["mixed"].format(sites=", ".join(mismatch)) if mismatch else None, chapters=chapters)
