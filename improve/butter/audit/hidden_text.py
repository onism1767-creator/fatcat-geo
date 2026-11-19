"""检查四:隐藏文本与面向 AI 的指令。

依据(research/04 G7):Google 垃圾政策把试图操纵生成式 AI 回答列入 spam;Bing 指南有提示注入与 AI 操纵章节。
分级:
- 问题:人眼看不到的位置(隐藏元素、注释、alt、meta、JSON-LD)里有写给 AI 的指令;
- 提示:可见正文里有写给 AI 的话(讨论提示注入的文章也会命中);用典型藏字手法藏了较多文字;
- display:none / hidden 属性的界面文字(菜单、弹窗)很常见,只计数,不算问题。
"""

from __future__ import annotations

from butter.audit.base import finding
from butter.audit.instructions import find_instructions
from butter.contracts import AuditFinding, PageExtract
from butter.registry.ids import HIDDEN_TEXT_INJECTION
from butter.util import snippet

CHECK = "hidden_text"
_SUSPICIOUS_MIN_CHARS = 100


def check(extracts: list[PageExtract]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for page in extracts:
        page_findings: list[AuditFinding] = []

        # 一处文字命中几条规则也只报一条:规则名并在一起,片段取第一处
        for hidden in page.hidden_texts:
            if hits := find_instructions(hidden.text):
                page_findings.append(finding(
                    CHECK, HIDDEN_TEXT_INJECTION, "problem", page.url,
                    f"隐藏的文字里有写给 AI 的指令(隐藏方式:{hidden.reason})。",
                    location=hidden.location, snippet=hits[0][1], rule="、".join(r for r, _ in hits)))
        visible_hits = find_instructions(page.visible_text)
        in_visible = {rule for rule, _ in visible_hits}
        for carrier in page.attr_texts:
            hits = find_instructions(carrier.text)
            if carrier.carrier.startswith("meta["):  # meta 描述常常照抄正文:正文里也有的同一条不重复算
                hits = [h for h in hits if h[0] not in in_visible]
            if hits:
                page_findings.append(finding(
                    CHECK, HIDDEN_TEXT_INJECTION, "problem", page.url,
                    f"读者看不到的位置({carrier.carrier})里有写给 AI 的指令。",
                    location=carrier.carrier, snippet=hits[0][1], rule="、".join(r for r, _ in hits)))
        for block in page.jsonld_blocks:
            hits = [h for h in find_instructions(block.raw) if h[0] not in in_visible]  # 照抄正文的同一句不算
            if hits:
                page_findings.append(finding(
                    CHECK, HIDDEN_TEXT_INJECTION, "problem", page.url,
                    "结构化数据(JSON-LD)的文字里有写给 AI 的指令,而正文里没有这句话。",
                    location=f"jsonld[{block.index}]", snippet=hits[0][1], rule="、".join(r for r, _ in hits)))
        if visible_hits:
            page_findings.append(finding(
                CHECK, HIDDEN_TEXT_INJECTION, "notice", page.url,
                "可见正文里有写给 AI 的话。如果这页是在讨论提示注入这个话题,属正常;否则建议删掉。",
                location="正文", snippet=visible_hits[0][1], rule="、".join(r for r, _ in visible_hits)))

        flagged_locations = {f.evidence.get("location") for f in page_findings}
        for hidden in page.hidden_texts:
            if (hidden.technique == "suspicious" and len(hidden.text) >= _SUSPICIOUS_MIN_CHARS
                    and hidden.location not in flagged_locations):
                page_findings.append(finding(
                    CHECK, HIDDEN_TEXT_INJECTION, "notice", page.url,
                    f"有 {len(hidden.text)} 个字用「{hidden.reason}」藏了起来,读者看不到。没有发现其中有指令;请确认这不是给搜索引擎或 AI 单独准备的文字。",
                    location=hidden.location, snippet=snippet(hidden.text, 200)))

        ui_state = [h for h in page.hidden_texts if h.technique == "ui_state"]
        if not page_findings:
            extra = (f"另有 {len(ui_state)} 处 display:none / hidden 之类的界面文字(菜单、弹窗常见),其中没有指令,不算问题。"
                     if ui_state else "也没有发现用样式藏起来的文字。")
            page_findings.append(finding(
                CHECK, HIDDEN_TEXT_INJECTION, "pass", page.url,
                f"未见写给 AI 的指令(看了正文、隐藏元素、注释、alt、meta、JSON-LD)。{extra}"))
        findings.extend(page_findings)
    return findings
