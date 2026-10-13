"""互联网档案馆(Wayback Machine)的公开存档(v0.4.26 S6.4,用户定 2026-10-13):网页现在打不开时,退一步看它的历史快照。

这是公开存档,不是绕过;存档是「某年某月那一刻的页面」,不是今天的,调用方必须注明存档日期、不算实时观测、单独计数。
用公开的可用性接口找最近一张快照,再取快照原文(id_ 标记:不带档案馆自己的工具栏)。
只做取回,不做判定;零项目内依赖。
档案馆限流(429)翻译成 ArchiveRateLimited 带 Retry-After,等不等、等多久归调用方(有界等待)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import quote

import httpx
import structlog

from crawler.fetch import MAX_PAGE_BYTES, fetch_page, get_as_browser

log = structlog.get_logger("crawler.wayback")

AVAILABILITY_URL: Final = "https://archive.org/wayback/available?url={url}"
SNAPSHOT_URL: Final = "https://web.archive.org/web/{timestamp}id_/{url}"


@dataclass(frozen=True)
class ArchivedPage:
    html: str
    archive_url: str  # 快照地址(证据里原样给)
    timestamp: str  # 档案馆的时间戳 YYYYMMDDhhmmss
    http_status: int | None  # 快照里记录的原网页状态
    truncated: bool = False


class ArchiveRateLimited(Exception):
    """档案馆说「太快了」(429);retry_after_s 是它给的等待秒数,没给就是 None。"""

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__("archive rate limited")
        self.retry_after_s = retry_after_s


class ArchiveSource(Protocol):
    def fetch(self, url: str) -> ArchivedPage | None: ...


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("Retry-After", "").strip()
    return float(value) if value.isdigit() else None  # 只认秒数;日期写法当没给


class WaybackClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client  # 与网页核实共用一个客户端(超时、身份一致);不负责关闭

    def fetch(self, url: str) -> ArchivedPage | None:
        """最近一张可用快照的原文;没有快照 / 接口不通 / 快照取不回 → None(调用方记「存档也没有」);
        限流 → ArchiveRateLimited。"""
        try:
            lookup = get_as_browser(self._client, AVAILABILITY_URL.format(url=quote(url, safe="")))
        except httpx.HTTPError as exc:
            log.warning("wayback_lookup_failed", url=url, error=type(exc).__name__)
            return None
        if lookup.status_code == 429:
            raise ArchiveRateLimited(_retry_after(lookup.headers))
        if lookup.status_code != 200:
            return None
        try:
            data = lookup.json()
        except ValueError:
            return None
        closest = (data.get("archived_snapshots") or {}).get("closest") if isinstance(data, dict) else None
        if not isinstance(closest, dict) or not closest.get("available") or not closest.get("timestamp"):
            return None
        timestamp = str(closest["timestamp"])
        snapshot_url = SNAPSHOT_URL.format(timestamp=timestamp, url=url)
        try:
            response, html, truncated = fetch_page(self._client, snapshot_url, max_bytes=MAX_PAGE_BYTES)
        except httpx.HTTPError as exc:
            log.warning("wayback_snapshot_failed", url=url, error=type(exc).__name__)
            return None
        if response.status_code == 429:
            raise ArchiveRateLimited(_retry_after(response.headers))
        if not response.is_success:
            return None
        status_text = str(closest.get("status") or "")
        return ArchivedPage(html=html, archive_url=str(closest.get("url") or snapshot_url), timestamp=timestamp,
                            http_status=int(status_text) if status_text.isdigit() else None, truncated=truncated)
