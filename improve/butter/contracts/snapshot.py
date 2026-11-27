"""站点快照(fetch 的产物;抓到的内容一律是不可信数据)。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from butter.contracts.common import Frozen, PageRole

StopReason = Literal["completed", "challenge", "refused", "time_budget", "off_host", "error"]
RobotsState = Literal["ok", "missing", "unreadable"]
SnapshotSource = Literal["network", "local_files"]  # local_files = 站长自己存下的 HTML 文件,没有联网


class FetchedPage(Frozen):
    url: str  # 请求的地址
    final_url: str  # 同主机跳转后的地址
    role: PageRole
    status: int | None = None  # None = 没连上
    content_type: str = ""
    headers: dict[str, str] = {}  # 只留审计要用的几个头,键小写
    html: str = ""
    truncated: bool = False
    error: str | None = None
    fetched_at: datetime  # 联网:收到响应的时刻;本地导入:文件的修改时间
    local_file: str = ""  # 本地导入时的文件名;空 = 联网抓的。导入的页面没有 HTTP 状态和响应头
    rendered_dom: bool = False  # 文件像是浏览器「网页,全部」方式存的:脚本运行后的页面,不是服务器返回的原始 HTML


class RobotsFetch(Frozen):
    url: str
    state: RobotsState
    status: int | None = None
    text: str = ""
    note: str | None = None


class SitemapEntry(Frozen):
    loc: str
    lastmod: str | None = None
    source_url: str  # 读自哪个 sitemap 文件


class SiteSnapshot(Frozen):
    schema_version: Literal["snapshot/1"] = "snapshot/1"
    site_url: str  # 用户给的网址(补全协议后)
    host: str
    origin: str  # scheme://host
    fetched_at: datetime
    user_agent: str  # 本地导入时为空:没有发出任何请求
    source: SnapshotSource = "network"
    pages: list[FetchedPage] = []
    robots: RobotsFetch
    sitemap_urls: list[str] = []  # 实际读到的 sitemap 文件
    sitemap_entries: list[SitemapEntry] = []
    stop_reason: StopReason = "completed"
    notes: list[str] = []  # 跳过了什么、停在哪、为什么
