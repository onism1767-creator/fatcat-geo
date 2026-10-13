"""L2 网页核实:打开 AI 引用 / 搜到的网页,抽正文、原样存档(v0.4.26 S6.1;产品文档 15.2 D14 / D23)。

这里只做两件事:按边界礼貌地抓,把抓到的东西原样写进只追加表 page_check。
「正文里有没有写到你」是判定,在 engine/detectors/page_mention.py——存的是文字,判定可随版本重算。

边界(用户定,2026-10-13):按规范化 URL 一单只开一次;总并发 CONCURRENCY、同一网站同时只开 1 个、
同站间隔 HOST_GAP_S;
单页 MAX_PAGE_BYTES、连接 5 秒 / 总 10 秒;一单 BUDGET_S,到时没开到的记「没来得及开」;
优先级由调用方给(被引 > 搜到 > 出现次数);
只开链接本身、不往站内爬;第三方站尊重 robots.txt Disallow(记 ROBOTS_DISALLOWED;用户自己的站按 D13 照样开);
7 天内同一网址复用。看不到按原因分开记,不猜。网页内容一律当数据,只做机械解析,不回喂模型。
S6.2(用户定):原始 HTML 抽不出正文的页,再用无头浏览器(crawler/render.py)渲染一次、串行、同一预算内;
渲染也失败或仍无正文,照旧记「抽不出正文」并留下渲染尝试的记录。
S6.3(用户定,不加必填 key):① 配了官方接口的平台(如 Reddit)先走接口拿标题 + 正文 + 评论;
② 抽不出正文的页退一步读网页自带的标题 + 简介(meta);③ 网页打不开或仍无文字、而平台有公开嵌入接口(不要 key)的,
拿标题(oembed)。文字来源都记在 text_source。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol
from urllib import robotparser
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import structlog

from crawler.fetch import (
    FETCH_TIMEOUT,
    MAX_PAGE_BYTES,
    PROTECTION_SUSPECT_STATUSES,
    fetch_json,
    fetch_page,
    is_html,
    new_client,
    page_title,
    robots_parser,
    text_from_html,
)
from crawler.platform import PlatformFetcher
from crawler.render import PlaywrightRenderer, RenderedPage, Renderer, playwright_available
from engine.models import PageCheck, PageFetchStatus

log = structlog.get_logger("engine.page_check")

FETCHER_VERSION: Final = 1
CONCURRENCY: Final = 8
HOST_GAP_S: Final = 1.0
BUDGET_S: Final = 15 * 60
REUSE_DAYS: Final = 7
MAX_TEXT_CHARS: Final = 100_000
_LOGIN_PATH_HINTS: Final = ("/login", "/signin", "/sign-in", "/sign_in", "/accounts/login", "/auth/")
nl_marker: Final = chr(10)  # 摘要文字里「标题 + 空行 + 简介」的分隔
_POLL_S: Final = 0.05  # 没有可开的网址时的轮询间隔(所有网站都在等间隔)


def normalize_url(url: str) -> str:
    """去 fragment、主机小写;保留 www 与查询串(规范化后必须仍可抓取)。"""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


class PageCheckStore(Protocol):
    def insert_page_check(self, check: PageCheck) -> None: ...
    def recent_page_checks(
        self, normalized_urls: list[str], *, max_age_days: int = REUSE_DAYS,
    ) -> dict[str, PageCheck]: ...


@dataclass(frozen=True)
class PageRequest:
    url: str
    priority: int = 0  # 小的先开(被引 0 / 搜到 1),同级按出现次数由调用方排
    respect_robots: bool = True  # 用户自己的站 False(D13:robots 只作检查对象)


@dataclass(frozen=True)
class PageIndex:
    """一单的网页核实结果:规范化 URL → 记录;没来得及开的不在 checks 里、也不在 attempted 里。"""

    checks: Mapping[str, PageCheck] = field(default_factory=dict)
    attempted: frozenset[str] = frozenset()

    def lookup(self, url: str) -> PageCheck | None:
        return self.checks.get(normalize_url(url))

    def not_attempted(self, url: str) -> bool:
        return normalize_url(url) not in self.attempted


class PageChecker:
    def __init__(
        self,
        store: PageCheckStore,
        *,
        client: httpx.Client | None = None,
        budget_s: float = BUDGET_S,
        concurrency: int = CONCURRENCY,
        host_gap_s: float = HOST_GAP_S,
        reuse_days: int = REUSE_DAYS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        render: bool = True,
        renderer: Renderer | None = None,
        platform_fetchers: Sequence[PlatformFetcher] | None = None,
        oembed: Mapping[str, str] | None = None,
    ) -> None:
        """render=False 关掉渲染回退(测试、对照脚本);renderer 不给就在 Playwright 可用时用它,不可用就不渲染。
        platform_fetchers:配了 key 的平台官方接口(S6.3);oembed:主机 → 公开嵌入接口模板({url} 占位)。"""
        self._fetchers = list(platform_fetchers or [])
        self._oembed = dict(oembed or {})
        self._store = store
        self._own_client = client is None
        self._client = client or new_client(timeout=FETCH_TIMEOUT)
        if renderer is None and render and playwright_available():
            renderer = PlaywrightRenderer()
        self._renderer: Renderer | None = renderer if render else None
        self._budget_s, self._concurrency, self._host_gap_s = budget_s, concurrency, host_gap_s
        self._reuse_days = reuse_days
        self._clock, self._sleep = clock, sleep
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()

    def close(self) -> None:
        if self._own_client:
            self._client.close()
        if self._renderer is not None:
            self._renderer.close()
        for fetcher in self._fetchers:
            fetcher.close()

    # ── 一单一次 ──

    def check(self, requests: Iterable[PageRequest]) -> PageIndex:
        wanted: dict[str, PageRequest] = {}
        for request in sorted(requests, key=lambda r: r.priority):
            wanted.setdefault(normalize_url(request.url), request)  # 同一网址只开一次,优先级取最高(最小)
        checks: dict[str, PageCheck] = dict(self._store.recent_page_checks(list(wanted), max_age_days=self._reuse_days))
        pending = {normalized: request for normalized, request in wanted.items() if normalized not in checks}
        deadline = self._clock() + self._budget_s
        fetched = self._run(pending, deadline)
        for normalized, check in fetched.items():
            self._store.insert_page_check(check)  # 落库在主线程:psycopg 连接非线程安全
            checks[normalized] = check
        rendered = self._render_pass(wanted, checks, deadline)
        for normalized, check in rendered.items():
            self._store.insert_page_check(check)
            checks[normalized] = check
        log.info("pages_checked", wanted=len(wanted), reused=len(wanted) - len(pending),
                 fetched=len(fetched), rendered=len(rendered), not_attempted=len(pending) - len(fetched))
        return PageIndex(checks=checks, attempted=frozenset(checks))

    def _run(self, pending: dict[str, PageRequest], deadline: float) -> dict[str, PageCheck]:
        if not pending:
            return {}
        schedule = _HostSchedule(pending, clock=self._clock, host_gap_s=self._host_gap_s, deadline=deadline)
        results: dict[str, PageCheck] = {}
        lock = threading.Lock()

        def worker() -> None:
            while True:
                picked = schedule.next()
                if isinstance(picked, _Signal):
                    if picked is _Signal.STOP:
                        return
                    self._sleep(_POLL_S)
                    continue
                normalized, request = picked
                try:
                    check = self._fetch_one(normalized, request)
                except Exception as exc:  # 单页任何意外都不能让整单的核实静默蒸发
                    log.error("page_check_crashed", url=normalized, error=f"{type(exc).__name__}: {exc}")
                    check = self._record(request.url, normalized, PageFetchStatus.FETCH_FAILED,
                                         error=type(exc).__name__)
                finally:
                    schedule.release(normalized)
                with lock:
                    results[normalized] = check

        workers = min(self._concurrency, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
        for future in futures:
            future.result()  # 调度本身的编程错误直接上抛,不吞
        return results

    # ── 渲染回退(S6.2) ──

    def _render_pass(
        self, wanted: dict[str, PageRequest], checks: dict[str, PageCheck], deadline: float,
    ) -> dict[str, PageCheck]:
        """原始 HTML 抽不出正文的页,用无头浏览器渲染后再抽一次:串行、按优先级、同站仍隔间隔、到截止时间就停;
        已经渲染过(记录带 render: 标记)的不再渲染;没有渲染器就不做。"""
        if self._renderer is None:
            return {}
        todo = sorted(
            ((normalized, wanted[normalized]) for normalized, check in checks.items()
             if check.status is PageFetchStatus.EMPTY_TEXT and not (check.error or "").startswith("render:")),
            key=lambda item: item[1].priority,
        )
        rendered: dict[str, PageCheck] = {}
        last_host, last_at = "", 0.0
        for normalized, request in todo:
            if self._clock() >= deadline:
                break
            host = urlsplit(normalized).netloc
            if host == last_host:
                wait = last_at + self._host_gap_s - self._clock()
                if wait > 0:
                    self._sleep(wait)
            page = self._renderer.render(normalized)
            last_host, last_at = host, self._clock()
            rendered[normalized] = self._from_render(request, normalized, page)
        return rendered

    def _from_render(self, request: PageRequest, normalized: str, page: RenderedPage | None) -> PageCheck:
        if page is None:
            return self._record(request.url, normalized, PageFetchStatus.EMPTY_TEXT, error="render:failed")
        title = page_title(page.html)
        common = dict(http_status=page.http_status, final_url=page.final_url, title=title)
        extracted = text_from_html(page.html, page.final_url)
        if extracted is None:
            return self._record(request.url, normalized, PageFetchStatus.EMPTY_TEXT, error="render:empty", **common)
        text, source = extracted
        if source == "meta":
            common["title"] = text.split(nl_marker, 1)[0]  # 摘要退路:标题取网页自带的 og/twitter 标题
        return self._record(request.url, normalized, PageFetchStatus.OK, text=text[:MAX_TEXT_CHARS],
                            text_source=f"rendered_{source}", text_chars=len(text), **common)

    # ── 单页 ──

    def _fetch_one(self, normalized: str, request: PageRequest) -> PageCheck:
        """接口 → 网页 → 嵌入接口:平台官方接口拿到就用(最完整);否则按边界抓网页;网页拿不到文字再试嵌入接口拿标题。"""
        via_api = self._via_platform_api(request.url, normalized)
        if via_api is not None:
            return via_api
        check = self._fetch_html(normalized, request)
        if check.status is PageFetchStatus.OK:
            return check
        via_oembed = self._via_oembed(request.url, normalized)
        return via_oembed if via_oembed is not None else check

    def _via_platform_api(self, url: str, normalized: str) -> PageCheck | None:
        for fetcher in self._fetchers:
            if not fetcher.matches(normalized):
                continue
            page = fetcher.fetch(normalized)
            if page is None or not page.text.strip():
                return None  # 接口没拿到:退回普通抓取
            return self._record(url, normalized, PageFetchStatus.OK, http_status=page.http_status,
                                final_url=page.final_url, title=page.title, text=page.text[:MAX_TEXT_CHARS],
                                text_source=f"api:{fetcher.name}", text_chars=len(page.text))
        return None

    def _via_oembed(self, url: str, normalized: str) -> PageCheck | None:
        host = (urlsplit(normalized).hostname or "").lower()
        template = next((t for h, t in self._oembed.items() if host == h or host.endswith("." + h)), None)
        if template is None:
            return None
        try:
            data = fetch_json(self._client, template.replace("{url}", quote(normalized, safe="")))
        except httpx.HTTPError as exc:
            log.warning("oembed_failed", url=normalized, error=type(exc).__name__)
            return None
        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str) or not title.strip():
            return None
        return self._record(url, normalized, PageFetchStatus.OK, final_url=normalized, title=title.strip(),
                            text=title.strip(), text_source="oembed", text_chars=len(title.strip()))

    def _fetch_html(self, normalized: str, request: PageRequest) -> PageCheck:
        parts = urlsplit(normalized)
        origin = f"{parts.scheme}://{parts.netloc}"
        if request.respect_robots and not self._robots_allows(origin, normalized):
            return self._record(request.url, normalized, PageFetchStatus.ROBOTS_DISALLOWED)
        try:
            response, body, truncated = fetch_page(self._client, normalized, max_bytes=MAX_PAGE_BYTES)
        except httpx.HTTPError as exc:
            log.warning("page_fetch_failed", url=normalized, error=type(exc).__name__)
            return self._record(request.url, normalized, PageFetchStatus.FETCH_FAILED, error=type(exc).__name__)
        status, final_url = response.status_code, str(response.url)
        common = dict(http_status=status, final_url=final_url, truncated=truncated)
        if status == 401 or (response.is_success and _looks_like_login(final_url)):
            return self._record(request.url, normalized, PageFetchStatus.LOGIN_REQUIRED, **common)
        if status in PROTECTION_SUSPECT_STATUSES:
            return self._record(request.url, normalized, PageFetchStatus.BLOCKED, **common)
        if not response.is_success:
            return self._record(request.url, normalized, PageFetchStatus.FETCH_FAILED, **common)
        if not is_html(response):
            return self._record(request.url, normalized, PageFetchStatus.NOT_HTML, **common)
        title = page_title(body)
        extracted = text_from_html(body, final_url)
        if extracted is None:
            return self._record(request.url, normalized, PageFetchStatus.EMPTY_TEXT, title=title, **common)
        text, source = extracted
        if source == "meta":
            title = text.split(nl_marker, 1)[0]  # 摘要退路:标题取网页自带的 og/twitter 标题,不是 <title>
        return self._record(request.url, normalized, PageFetchStatus.OK, title=title, text=text[:MAX_TEXT_CHARS],
                            text_source=source, text_chars=len(text), **common)

    def _robots_allows(self, origin: str, url: str) -> bool:
        with self._robots_lock:
            known = origin in self._robots
        if not known:
            parser = robots_parser(self._client, origin)
            with self._robots_lock:
                self._robots[origin] = parser
        with self._robots_lock:
            parser = self._robots[origin]
        return True if parser is None else parser.can_fetch("*", url)

    @staticmethod
    def _record(url: str, normalized: str, status: PageFetchStatus, **fields: object) -> PageCheck:
        return PageCheck(url=url, normalized_url=normalized, status=status, fetcher_version=FETCHER_VERSION, **fields)  # type: ignore[arg-type]


def _looks_like_login(final_url: str) -> bool:
    path = urlsplit(final_url).path.lower()
    return any(hint in path for hint in _LOGIN_PATH_HINTS)


class _Signal(Enum):
    WAIT = "wait"  # 现在没有可开的(所有网站都在等间隔或正在开)
    STOP = "stop"  # 没有了,或到截止时间了


class _HostSchedule:
    """同一网站同时只开 1 个、开完隔 host_gap_s 秒;到截止时间不再派新的。线程安全。"""

    def __init__(self, pending: dict[str, PageRequest], *, clock: Callable[[], float], host_gap_s: float,
                 deadline: float) -> None:
        self._clock, self._gap, self._deadline = clock, host_gap_s, deadline
        self._queues: dict[str, list[tuple[str, PageRequest]]] = {}
        for normalized, request in sorted(pending.items(), key=lambda item: item[1].priority):
            self._queues.setdefault(urlsplit(normalized).netloc, []).append((normalized, request))
        self._ready_at: dict[str, float] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def next(self) -> tuple[str, PageRequest] | _Signal:
        """下一个可开的网址;WAIT = 现在没有(等一下);STOP = 没有了或到时间了。"""
        with self._lock:
            now = self._clock()
            if now >= self._deadline or not self._queues:
                return _Signal.STOP
            for host, queue in sorted(self._queues.items(), key=lambda item: item[1][0][1].priority):
                if host in self._in_flight or self._ready_at.get(host, 0.0) > now:
                    continue
                item = queue.pop(0)
                if not queue:
                    del self._queues[host]
                self._in_flight.add(host)
                return item
            return _Signal.WAIT

    def release(self, normalized: str) -> None:
        host = urlsplit(normalized).netloc
        with self._lock:
            self._in_flight.discard(host)
            self._ready_at[host] = self._clock() + self._gap
