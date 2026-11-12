"""抓取:全仓库唯一联网的模块。只抓用户给的那个站。

规矩(写死,不提供开关):
- 固定版本的 Chrome 浏览器 UA,不冒充任何爬虫;不带 cookie、不登录。
- 只抓同一主机(www. 有无算同一个);跳去别的主机就停下记一笔,不跟。
- 自动发现的页面遵守该站 robots.txt 对 * 的 Disallow。
- 遇到人机验证页(验证码、质询)整次抓取立刻停,不重试、不换法子。
- 每次请求之间停顿;页数、单页体积、总时长都有上限。失败不重试。
- 抓到的内容一律当不可信数据原样交出去,这里不解释、不执行。
"""

from __future__ import annotations

import gzip
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from butter.contracts import FetchedPage, PageRole, RobotsFetch, SitemapEntry, SiteSnapshot, StopReason
from butter.extract.challenge import REFUSAL_STATUSES as _REFUSAL_STATUSES
from butter.extract.challenge import looks_like_challenge  # noqa: F401  (测试和命令行从这里取)
from butter.extract.html import extract_page  # 只用它取页面上的链接;纯函数
from butter.extract.robots import RobotsParsed, parse_robots, rules_allow
from butter.extract.sitemap import parse_sitemap
from butter.util import decode_html as _decode
from butter.util import host_of, normalize_site_url, page_key, safe_http_url, same_site

# 与 Toast crawler/fetch.py 的 BROWSER_UA 同一串(产品文档 D13:固定版本的 Chrome 浏览器 UA)
BROWSER_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_KEPT_HEADERS: Final = ("content-type", "x-robots-tag", "last-modified", "location", "cf-mitigated")
_NON_HTML_EXT: Final = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|webp|svg|ico|css|js|json|xml|rss|atom|zip|gz|rar|7z|mp[34]|mov|avi|webm|woff2?|ttf|"
    r"eot|docx?|xlsx?|pptx?|exe|dmg|apk|txt)$", re.IGNORECASE)


@dataclass(frozen=True)
class FetchLimits:
    max_pages: int = 10  # 含用户给的那页和首页
    delay_s: float = 1.0
    max_bytes: int = 2_000_000
    timeout_s: float = 15.0
    total_budget_s: float = 90.0
    max_redirects: int = 5
    max_child_sitemaps: int = 3
    max_sitemap_bytes: int = 5_000_000
    max_robots_bytes: int = 512_000


@dataclass
class _Response:
    url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    raw: bytes
    truncated: bool
    error: str | None
    off_host: str | None = None  # 跳去了别的主机


class _Session:
    def __init__(self, client: httpx.Client, limits: FetchLimits, host: str,
                 sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
        self.client, self.limits, self.host = client, limits, host
        self._sleep, self._clock = sleep, clock
        self._started = clock()
        self._requests = 0

    def out_of_time(self) -> bool:
        return self._clock() - self._started >= self.limits.total_budget_s

    def get(self, url: str, max_bytes: int) -> _Response:
        """手动跟跳转:每一跳都要是同一主机。"""
        current = url
        for _ in range(self.limits.max_redirects + 1):
            if self._requests:
                self._sleep(self.limits.delay_s)
            self._requests += 1
            try:
                with self.client.stream("GET", current, headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }) as response:
                    headers = {}
                    for key in _KEPT_HEADERS:
                        values = response.headers.get_list(key)
                        if values:
                            headers[key] = ", ".join(values)
                    if response.status_code in (301, 302, 303, 307, 308) and "location" in headers:
                        target = safe_http_url(urljoin(current, response.headers["location"]))
                        if target is None:
                            return _Response(url, current, response.status_code, headers, b"", False, "跳转地址无效")
                        if not same_site(host_of(target), self.host):
                            return _Response(url, current, response.status_code, headers, b"", False, None,
                                             off_host=host_of(target))
                        current = target
                        continue
                    chunks, size, truncated = [], 0, False
                    for chunk in response.iter_bytes():
                        if size + len(chunk) > max_bytes:
                            chunks.append(chunk[: max_bytes - size])
                            truncated = True
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    return _Response(url, current, response.status_code, headers, b"".join(chunks), truncated, None)
            except httpx.HTTPError as exc:
                return _Response(url, current, None, {}, b"", False, f"{type(exc).__name__}: {exc}"[:300])
        return _Response(url, current, None, {}, b"", False, f"跳转超过 {self.limits.max_redirects} 次")


def _to_page(resp: _Response, role: PageRole, now: datetime) -> FetchedPage:
    content_type = resp.headers.get("content-type", "")
    is_html = resp.status is not None and (
        "html" in content_type.lower() or (not content_type and resp.raw.lstrip()[:1] == b"<"))
    html = _decode(resp.raw, content_type) if is_html else ""
    error = resp.error
    if error is None and resp.off_host:
        error = f"跳去了别的主机 {resp.off_host},没有跟"
    return FetchedPage(url=resp.url, final_url=resp.final_url, role=role, status=resp.status, content_type=content_type,
                       headers=resp.headers, html=html, truncated=resp.truncated, error=error, fetched_at=now)


def _gunzip_capped(raw: bytes, cap: int) -> bytes:
    if raw[:2] != b"\x1f\x8b":
        return raw
    try:
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, cap)
    except (zlib.error, gzip.BadGzipFile):
        return b""


def fetch_site(
    url: str,
    *,
    limits: FetchLimits = FetchLimits(),
    client: httpx.Client | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SiteSnapshot:
    site_url = normalize_site_url(url)
    parts = urlsplit(site_url)
    host = parts.hostname or ""
    origin = f"{parts.scheme}://{parts.netloc}"
    own_client = client is None
    client = client or httpx.Client(timeout=limits.timeout_s, follow_redirects=False)
    session = _Session(client, limits, host, sleep, clock)

    pages: list[FetchedPage] = []
    notes: list[str] = []
    stop: StopReason = "completed"
    robots = RobotsFetch(url=f"{origin}/robots.txt", state="unreadable", note="没有抓到这一步")
    robots_parsed = RobotsParsed()
    sitemap_urls: list[str] = []
    sitemap_entries: list[SitemapEntry] = []
    seen: set[str] = set()
    started_at = now()

    def halted(resp: _Response, body: str, what: str) -> bool:
        """这次响应是否要求整次抓取停下。"""
        nonlocal stop
        if marker := looks_like_challenge(resp.status, resp.headers, body):
            stop = "challenge"
            notes.append(f"{what} 返回了人机验证页(标记:{marker}),整次抓取到此为止,没有重试。")
            return True
        if resp.status == 429:
            stop = "refused"
            notes.append(f"{what} 返回 429(请求过多),整次抓取到此为止。")
            return True
        return False

    def fetch_page(page_url: str, role: PageRole) -> FetchedPage | None:
        """抓一页并记下来。返回 None 表示应当停下。"""
        seen.add(page_key(page_url))
        resp = session.get(page_url, limits.max_bytes)
        page = _to_page(resp, role, now())
        pages.append(page)
        seen.add(page_key(page.final_url))
        if halted(resp, page.html or resp.raw[:20_000].decode("utf-8", "replace"), page_url):
            return None
        if resp.off_host:
            notes.append(f"{page_url} 跳去了别的主机 {resp.off_host},没有跟。")
        elif resp.error:
            notes.append(f"{page_url} 没抓到:{resp.error}")
        elif resp.status is not None and resp.status >= 400:
            notes.append(f"{page_url} 返回 {resp.status}。")
        return page

    try:
        # 1. 用户给的那一页
        given = fetch_page(site_url, "given")
        if given is None:
            pass
        elif given.status in (301, 302, 303, 307, 308):  # 跳转没跟完 = 目的地在别的主机
            stop = "off_host"
            target = safe_http_url(given.headers.get("location", ""), site_url)
            notes.append("Butter 只抓你给的那个主机,所以到此为止。"
                         + (f"如果 {target} 也是你的站,请直接用这个网址重跑。" if target else ""))
        elif given.status in (401, 403, 503):
            stop = "refused"
            notes.append(f"用户给的网址被拒({given.status}),没有继续抓这个站的其他页面。")
        elif given.status is None:
            stop = "error"

        # 2. robots.txt
        if stop == "completed":
            resp = session.get(robots.url, limits.max_robots_bytes)
            text = _decode(resp.raw, resp.headers.get("content-type", "")) if resp.raw else ""
            if halted(resp, text, "robots.txt"):
                robots = RobotsFetch(url=robots.url, state="unreadable", status=resp.status, note="被人机验证页挡住")
            elif resp.off_host:
                robots = RobotsFetch(url=robots.url, state="unreadable", status=resp.status,
                                     note=f"跳去了别的主机 {resp.off_host}")
            elif resp.status == 200:
                if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                    robots = RobotsFetch(url=robots.url, state="missing", status=200,
                                         note="robots.txt 的地址返回的是一个网页,不是规则文件;按没有 robots.txt 处理")
                else:
                    robots = RobotsFetch(url=robots.url, state="ok", status=200, text=text)
                    robots_parsed = parse_robots(text)
            elif resp.status in (404, 410):
                robots = RobotsFetch(url=robots.url, state="missing", status=resp.status)
            else:
                robots = RobotsFetch(url=robots.url, state="unreadable", status=resp.status,
                                     note=resp.error or f"HTTP {resp.status}")

        # 3. 首页
        home_url = f"{origin}/"
        if stop == "completed" and page_key(home_url) not in seen:
            if fetch_page(home_url, "home") is None:
                pass

        # 4. sitemap
        if stop == "completed":
            declared = [u for u in (safe_http_url(s) for s in robots_parsed.sitemaps) if u]
            queue = [u for u in declared if same_site(host_of(u), host)]
            for skipped in (u for u in declared if not same_site(host_of(u), host)):
                notes.append(f"robots.txt 声明的 sitemap {skipped} 在别的主机上,没有读。")
            if not queue:
                queue = [f"{origin}/sitemap.xml"]
            read = 0
            while queue and read < 1 + limits.max_child_sitemaps and stop == "completed":
                if session.out_of_time():
                    stop = "time_budget"
                    break
                sm_url = queue.pop(0)
                resp = session.get(sm_url, limits.max_sitemap_bytes)
                read += 1
                raw = _gunzip_capped(resp.raw, limits.max_sitemap_bytes)
                text = _decode(raw, "") if raw else ""
                if halted(resp, text, sm_url):
                    break
                if resp.status != 200 or not text:
                    if resp.status not in (404, 410):
                        notes.append(f"sitemap {sm_url} 没读到({resp.error or resp.status})。")
                    continue
                parsed = parse_sitemap(text)
                if parsed.error:
                    notes.append(f"sitemap {sm_url}:{parsed.error}。")
                    continue
                sitemap_urls.append(sm_url)
                kept_before = len(sitemap_entries)
                for loc, lastmod in parsed.entries:
                    safe = safe_http_url(loc)
                    if safe and same_site(host_of(safe), host):
                        sitemap_entries.append(SitemapEntry(loc=safe, lastmod=lastmod, source_url=sm_url))
                dropped = len(parsed.entries) - (len(sitemap_entries) - kept_before)
                if dropped:
                    notes.append(f"sitemap {sm_url} 里有 {len(parsed.entries)} 个地址,其中 {dropped} 个在别的主机上"
                                 "(或不是网址),没有采用。")
                elif not parsed.entries and not parsed.children:
                    notes.append(f"sitemap {sm_url} 读到了,但里面没有地址。")
                if parsed.truncated:
                    notes.append(f"sitemap {sm_url} 条目太多,只读了前 {len(parsed.entries)} 条。")
                queue.extend(u for u in (safe_http_url(c) for c in parsed.children)
                             if u and same_site(host_of(u), host))
            if queue and stop == "completed":
                notes.append(f"还有 {len(queue)} 个子 sitemap 没读(上限 {limits.max_child_sitemaps} 个)。")

        # 5. 自动发现的页面:先首页和给定页上的站内链接,再 sitemap 顺序
        if stop == "completed":
            candidates: list[str] = []
            ordered = sorted((p for p in pages if p.html), key=lambda p: 0 if p.role == "home" else 1)
            for page in ordered:
                candidates.extend(link.href for link in extract_page(page).links)
            candidates.extend(entry.loc for entry in sitemap_entries)
            star_rules = robots_parsed.star_rules()
            blocked_by_robots = 0
            refusals = 0
            for candidate in candidates:
                if len(pages) >= limits.max_pages:
                    break
                parts_c = urlsplit(candidate)
                key = page_key(candidate)
                if (key in seen or not same_site(parts_c.hostname or "", host) or parts_c.query
                        or _NON_HTML_EXT.search(parts_c.path)):
                    continue
                seen.add(key)
                if not rules_allow(star_rules, parts_c.path or "/"):
                    blocked_by_robots += 1
                    continue
                if session.out_of_time():
                    stop = "time_budget"
                    notes.append(f"到了总时长上限({limits.total_budget_s:.0f} 秒),停止抓取。")
                    break
                page = fetch_page(candidate, "discovered")
                if page is None:
                    break
                if page.status in _REFUSAL_STATUSES:
                    refusals += 1
                    if refusals >= 2:
                        stop = "refused"
                        notes.append("连续两个页面被拒,停止抓取。")
                        break
                else:
                    refusals = 0
            if blocked_by_robots:
                notes.append(f"有 {blocked_by_robots} 个站内地址被该站 robots.txt 对 * 的规则挡住,没有抓。")
            if stop == "completed" and not any(p.role == "discovered" for p in pages):
                notes.append("没有找到可以接着抓的站内页面(已抓页面的原始 HTML 里没有可用的站内链接,"
                             "sitemap 里也没有本站的地址),所以只看了这 " f"{len(pages)} 页。")
    finally:
        if own_client:
            client.close()

    return SiteSnapshot(
        site_url=site_url, host=host, origin=origin, fetched_at=started_at, user_agent=BROWSER_UA, pages=pages,
        robots=robots, sitemap_urls=sitemap_urls, sitemap_entries=sitemap_entries, stop_reason=stop, notes=notes,
    )
