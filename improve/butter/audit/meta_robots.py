"""检查一:meta robots 与 X-Robots-Tag 里的 noindex / nosnippet / noarchive / max-snippet。

依据(research/04 G4,官方文档口径):
- Google:nosnippet 的内容不会作为 AI Overviews / AI Mode 的直接输入;进入这些功能的前提是可被索引并能出摘要。
- Bing:2023 年公开表态(二手报道)称带 NOARCHIVE 的内容不进 Bing Chat 回答;NOCACHE 只给地址 / 标题 / 摘要。
"""

from __future__ import annotations

import re

from butter.audit.base import finding
from butter.contracts import AuditFinding, PageExtract
from butter.registry.ids import ACCESS_META_SNIPPET

CHECK = "meta_robots"
_META_NAMES = frozenset({"robots", "googlebot", "bingbot"})
_VALUE_DIRECTIVES = ("max-snippet", "max-image-preview", "max-video-preview", "unavailable_after")
_MAX_SNIPPET = re.compile(r"^max-snippet\s*:\s*(-?\d+)$")
_NOSNIPPET_NOTICE_CHARS = 200

_PROBLEM_TEXT = {
    "noindex": "noindex:本页不让搜索引擎收录。Google 官方文档写明,进入 AI Overviews / AI Mode 的前提是页面可被索引并能出摘要。",
    "none": "none(等于 noindex + nofollow):本页不让搜索引擎收录。Google 官方文档写明,进入 AI Overviews / AI Mode 的前提是页面可被索引并能出摘要。",
    "nosnippet": "nosnippet:Google 官方文档写明,带 nosnippet 的内容不会作为 AI Overviews / AI Mode 的直接输入。",
    "noarchive": "noarchive:Bing 2023 年的公开表态(二手报道)称带 NOARCHIVE 的内容不进 Bing Chat 的回答。",
}


def _directives(content: str) -> list[str]:
    return [part.strip().lower() for part in content.split(",") if part.strip()]


def _from_header(header: str) -> list[tuple[str, str]]:
    """X-Robots-Tag 可以写成「googlebot: noindex」;返回 (作用对象, 指令)。"""
    out = []
    for part in _directives(header):
        scope = "所有爬虫"
        if ":" in part and not part.startswith(_VALUE_DIRECTIVES):
            scope, part = (s.strip() for s in part.split(":", 1))
        if part:
            out.append((scope, part))
    return out


def check(extracts: list[PageExtract]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for page in extracts:
        seen: list[tuple[str, str, str]] = []  # (位置, 指令, 原文)
        for meta in page.metas:
            if meta.attr == "name" and meta.key in _META_NAMES:
                seen.extend((f"meta[name={meta.key}]", d, meta.content) for d in _directives(meta.content))
        for scope, directive in _from_header(page.x_robots_tag):
            seen.append((f"HTTP 头 X-Robots-Tag({scope})", directive, page.x_robots_tag))

        page_findings: list[AuditFinding] = []
        for location, directive, raw in seen:
            if directive in _PROBLEM_TEXT:
                page_findings.append(finding(CHECK, ACCESS_META_SNIPPET, "problem", page.url,
                                             f"本页带 {_PROBLEM_TEXT[directive]}", location=location, observed=raw))
            elif directive == "nocache":
                page_findings.append(finding(
                    CHECK, ACCESS_META_SNIPPET, "notice", page.url,
                    "本页带 nocache:按 Bing 的说法,这类页面只给出地址、标题和摘要。", location=location, observed=raw))
            elif m := _MAX_SNIPPET.match(directive):
                limit = int(m.group(1))
                if limit == 0:
                    page_findings.append(finding(
                        CHECK, ACCESS_META_SNIPPET, "problem", page.url,
                        "本页带 max-snippet:0,摘要长度上限为 0,等于不让出摘要。Google 官方文档写明摘要控制同样作用于 AI Overviews / AI Mode。",
                        location=location, observed=raw))
                elif limit > 0:
                    page_findings.append(finding(
                        CHECK, ACCESS_META_SNIPPET, "notice", page.url,
                        f"本页带 max-snippet:{limit},摘要最多 {limit} 个字符。请确认这是有意设置的上限。",
                        location=location, observed=raw))
        if page.data_nosnippet_chars >= _NOSNIPPET_NOTICE_CHARS:
            page_findings.append(finding(
                CHECK, ACCESS_META_SNIPPET, "notice", page.url,
                f"本页有 {page.data_nosnippet_chars} 个字的内容标了 data-nosnippet,这部分不会出现在摘要里。请确认没有误伤想被引用的内容。",
                location="data-nosnippet 属性", observed=f"{page.data_nosnippet_chars} 字"))
        if not page_findings:
            page_findings.append(finding(
                CHECK, ACCESS_META_SNIPPET, "pass", page.url,
                "未见 noindex / nosnippet / noarchive / max-snippet 限制(看了 meta robots 与 X-Robots-Tag 头)。",
                observed="; ".join(sorted({f"{loc}: {raw}" for loc, _, raw in seen})) or "没有 robots 类 meta 与头"))
        findings.extend(page_findings)
    return findings
