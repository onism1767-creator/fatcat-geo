"""离线测试的公共夹具:一个固定的小站(tests/fixtures/acme),用 httpx.MockTransport 假装联网。全程不出本机。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from butter import sources
from butter.audit import run_audit
from butter.extract.html import extract_site
from butter.factbase import build_factbase
from butter.fetch import FetchLimits, fetch_site
from butter.generate import build_package

FIXTURES = Path(__file__).parent / "fixtures"
ACME = FIXTURES / "acme"
HOST = "www.acme-kettles.test"
SITE = f"https://{HOST}/"
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)

ACME_ROUTES = {
    "/": ("home.html", "text/html; charset=utf-8"),
    "/products/classic": ("classic.html", "text/html; charset=utf-8"),
    "/about": ("about.html", "text/html; charset=utf-8"),
    "/blog/boil-time": ("boil-time.html", "text/html; charset=utf-8"),
    "/robots.txt": ("robots.txt", "text/plain"),
    "/sitemap.xml": ("sitemap.xml", "application/xml"),
}


class Recorder:
    """记下每个请求,供测试断言「只抓了该抓的」。"""

    def __init__(self, routes: dict[str, tuple[str, str]], root: Path, overrides: dict | None = None) -> None:
        self.routes, self.root, self.overrides = routes, root, overrides or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path](request)
        if request.url.host != HOST:
            return httpx.Response(599, text="测试里不该请求别的主机")
        if path in self.routes:
            name, content_type = self.routes[path]
            return httpx.Response(200, content=(self.root / name).read_bytes(), headers={"content-type": content_type})
        return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def fetch_with(recorder: Recorder, url: str = SITE, **limits):
    client = httpx.Client(transport=httpx.MockTransport(recorder), follow_redirects=False)
    try:
        return fetch_site(url, limits=FetchLimits(**limits), client=client, now=lambda: NOW,
                          sleep=lambda _s: None, clock=lambda: 0.0)
    finally:
        client.close()


@pytest.fixture(scope="session")
def acme_recorder() -> Recorder:
    return Recorder(ACME_ROUTES, ACME)


@pytest.fixture(scope="session")
def acme_snapshot(acme_recorder):
    return fetch_with(acme_recorder)


@pytest.fixture(scope="session")
def acme_extracts(acme_snapshot):
    return extract_site(acme_snapshot.pages, acme_snapshot.sitemap_entries)


@pytest.fixture(scope="session")
def acme_factbase(acme_snapshot, acme_extracts):
    return build_factbase(acme_snapshot, acme_extracts, platforms=sources.load_platforms(), run_id="test-run", now=NOW)


@pytest.fixture(scope="session")
def acme_audit(acme_snapshot, acme_extracts):
    return run_audit(acme_snapshot, acme_extracts, run_id="test-run", now=NOW)


@pytest.fixture(scope="session")
def acme_bundle(acme_snapshot, acme_extracts, acme_factbase):
    return build_package(acme_snapshot, acme_extracts, acme_factbase, ua_table=sources.load_ua_table(),
                         excluded=sources.load_excluded_vendors(), platforms=sources.load_platforms(), board=None,
                         training="keep", now=NOW)


def page(extracts, suffix: str):
    return next(p for p in extracts if p.url.endswith(suffix))
