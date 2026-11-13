"""sitemap 解析:只取 loc 与 lastmod。外来 XML 是不可信数据:带 DTD / 实体声明的一律拒收。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

MAX_ENTRIES = 5000
_MIN_FOR_VERDICT = 5  # 少于这么多条带 lastmod 的地址,不下判断
_NEAR_NOW = timedelta(minutes=30)
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass
class SitemapParsed:
    entries: list[tuple[str, str | None]] = field(default_factory=list)  # (loc, lastmod)
    children: list[str] = field(default_factory=list)  # sitemapindex 里的子 sitemap
    error: str | None = None
    truncated: bool = False


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_lastmod(raw: str) -> datetime | None:
    """sitemap 的 lastmod:YYYY-MM-DD 或 W3C 日期时间。只有带时间的才有时刻可比;只有日期的按当天 0 点、无时区。"""
    text = raw.strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def lastmod_suspicion(entries, reference: datetime | None) -> str | None:
    """sitemap 里的 lastmod 整体可不可信。返回不可信的原因;可信或条数太少返回 None。

    两条规则,都是真实运行里见过的:
    - 带时刻的 lastmod 多数与抓取时刻相差不到 30 分钟:sitemap 是动态生成的,打的是「现在」,不是各页的修改时间;
    - 所有 lastmod 都是同一天:多半是生成 sitemap 时统一打的日期。
    这里只判断,不改数据:调用方决定怎么用(事实库不采用,审计提示)。
    """
    stamps = [(e.loc, _parse_lastmod(e.lastmod)) for e in entries if e.lastmod]
    parsed = [(loc, dt) for loc, dt in stamps if dt is not None]
    if len(parsed) < _MIN_FOR_VERDICT:
        return None
    timed = [dt for _, dt in parsed if dt.tzinfo is not None]
    if reference is not None and timed:
        ref = reference if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
        near = sum(1 for dt in timed if abs(dt - ref) <= _NEAR_NOW)
        if near >= _MIN_FOR_VERDICT and near * 2 >= len(timed):
            return (f"{near}/{len(timed)} 条带时刻的 lastmod 与抓取时刻相差不到 30 分钟,"
                    "像是生成 sitemap 时打的当前时间,不是各页真实的修改时间")
    days = {dt.date() for _, dt in parsed}
    if len(days) == 1:
        return f"{len(parsed)} 条 lastmod 全是同一天({next(iter(days)).isoformat()}),可能是生成 sitemap 时统一打的"
    return None


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
