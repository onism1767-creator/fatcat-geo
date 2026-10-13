"""L2 官网名单自动发现(产品文档 15.2 D12 / TK-09;架构书附录 v0.4.26 第 3 节 C1;S5)。

用户只填主站。判断标准只有一条:**主站自己承认的才算**——名字一样不算数(brand.com.cn 可能是别人的或抢注的),
对方单方面宣称也不算数(任何网站都能单方面声称自己是别人的地区版本)。

采样前读主站首页(HTML 与响应头)、robots.txt 声明的 sitemap(最多 MAX_SITEMAP_FILES 个文件)、候选域名首页
(最多 MAX_CANDIDATE_HOMEPAGES 个,预算贯穿采样前后):
  A1 主站 hreflang 声明(HTML <link rel=alternate hreflang>、HTTP Link 头、sitemap 的 xhtml:link);对方也指回 → 注明双向
  A2 主站 Organization / WebSite 结构化数据的 url / sameAs
  A3 主站首页直接链接到它,且网址名相同(注册域名主体相同、后缀不同)
→ 自动计入,每项附依据。采样后把回答里出现、清单外的同名域名再查一次:
  B1 主站没有任何依据指向它;B2 只有对方单方面声明(它指向主站,主站没指回)→ 只提示,用户确认才算。
官方渠道(电商旗舰店、官方社交账号)只认主站 sameAs 或首页链接指向、且主机在 config/platforms.yaml 上的页面,
按「主机名 + 路径前缀」单列,不按注册域名(否则整个 tmall.com 都算进去)。

抓网页走 crawler/(Chrome 身份,D13);网页内容一律当数据,只做机械解析,不回喂模型。本模块不做任何引用判定。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from html.parser import HTMLParser
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any, Final
from urllib import robotparser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
import structlog
import yaml

from crawler.fetch import MAX_PARSED_BYTES, get_as_browser, is_html, new_client
from engine.detectors.matching import registrable
from engine.models import (
    BrandDomain,
    BrandDomainList,
    DomainEvidence,
    DomainStatus,
    DomainTier,
    FrozenModel,
    OfficialChannel,
)
from engine.official_domains import DomainCandidate

log = structlog.get_logger("engine.brand_domains")

MAX_SITEMAP_FILES: Final = 3
MAX_CANDIDATE_HOMEPAGES: Final = 20
DEFAULT_PLATFORMS_PATH: Final = Path(__file__).resolve().parents[1] / "config" / "platforms.yaml"
_PUBLIC_SUFFIX_LIST: Final = f"snapshot bundled with tldextract {_package_version('tldextract')}"
_SCHEMA_TYPES: Final = frozenset({"Organization", "Corporation", "WebSite"})
_TIER_ORDER: Final = (DomainTier.A1, DomainTier.A2, DomainTier.A3)


class Platform(FrozenModel):
    name: str
    kind: str
    hosts: list[str]
    oembed: str | None = None  # 公开嵌入接口模板,{url} 替换成被引链接(S6.3)


def load_platforms(path: Path = DEFAULT_PLATFORMS_PATH) -> list[Platform]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [Platform.model_validate(item) for item in data["platforms"]]


# ── 机械解析(网页内容是数据) ─────────────────────────────────────────


class _PageParser(HTMLParser):
    """只收三样东西:hreflang 声明、JSON-LD 脚本、页面链接。"""

    def __init__(self) -> None:
        super().__init__()
        self.hreflangs: list[tuple[str, str]] = []  # (hreflang, href)
        self.links: list[str] = []
        self.jsonld: list[str] = []
        self._in_jsonld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("hreflang") and a.get("href"):
            self.hreflangs.append((a["hreflang"], a["href"]))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_jsonld = False

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self.jsonld.append(data)


class PageFacts(FrozenModel):
    """一个页面里与官网名单有关的机械事实(绝对 URL)。"""

    url: str
    hreflangs: list[tuple[str, str]] = []  # (hreflang, 绝对 URL)
    schema_urls: list[tuple[str, str]] = []  # (字段 url / sameAs, 绝对 URL),只取 Organization / WebSite
    links: list[str] = []


def parse_page(html: str, page_url: str) -> PageFacts:
    parser = _PageParser()
    parser.feed(html[:MAX_PARSED_BYTES])
    schema_urls: list[tuple[str, str]] = []
    for blob in parser.jsonld:
        try:
            schema_urls += _schema_urls(json.loads(blob))
        except ValueError:
            continue  # 坏 JSON-LD:跳过,不猜
    return PageFacts(
        url=page_url,
        hreflangs=[(lang, urljoin(page_url, href)) for lang, href in parser.hreflangs],
        schema_urls=[(field, urljoin(page_url, url)) for field, url in schema_urls],
        links=[urljoin(page_url, href) for href in parser.links],
    )


def _schema_urls(node: Any) -> list[tuple[str, str]]:
    """Organization / WebSite 节点(含 @graph、列表嵌套)的 url 与 sameAs。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, list):
        for item in node:
            found += _schema_urls(item)
        return found
    if not isinstance(node, dict):
        return found
    types = node.get("@type")
    type_set = set(types) if isinstance(types, list) else {types} if isinstance(types, str) else set()
    if type_set & _SCHEMA_TYPES:
        for field in ("url", "sameAs"):
            value = node.get(field)
            values = value if isinstance(value, list) else [value]
            found += [(field, v) for v in values if isinstance(v, str) and v.startswith(("http://", "https://"))]
    for key in ("@graph", "publisher", "brand"):
        if key in node:
            found += _schema_urls(node[key])
    return found


_LINK_HEADER_ENTRY = re.compile(r"<([^>]+)>((?:\s*;[^,]*)*)")


def parse_link_header(value: str, page_url: str) -> list[tuple[str, str]]:
    """HTTP Link 头里的 rel=alternate hreflang 声明 → (hreflang, 绝对 URL)。"""
    found: list[tuple[str, str]] = []
    for url, params in _LINK_HEADER_ENTRY.findall(value or ""):
        lowered = params.lower()
        if "rel=" in lowered and "alternate" in lowered:
            match = re.search(r'hreflang="?([^";]+)"?', params)
            if match:
                found.append((match.group(1).strip(), urljoin(page_url, url.strip())))
    return found


def parse_sitemap(xml_text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """sitemap 文件 → (子 sitemap 列表, hreflang 声明)。坏 XML 抛 ElementTree.ParseError,由调用方记说明。"""
    root = ElementTree.fromstring(xml_text[:MAX_PARSED_BYTES])
    children: list[str] = []
    hreflangs: list[tuple[str, str]] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "sitemap":
            loc = next((c.text for c in element if c.tag.rsplit("}", 1)[-1] == "loc" and c.text), None)
            if loc:
                children.append(loc.strip())
        elif tag == "link" and element.get("rel") == "alternate" and element.get("hreflang") and element.get("href"):
            hreflangs.append((element.get("hreflang", ""), element.get("href", "")))
    return children, hreflangs


# ── 发现 ────────────────────────────────────────────────────────────


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def channel_match(url: str) -> str:
    """官方渠道的匹配键:主机名 + 路径前缀(去查询串;空路径记为 /)。"""
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path or '/'}"


class BrandDomainDiscovery:
    def __init__(self, platforms: list[Platform], *, client: httpx.Client | None = None) -> None:
        self._platforms = platforms
        self._own_client = client is None
        self._client = client or new_client()

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    # ── 采样前:主站自己承认的 ──

    def discover(self, primary_url: str) -> BrandDomainList:
        primary_label, primary_domain = registrable(_host(primary_url))
        builder = _ListBuilder(primary_domain, primary_label, self._platforms)
        response = self._fetch(primary_url)
        if response is None or not response.is_success or not is_html(response):
            detail = "unreachable" if response is None else f"HTTP {response.status_code}"
            builder.note("homepage_unreachable", source=primary_url, detail=detail)
            return builder.build()
        page_url = str(response.url)
        for lang, href in parse_link_header(response.headers.get("link", ""), page_url):
            builder.hreflang(href, lang, kind="link_header", source=page_url)
        facts = parse_page(response.text, page_url)
        for lang, href in facts.hreflangs:
            builder.hreflang(href, lang, kind="hreflang", source=page_url)
        for field, url in facts.schema_urls:
            if not builder.channel(url, kind="jsonld_sameas", source=page_url):
                builder.domain(url, DomainTier.A2, kind="jsonld", source=page_url, detail=field)
        for href in facts.links:
            if builder.channel(href, kind="homepage_link", source=page_url):
                continue
            label, domain = registrable(_host(href))
            if label == primary_label and domain != primary_domain:
                builder.domain(href, DomainTier.A3, kind="homepage_link", source=page_url)
        self._read_sitemaps(page_url, builder)
        for entry in builder.ordered_domains():  # 对方指不指回主站:注明双向 / 单向
            if not self._homepage_budget_left(builder):
                builder.note("homepage_limit")
                break
            points_back = self._points_to(entry, builder, primary_domain)
            builder.annotate(entry, points_back)
        return builder.build()

    # ── 采样后:回答里出现、清单外的同名域名 ──

    def look_alikes(self, domain_list: BrandDomainList, candidates: Iterable[DomainCandidate]) -> BrandDomainList:
        """B1 / B2 只提示;已在名单里的(含用户去掉的)不再列。"""
        known = {d.domain for d in domain_list.domains} | {domain_list.primary}
        fetches = domain_list.candidate_fetches
        notes = list(domain_list.notes)
        added: list[BrandDomain] = []
        for candidate in candidates:
            if candidate.domain in known:
                continue
            evidence = [DomainEvidence(kind="seen", detail=str(candidate.count))]
            tier = DomainTier.B1
            if fetches < MAX_CANDIDATE_HOMEPAGES:
                fetches += 1
                homepage = f"https://{candidate.domain}/"
                facts = self._facts_of(homepage)
                if facts is not None and _points_to_domain(facts, domain_list.primary):
                    tier = DomainTier.B2
                    evidence.append(DomainEvidence(kind="one_way", source=facts.url))
            elif not any(n.kind == "homepage_limit" for n in notes):
                notes.append(DomainEvidence(kind="homepage_limit"))
            added.append(BrandDomain(domain=candidate.domain, tier=tier, status=DomainStatus.UNCONFIRMED,
                                     evidence=evidence, seen_count=candidate.count))
        return domain_list.model_copy(update={
            "domains": [*domain_list.domains, *added], "notes": notes, "candidate_fetches": fetches,
        })

    # ── 内部 ──

    def _fetch(self, url: str) -> httpx.Response | None:
        try:
            return get_as_browser(self._client, url)
        except httpx.HTTPError as exc:
            log.warning("brand_domains_fetch_failed", url=url, error=type(exc).__name__)
            return None

    def _facts_of(self, url: str) -> PageFacts | None:
        response = self._fetch(url)
        if response is None or not response.is_success or not is_html(response):
            return None
        page_url = str(response.url)
        facts = parse_page(response.text, page_url)
        header_hreflangs = parse_link_header(response.headers.get("link", ""), page_url)
        return facts.model_copy(update={"hreflangs": [*facts.hreflangs, *header_hreflangs]})

    def _read_sitemaps(self, page_url: str, builder: _ListBuilder) -> None:
        robots = self._fetch(urljoin(page_url, "/robots.txt"))
        if robots is None or robots.status_code != 200:
            return
        parser = robotparser.RobotFileParser()
        parser.parse(robots.text.splitlines())
        queue = list(parser.site_maps() or [])
        read = 0
        while queue:
            if read >= MAX_SITEMAP_FILES:
                builder.note("sitemap_limit")
                break
            url = queue.pop(0)
            read += 1
            response = self._fetch(url)
            if response is None or not response.is_success:
                continue
            try:
                children, hreflangs = parse_sitemap(response.text)
            except ElementTree.ParseError as exc:
                builder.note("parse_error", source=url, detail=type(exc).__name__)
                continue
            queue += children
            for lang, href in hreflangs:
                builder.hreflang(href, lang, kind="sitemap_hreflang", source=url)

    def _homepage_budget_left(self, builder: _ListBuilder) -> bool:
        return builder.fetches < MAX_CANDIDATE_HOMEPAGES

    def _points_to(self, entry: BrandDomain, builder: _ListBuilder, primary_domain: str) -> bool | None:
        builder.fetches += 1
        facts = self._facts_of(builder.homepage_of(entry.domain))
        return None if facts is None else _points_to_domain(facts, primary_domain)


def _points_to_domain(facts: PageFacts, primary_domain: str) -> bool:
    urls = [href for _, href in facts.hreflangs] + [url for _, url in facts.schema_urls]
    return any(registrable(_host(u))[1] == primary_domain for u in urls)


class _ListBuilder:
    def __init__(self, primary_domain: str, primary_label: str, platforms: list[Platform]) -> None:
        self.primary_domain, self.primary_label = primary_domain, primary_label
        self._platforms = platforms
        self._domains: dict[str, BrandDomain] = {}
        self._homepages: dict[str, str] = {}  # 域名 → 主站声明里给出的那个主机的首页(对方指不指回就看它)
        self._channels: dict[str, OfficialChannel] = {}
        self._notes: list[DomainEvidence] = []
        self.fetches = 0

    def hreflang(self, href: str, lang: str, *, kind: str, source: str) -> None:
        self.domain(href, DomainTier.A1, kind=kind, source=source, detail=lang)

    def domain(self, url: str, tier: DomainTier, *, kind: str, source: str, detail: str | None = None) -> None:
        label, domain = registrable(_host(url))
        if not label or not domain or domain == self.primary_domain:
            return
        evidence = DomainEvidence(kind=kind, source=source, detail=detail)
        parts = urlsplit(url)
        self._homepages.setdefault(domain, f"{parts.scheme or 'https'}://{parts.hostname}/")
        existing = self._domains.get(domain)
        if existing is None:
            self._domains[domain] = BrandDomain(
                domain=domain, tier=tier, status=DomainStatus.CONFIRMED, evidence=[evidence],
            )
            return
        if evidence in existing.evidence:
            return
        strongest = min((existing.tier, tier), key=_TIER_ORDER.index)
        self._domains[domain] = existing.model_copy(
            update={"tier": strongest, "evidence": [*existing.evidence, evidence]},
        )

    def channel(self, url: str, *, kind: str, source: str) -> bool:
        """主机在平台清单上 → 记官方渠道并返回 True;否则 False(交给域名规则)。"""
        host = _host(url)
        platform = next((p for p in self._platforms if any(host == h or host.endswith("." + h) for h in p.hosts)), None)
        if platform is None:
            return False
        match = channel_match(url)
        if match not in self._channels:
            self._channels[match] = OfficialChannel(
                match=match, platform=platform.name, kind=platform.kind,
                evidence=DomainEvidence(kind=kind, source=source),
            )
        return True

    def note(self, kind: str, *, source: str | None = None, detail: str | None = None) -> None:
        self._notes.append(DomainEvidence(kind=kind, source=source, detail=detail))

    def ordered_domains(self) -> list[BrandDomain]:
        return list(self._domains.values())

    def homepage_of(self, domain: str) -> str:
        return self._homepages.get(domain, f"https://{domain}/")

    def annotate(self, entry: BrandDomain, points_back: bool | None) -> None:
        if points_back is None:
            return  # 对方首页没打开:不知道指不指回,不写
        kind = "back_link" if points_back else "no_back_link"
        self._domains[entry.domain] = entry.model_copy(update={
            "bidirectional": points_back, "evidence": [*entry.evidence, DomainEvidence(kind=kind)],
        })

    def build(self) -> BrandDomainList:
        return BrandDomainList(
            primary=self.primary_domain,
            discovered_at=datetime.now(UTC),
            public_suffix_list=_PUBLIC_SUFFIX_LIST,
            domains=list(self._domains.values()),
            official_channels=list(self._channels.values()),
            notes=self._notes,
            candidate_fetches=self.fetches,
        )


# ── 用户的确认与移除 ──────────────────────────────────────────────────


def apply_user_choice(
    domain_list: BrandDomainList, *, confirm: Iterable[str], remove: Iterable[str],
) -> BrandDomainList:
    """B 类确认 → 计入(依据记「用户确认」);A 类移除 → 不算(留痕)。不在名单里的网址一律不认。"""
    confirm_set, remove_set = set(confirm), set(remove)
    domains = []
    for d in domain_list.domains:
        if d.domain in confirm_set and d.status is DomainStatus.UNCONFIRMED:
            d = d.model_copy(update={"status": DomainStatus.CONFIRMED,
                                     "evidence": [*d.evidence, DomainEvidence(kind="user_confirmed")]})
        elif d.domain in remove_set and d.status is DomainStatus.CONFIRMED:
            d = d.model_copy(update={"status": DomainStatus.REMOVED,
                                     "evidence": [*d.evidence, DomainEvidence(kind="user_removed")]})
        domains.append(d)
    return domain_list.model_copy(update={"domains": domains})
