"""sitemap 解析:只取 loc 与 lastmod。外来 XML 是不可信数据:带 DTD / 实体声明的一律拒收。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

MAX_ENTRIES = 5000
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass
class SitemapParsed:
    entries: list[tuple[str, str | None]] = field(default_factory=list)  # (loc, lastmod)
    children: list[str] = field(default_factory=list)  # sitemapindex 里的子 sitemap
    error: str | None = None
    truncated: bool = False


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(text: str) -> SitemapParsed:
    parsed = SitemapParsed()
    if _DTD.search(text):
        parsed.error = "含 DTD 或实体声明,拒收"
        return parsed
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        parsed.error = f"XML 解析失败:{exc}"
        return parsed
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        parsed.error = f"根元素是 <{kind}>,不是 sitemap"
        return parsed
    for node in root:
        loc, lastmod = None, None
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip() or None
        if not loc:
            continue
        if kind == "sitemapindex":
            parsed.children.append(loc)
        else:
            if len(parsed.entries) >= MAX_ENTRIES:
                parsed.truncated = True
                break
            parsed.entries.append((loc, lastmod))
    return parsed
