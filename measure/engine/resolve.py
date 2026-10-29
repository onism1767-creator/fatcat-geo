"""L2 引用链接还原跳转(架构书附录 v0.4.26 第 2 节 C2;S4)。

有的仪器给的引用是平台跳转链(能力声明 citation_urls_redirect):链接的主机名是跳转服务,不是真正的来源。
采样后对这些链接逐条跟随跳转——最多 MAX_HOPS 跳、只取响应头不读正文、按链接去重——结果写入只追加表
citation_resolution(原链接、终点链接、跳数、状态、还原器版本)。判定一律用终点链接;还原不出去向的
那条引用记「看不到」,不拿标题代替(铁律 7)。
请求被跳转指向的每一站之前先查那个站的 robots(用户 2026-10-29 定):不让抓就停下,网址取自上一跳的跳转头照记为去向
(STOPPED_AT_ROBOTS),不去请求它;代价是极少数还会接着跳的链接停在前一站。第一站是平台给的跳转链本身,不查。

经 crawler/ 发请求(Chrome 身份,D13)。这里不做任何品牌判定——归属判定在 detectors/。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

import httpx
import structlog

from crawler.fetch import RobotsRules, fetch_headers, new_client, robots_parser
from engine.models import CitationResolution, ResolutionStatus

log = structlog.get_logger("engine.resolve")

RESOLVER_VERSION = 2  # 2(2026-10-29):请求被跳转指向的站之前先查它的 robots
MAX_HOPS = 10
# 这两种都知道真正去向:跟到了终点,或停在 robots 不让抓的那一站(网址来自上一跳的跳转头)
_KNOWN_DESTINATION: frozenset[ResolutionStatus] = frozenset({ResolutionStatus.RESOLVED,
                                                              ResolutionStatus.STOPPED_AT_ROBOTS})


class ResolutionStore(Protocol):
    def insert_citation_resolution(self, resolution: CitationResolution) -> None: ...
    def latest_citation_resolutions(self, urls: list[str]) -> dict[str, CitationResolution]: ...


@dataclass(frozen=True)
class ResolutionContext:
    """判定器用的「这条引用到底指向哪」:非跳转仪器照原链接;跳转仪器取终点链接,还原失败 → None(看不到)。"""

    redirect_surfaces: frozenset[str] = frozenset()
    final_urls: Mapping[str, str] = field(default_factory=dict)  # 原链接 → 终点链接(只含还原成功的)

    def final_url(self, surface: str, citation_url: str) -> str | None:
        if surface not in self.redirect_surfaces:
            return citation_url
        return self.final_urls.get(citation_url)

    @classmethod
    def from_resolutions(
        cls, redirect_surfaces: frozenset[str], resolutions: Mapping[str, CitationResolution],
    ) -> ResolutionContext:
        return cls(redirect_surfaces, {
            url: r.final_url for url, r in resolutions.items()
            if r.status in _KNOWN_DESTINATION and r.final_url is not None
        })


class UrlResolver:
    def __init__(
        self, storage: ResolutionStore, *, client: httpx.Client | None = None, max_age_days: int = 30,
    ) -> None:
        self._storage = storage
        self._own_client = client is None
        self._client = client or new_client(follow_redirects=False)
        self._max_age = timedelta(days=max_age_days)
        self._robots: dict[str, RobotsRules] = {}  # 站点 → robots 规则,一单只读一次

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def resolve(
        self, urls: list[str], *, heartbeat: Callable[[], None] | None = None,
    ) -> dict[str, CitationResolution]:
        """一批链接 → {原链接: 还原记录}。按链接去重;库里时效内还原成功的记录直接复用,失败的重试;
        每次新还原(含失败)都落一条,失败不蒸发。heartbeat:每还原完一条叫一下,让任务页知道还在动
        (链接多、对方慢时这一段能跑很久,没心跳会被当成「卡住了」;审核 2026-10-27 黄 17)。"""
        wanted = list(dict.fromkeys(urls))
        result: dict[str, CitationResolution] = {}
        cutoff = datetime.now(UTC) - self._max_age
        for url, prior in self._storage.latest_citation_resolutions(wanted).items():
            if prior.status in _KNOWN_DESTINATION and prior.resolved_at >= cutoff:
                result[url] = prior
        for url in wanted:
            if url in result:
                continue
            resolution = self._follow(url)
            self._storage.insert_citation_resolution(resolution)
            result[url] = resolution
            if heartbeat is not None:
                heartbeat()
        return result

    def _follow(self, url: str) -> CitationResolution:
        current, hops = url, 0
        while True:
            try:
                response = fetch_headers(self._client, current)
            except httpx.HTTPError as exc:
                log.warning("citation_resolve_failed", url=url, hops=hops, error=type(exc).__name__)
                return _record(url, ResolutionStatus.FETCH_FAILED, hops=hops, error=type(exc).__name__)
            location = response.headers.get("location")
            if response.is_redirect and location:
                hops += 1
                if hops > MAX_HOPS:
                    return _record(url, ResolutionStatus.TOO_MANY_HOPS, hops=MAX_HOPS,
                                   http_status=response.status_code)
                try:
                    target = str(httpx.URL(current).join(location))
                except (httpx.InvalidURL, ValueError):  # 跳转头给了个坏网址:去向看不到,如实记,不让整批还原崩
                    return _record(url, ResolutionStatus.FETCH_FAILED, hops=hops, error="InvalidURL",
                                   http_status=response.status_code)
                if not self._robots_allow(target):
                    log.info("citation_resolve_stopped_at_robots", url=url, target=target, hops=hops)
                    return _record(url, ResolutionStatus.STOPPED_AT_ROBOTS, hops=hops,
                                   http_status=response.status_code, final_url=target)
                current = target
                continue
            if hops == 0:  # 跳转服务没跳:看不到真正去向,如实记,不拿原链接或标题顶替
                return _record(url, ResolutionStatus.NOT_REDIRECTED, hops=0, http_status=response.status_code)
            return _record(url, ResolutionStatus.RESOLVED, hops=hops, http_status=response.status_code,
                           final_url=current)

    def _robots_allow(self, url: str) -> bool:
        """这一站让不让抓(按 robots 的 * 组;读不到当全禁,RFC 9309)。同一站一单只读一次 robots。"""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = robots_parser(self._client, origin)
        return self._robots[origin].can_fetch("*", url)


def _record(
    url: str, status: ResolutionStatus, *, hops: int,
    http_status: int | None = None, error: str | None = None, final_url: str | None = None,
) -> CitationResolution:
    return CitationResolution(
        url=url, final_url=final_url, hops=hops, status=status, http_status=http_status,
        error=error, resolver_version=RESOLVER_VERSION,
    )
