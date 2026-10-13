"""L2 引用链接还原跳转(架构书附录 v0.4.26 第 2 节 C2;S4)。

有的仪器给的引用是平台跳转链(能力声明 citation_urls_redirect):链接的主机名是跳转服务,不是真正的来源。
采样后对这些链接逐条跟随跳转——最多 MAX_HOPS 跳、只取响应头不读正文、按链接去重——结果写入只追加表
citation_resolution(原链接、终点链接、跳数、状态、还原器版本)。判定一律用终点链接;还原不出去向的
那条引用记「看不到」,不拿标题代替(铁律 7)。

经 crawler/ 发请求(Chrome 身份,D13)。这里不做任何品牌判定——归属判定在 detectors/。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import structlog

from crawler.fetch import fetch_headers, new_client
from engine.models import CitationResolution, ResolutionStatus

log = structlog.get_logger("engine.resolve")

RESOLVER_VERSION = 1
MAX_HOPS = 10


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
            if r.status is ResolutionStatus.RESOLVED and r.final_url is not None
        })


class UrlResolver:
    def __init__(
        self, storage: ResolutionStore, *, client: httpx.Client | None = None, max_age_days: int = 30,
    ) -> None:
        self._storage = storage
        self._own_client = client is None
        self._client = client or new_client(follow_redirects=False)
        self._max_age = timedelta(days=max_age_days)

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def resolve(self, urls: list[str]) -> dict[str, CitationResolution]:
        """一批链接 → {原链接: 还原记录}。按链接去重;库里时效内还原成功的记录直接复用,失败的重试;
        每次新还原(含失败)都落一条,失败不蒸发。"""
        wanted = list(dict.fromkeys(urls))
        result: dict[str, CitationResolution] = {}
        cutoff = datetime.now(UTC) - self._max_age
        for url, prior in self._storage.latest_citation_resolutions(wanted).items():
            if prior.status is ResolutionStatus.RESOLVED and prior.resolved_at >= cutoff:
                result[url] = prior
        for url in wanted:
            if url in result:
                continue
            resolution = self._follow(url)
            self._storage.insert_citation_resolution(resolution)
            result[url] = resolution
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
                current = str(httpx.URL(current).join(location))
                continue
            if hops == 0:  # 跳转服务没跳:看不到真正去向,如实记,不拿原链接或标题顶替
                return _record(url, ResolutionStatus.NOT_REDIRECTED, hops=0, http_status=response.status_code)
            return _record(url, ResolutionStatus.RESOLVED, hops=hops, http_status=response.status_code,
                           final_url=current)


def _record(
    url: str, status: ResolutionStatus, *, hops: int,
    http_status: int | None = None, error: str | None = None, final_url: str | None = None,
) -> CitationResolution:
    return CitationResolution(
        url=url, final_url=final_url, hops=hops, status=status, http_status=http_status,
        error=error, resolver_version=RESOLVER_VERSION,
    )
