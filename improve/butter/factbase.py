"""事实库自动预填:页面抽取结果 + 本机文件 → FactBase。纯函数。

规则(组件形态决策 决定 2):
- 网站自己说的默认采信;同一字段多个说法按 JSON-LD / microdata > meta / og > 标题 > 本机文件 取,并标冲突。
- 每个值带来源 URL;空缺留空,不猜。
- 价格 / 规格只从结构化数据和 meta / og 取,不从正文自由文本里抽。
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from butter.contracts import (
    EntityFacts, FactBase, FactField, FactValue, Gap, MergedInput, PageExtract, PageFacts, Platform, PotatoBrand,
    PotatoBrands, ProductFacts, SiteSnapshot, SpecFact, StructuredNode, ToastBrandDomains,
)
from butter.extract.dates import to_iso_date
from butter.extract.jsonld import (
    ORG_TYPES, PRODUCT_TYPES, WEBSITE_TYPES, as_list, clean_type, has_type, is_top_level, text_of, texts_of,
)
from butter.util import host_matches, host_of, norm_text, page_key, safe_http_url, same_site, slugify, strip_www

# 来源层级:数字越小越优先
T_STRUCT, T_META, T_TITLE, T_LOCAL = 0, 1, 2, 3
KIND_LABEL = {
    "jsonld": "JSON-LD", "microdata": "microdata", "meta": "meta", "og": "og", "title": "标题", "sitemap": "sitemap",
    "link": "页面链接", "url": "网址", "potato_brands": "brands.yml", "toast_brand_domains": "brand-domains.json",
}
_APP_TYPES = frozenset({"SoftwareApplication", "WebApplication", "MobileApplication"})
_SKIP_PRODUCT_PATHS = (".isRelatedTo", ".isSimilarTo", ".hasVariant", ".isVariantOf", ".isAccessoryOrSparePartFor",
                       ".isConsumableFor")
_SIMPLE_SPEC_PROPS = ("model", "color", "material", "size", "weight", "width", "height", "depth", "pattern",
                      "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn")
_TITLE_SEPARATORS = re.compile(r"\s*[|｜_—–·•]\s*|\s+-\s+|(?<=[一-鿿])-(?=[一-鿿])")
# 不像账号主页的路径:分享、登录、搜索、帮助、政策之类
_NOT_PROFILE = re.compile(
    r"^/(?:share|sharer|sharing|sharearticle|intent|dialog|login|signin|signup|register|home|search|hashtag|explore|"
    r"watch|playlist|results|privacy|policy|policies|terms|tos|legal|help|support|about|ads|business|developers?|"
    r"settings|i|s|p|status|plugins|tr)(?:/|\.php|$)", re.IGNORECASE)
_MAX_PRODUCTS = 50

Candidate = tuple[int, FactValue]  # (层级, 值)


def _fv(value: str, source_url: str, kind: str, path: str) -> FactValue:
    return FactValue(value=value.strip(), source_url=source_url, source_kind=kind, source_path=path)  # type: ignore[arg-type]


def _dedupe(values: list[FactValue], key=lambda v: norm_text(v.value)) -> list[FactValue]:
    seen: set[str] = set()
    out = []
    for v in values:
        k = key(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _conflict_note(chosen: list[FactValue], alternatives: list[FactValue]) -> str:
    took = "、".join(f"「{v.value}」" for v in chosen[:3])
    others = ";".join(f"{KIND_LABEL[a.source_kind]} 说「{a.value}」({a.source_url})" for a in alternatives[:4])
    more = f";另有 {len(alternatives) - 4} 个说法" if len(alternatives) > 4 else ""
    return f"有冲突,已取 {KIND_LABEL[chosen[0].source_kind]} 的{took};{others}{more}"


def pick_single(key: str, candidates: list[Candidate], *, compare=norm_text, substring_ok_tiers: frozenset[int] = frozenset()) -> FactField:
    """单值字段:按层级取第一个;别的说法不同就记为冲突。
    substring_ok_tiers:这些层级的说法只要包含了采用值就不算冲突(标题里带标语很正常)。"""
    ordered = sorted((c for c in candidates if c[1].value), key=lambda c: c[0])  # 稳定排序,同层保持先后
    if not ordered:
        return FactField(key=key)
    chosen = ordered[0][1]
    chosen_key = compare(chosen.value)
    alternatives: list[FactValue] = []
    seen = {chosen_key}
    for tier, value in ordered[1:]:
        k = compare(value.value)
        if k in seen:
            continue
        if tier in substring_ok_tiers and chosen_key and chosen_key in k:
            continue
        seen.add(k)
        alternatives.append(value)
    return FactField(key=key, values=[chosen], alternatives=alternatives, conflict=bool(alternatives),
                     conflict_note=_conflict_note([chosen], alternatives) if alternatives else None)


def pick_group(key: str, groups: list[tuple[int, list[FactValue]]], *, compare=norm_text) -> FactField:
    """可多值的字段(如一个商品多个报价):取层级最高的那一组的全部不同值;别的组说法不同就记为冲突。"""
    ordered = sorted((g for g in groups if g[1]), key=lambda g: g[0])
    if not ordered:
        return FactField(key=key)
    chosen = _dedupe(ordered[0][1], key=lambda v: compare(v.value))
    seen = {compare(v.value) for v in chosen}
    alternatives: list[FactValue] = []
    for _, values in ordered[1:]:
        for value in values:
            if compare(value.value) not in seen:
                seen.add(compare(value.value))
                alternatives.append(value)
    return FactField(key=key, values=chosen, alternatives=alternatives, conflict=bool(alternatives),
                     conflict_note=_conflict_note(chosen, alternatives) if alternatives else None)


def multi(key: str, values: list[FactValue], candidates: list[FactValue] | None = None, *, compare=None) -> FactField:
    compare = compare or (lambda v: norm_text(v.value))
    adopted = _dedupe(values, key=compare)
    adopted_keys = {compare(v) for v in adopted}
    hinted = [c for c in _dedupe(candidates or [], key=compare) if compare(c) not in adopted_keys]
    return FactField(key=key, values=adopted, candidates=hinted)


def _url_key(value: FactValue) -> str:
    parts = urlsplit(value.value)
    return f"{strip_www(parts.hostname or '')}{parts.path.rstrip('/')}".lower()


def _number_key(text: str) -> str:
    """比较价格用:1299、1299.00、1,299 算同一个数。只用于比对,不改存下来的值。"""
    cleaned = re.sub(r"[,\s]", "", text)
    if re.fullmatch(r"\d+\.\d+", cleaned):
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned


def _date_key(raw: str) -> str:
    """比较日期用:同一天的不同写法算同一个。"""
    return to_iso_date(raw) or norm_text(raw)


def _meta(page: PageExtract, key: str, attr: str | None = None) -> str | None:
    for meta in page.metas:
        if meta.key == key and (attr is None or meta.attr == attr) and meta.attr != "itemprop":
            return meta.content
    return None


def _ordered_pages(extracts: list[PageExtract]) -> list[PageExtract]:
    """首页最权威,其次是用户给的那页,再是自动发现的。"""
    def rank(page: PageExtract) -> int:
        if page.role == "home" or urlsplit(page.url).path in ("", "/"):
            return 0
        return 1 if page.role == "given" else 2
    return sorted(extracts, key=rank)


def _org_nodes(page: PageExtract) -> list[StructuredNode]:
    """本页声明的机构节点:顶层的,以及作为 publisher 出现的。商品的 brand、文章的 author 不算。"""
    out = []
    for node in page.nodes:
        if not has_type(node, ORG_TYPES):
            continue
        if is_top_level(node) or re.search(r"\.publisher(?:\[\d+\])?$", node.path):
            out.append(node)
    return out


def _website_nodes(page: PageExtract) -> list[StructuredNode]:
    return [n for n in page.nodes if has_type(n, WEBSITE_TYPES) and is_top_level(n)]


def name_from_title(title: str, host: str) -> tuple[str, str]:
    """从首页标题取名称:按分隔符切段,取与域名同名的那一段;对不上就整句照收(并在空缺清单里提醒)。"""
    label = re.sub(r"[^a-z0-9]", "", strip_www(host).split(".")[0])
    segments = [s.strip() for s in _TITLE_SEPARATORS.split(title) if s.strip()]

    def squash(text: str) -> str:
        return re.sub(r"[^a-z0-9一-鿿]", "", norm_text(text))

    if label:
        exact = [s for s in segments if squash(s) == label]
        if exact:
            return exact[0], "title(按分隔符取与域名同名的一段)"
        containing = [s for s in segments if label in squash(s)]
        if len(containing) == 1 and len(segments) > 1:
            return containing[0], "title(按分隔符取含域名的一段)"
    return title.strip(), "title(整句)"


def _platform_for(host: str, platforms: tuple[Platform, ...] | list[Platform]) -> Platform | None:
    for platform in platforms:
        if any(host_matches(host, h) for h in platform.hosts):
            return platform
    return None


def looks_like_profile(url: str, platform: Platform) -> bool:
    """像不像品牌在这个平台上的主页(而不是分享按钮、平台首页、某篇内容)。"""
    parts = urlsplit(url)
    host = strip_www(parts.hostname or "")
    path = parts.path or "/"
    if parts.query and re.search(r"(?:^|&)(?:url|u|text|title|href|link)=", parts.query, re.IGNORECASE):
        return False
    brand_subdomain = all(host != strip_www(h) and not host.startswith(("m.", "mobile.")) for h in platform.hosts
                          if host_matches(host, h)) and platform.kind == "marketplace_store"
    if path in ("", "/"):
        return brand_subdomain  # 平台首页不算;brand.tmall.com 这种品牌子域算
    if _NOT_PROFILE.match(path):
        return False
    return len([s for s in path.split("/") if s]) <= 3


def _entity(
    pages: list[PageExtract], snapshot: SiteSnapshot, platforms: tuple[Platform, ...] | list[Platform],
    potato: PotatoBrand | None, potato_path: str, toast: ToastBrandDomains | None,
) -> EntityFacts:
    primary = pages[0] if pages else None
    host = snapshot.host

    name_c: list[Candidate] = []
    desc_c: list[Candidate] = []
    category_c: list[Candidate] = []
    logo_c: list[Candidate] = []
    aliases: list[FactValue] = []
    same_as: list[FactValue] = []
    same_as_hint: list[FactValue] = []
    domains: list[FactValue] = [_fv(strip_www(host), snapshot.site_url, "url", "用户给的网址")]
    domain_hint: list[FactValue] = []

    def from_nodes(nodes_of) -> None:
        for page in pages:
            for node in nodes_of(page):
                type_name = node.types[0]
                kind = node.source_kind
                if (v := text_of(node.props.get("name"))) is not None:
                    name_c.append((T_STRUCT, _fv(v, page.url, kind, f"{type_name}.name")))
                if (v := text_of(node.props.get("description"))) is not None:
                    desc_c.append((T_STRUCT, _fv(v, page.url, kind, f"{type_name}.description")))
                for prop in ("alternateName", "legalName"):
                    aliases.extend(_fv(v, page.url, kind, f"{type_name}.{prop}") for v in texts_of(node.props.get(prop)))
                for prop in ("industry", "category"):
                    if (v := text_of(node.props.get(prop))) is not None:
                        category_c.append((T_STRUCT, _fv(v, page.url, kind, f"{type_name}.{prop}")))
                for v in texts_of(node.props.get("sameAs")):
                    if (safe := safe_http_url(v, page.url)) and not same_site(host_of(safe), host):
                        same_as.append(_fv(safe, page.url, kind, f"{type_name}.sameAs"))
                if (v := text_of(node.props.get("url"))) and (safe := safe_http_url(v, page.url)):
                    if not same_site(host_of(safe), host):
                        domain_hint.append(_fv(strip_www(host_of(safe)), page.url, kind, f"{type_name}.url"))
                logo = node.props.get("logo")
                logo_text = text_of(logo.get("url") or logo.get("contentUrl")) if isinstance(logo, dict) else text_of(logo)
                if logo_text and (safe := safe_http_url(logo_text, page.url)):
                    logo_c.append((T_STRUCT, _fv(safe, page.url, kind, f"{type_name}.logo")))

    from_nodes(_org_nodes)
    from_nodes(_website_nodes)

    if primary is not None:
        for node in primary.nodes:
            if has_type(node, _APP_TYPES) and is_top_level(node):
                if (v := text_of(node.props.get("applicationCategory"))) is not None:
                    category_c.append((T_STRUCT, _fv(v, primary.url, node.source_kind, f"{node.types[0]}.applicationCategory")))
        if v := _meta(primary, "og:site_name"):
            name_c.append((T_META, _fv(v, primary.url, "og", "og:site_name")))
        if v := _meta(primary, "application-name"):
            name_c.append((T_META, _fv(v, primary.url, "meta", "meta[name=application-name]")))
        if v := _meta(primary, "description", "name"):
            desc_c.append((T_META, _fv(v, primary.url, "meta", "meta[name=description]")))
        if v := _meta(primary, "og:description"):
            desc_c.append((T_META, _fv(v, primary.url, "og", "og:description")))
        if primary.title:
            value, path = name_from_title(primary.title, host)
            name_c.append((T_TITLE, _fv(value, primary.url, "title", path)))

    # 页面上指向已知平台的链接:首页上的采用,只在内页出现的只提示
    for index, page in enumerate(pages):
        for link in page.links:
            link_host = host_of(link.href)
            if same_site(link_host, host):
                continue
            platform = _platform_for(link_host, platforms)
            if platform is None or platform.kind == "reference" or not looks_like_profile(link.href, platform):
                continue
            where = "首页链接" if index == 0 else "内页链接"
            value = _fv(link.href, page.url, "link", f"{where} → {platform.name}")
            (same_as if index == 0 else same_as_hint).append(value)

    if potato is not None:
        name_c.append((T_LOCAL, _fv(potato.name, potato_path, "potato_brands", "peers[].name")))
        aliases.extend(_fv(a, potato_path, "potato_brands", "peers[].aliases") for a in potato.aliases)
        domains.extend(_fv(strip_www(d), potato_path, "potato_brands", "peers[].domains") for d in potato.domains)
    if toast is not None:
        if toast.brand_name:
            name_c.append((T_LOCAL, _fv(toast.brand_name, toast.path, "toast_brand_domains", "brand.name")))
        aliases.extend(_fv(a, toast.path, "toast_brand_domains", "brand.aliases") for a in toast.brand_aliases)
        domains.extend(_fv(strip_www(d), toast.path, "toast_brand_domains", "list.domains[confirmed]")
                       for d in toast.confirmed_domains)
        domain_hint.extend(_fv(strip_www(d), toast.path, "toast_brand_domains", "list.domains[unconfirmed]")
                           for d in toast.unconfirmed_domains)
        for channel in toast.official_channels:
            if safe := safe_http_url("https://" + channel.lstrip("/")):
                same_as.append(_fv(safe, toast.path, "toast_brand_domains", "list.official_channels"))

    name = pick_single("name", name_c, substring_ok_tiers=frozenset({T_TITLE}))
    chosen_name = norm_text(name.values[0].value) if name.values else ""
    aliases = [a for a in aliases if norm_text(a.value) != chosen_name]
    return EntityFacts(
        name=name,
        aliases=multi("aliases", aliases),
        category=pick_single("category", category_c),
        official_domain=multi("official_domain", domains, domain_hint),
        same_as=multi("same_as", same_as, same_as_hint, compare=_url_key),
        description=pick_single("description", desc_c),
        logo=pick_single("logo", logo_c),
    )


# ── 商品 ─────────────────────────────────────────────────────────────


class _ProductAcc:
    """同一个商品在多页 / 多处出现时,先把各处的说法攒起来,最后统一按层级取。"""

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url
        self.single: dict[str, list[Candidate]] = {}
        self.grouped: dict[str, list[tuple[int, list[FactValue]]]] = {}
        self.specs: list[SpecFact] = []

    def add(self, field: str, tier: int, value: FactValue | None) -> None:
        if value is not None and value.value:
            self.single.setdefault(field, []).append((tier, value))

    def add_group(self, field: str, tier: int, values: list[FactValue]) -> None:
        if values:
            self.grouped.setdefault(field, []).append((tier, values))


def _quantity(value) -> str | None:
    if isinstance(value, dict):
        amount = text_of(value.get("value"))
        unit = text_of(value.get("unitText")) or text_of(value.get("unitCode"))
        if amount is None:
            return text_of(value)
        return f"{amount} {unit}" if unit else amount
    return text_of(value)


def _offer_values(node: StructuredNode, page_url: str) -> tuple[list[FactValue], list[FactValue], list[FactValue]]:
    prices, currencies, availability = [], [], []
    type_name = node.types[0]
    for offer in as_list(node.props.get("offers")):
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        spec = spec if isinstance(spec, dict) else {}
        for prop in ("price", "lowPrice", "highPrice"):
            if (v := text_of(offer.get(prop))) is not None:
                prices.append(_fv(v, page_url, node.source_kind, f"{type_name}.offers.{prop}"))
        if not prices and (v := text_of(spec.get("price"))) is not None:
            prices.append(_fv(v, page_url, node.source_kind, f"{type_name}.offers.priceSpecification.price"))
        if (v := text_of(offer.get("priceCurrency")) or text_of(spec.get("priceCurrency"))) is not None:
            currencies.append(_fv(v, page_url, node.source_kind, f"{type_name}.offers.priceCurrency"))
        if (v := text_of(offer.get("availability"))) is not None:
            availability.append(_fv(clean_type(v), page_url, node.source_kind, f"{type_name}.offers.availability"))
    return prices, currencies, availability


def _specs(node: StructuredNode, page_url: str) -> list[SpecFact]:
    out: list[SpecFact] = []
    type_name = node.types[0]
    for prop in as_list(node.props.get("additionalProperty")):
        if isinstance(prop, dict):
            name, value = text_of(prop.get("name")), _quantity(prop)
            if name and value:
                out.append(SpecFact(name=name, value=value, source_url=page_url, source_kind=node.source_kind,
                                    source_path=f"{type_name}.additionalProperty"))
    for prop in _SIMPLE_SPEC_PROPS:
        if (value := _quantity(node.props.get(prop))) is not None:
            out.append(SpecFact(name=prop, value=value, source_url=page_url, source_kind=node.source_kind,
                                source_path=f"{type_name}.{prop}"))
    return out


def _page_level(acc: _ProductAcc, page: PageExtract) -> None:
    """商品所在页面的更新日期与作者。"""
    tier_of = {"jsonld_modified": T_STRUCT, "meta_modified": T_META, "og_updated": T_META, "sitemap_lastmod": T_TITLE}
    kind_of = {"jsonld_modified": "jsonld", "meta_modified": "meta", "og_updated": "og", "sitemap_lastmod": "sitemap"}
    for d in page.dates:
        if d.kind in tier_of:
            acc.add("updated_date", tier_of[d.kind], _fv(d.raw, d.source_url, kind_of[d.kind], d.source_path))
    for node in page.nodes:
        if "author" in node.props and (v := text_of(node.props.get("author"))) is not None:
            acc.add("author", T_STRUCT, _fv(v, page.url, node.source_kind, f"{node.types[0]}.author"))
    if v := _meta(page, "author", "name"):
        acc.add("author", T_META, _fv(v, page.url, "meta", "meta[name=author]"))


def _products(pages: list[PageExtract]) -> list[ProductFacts]:
    accs: dict[str, _ProductAcc] = {}
    order: list[str] = []
    for page_index, page in enumerate(pages):
        nodes = [n for n in page.nodes if has_type(n, PRODUCT_TYPES)
                 and not any(skip in n.path for skip in _SKIP_PRODUCT_PATHS)]
        for node in nodes:
            name = text_of(node.props.get("name"))
            if name is None:
                continue
            type_name, kind = node.types[0], node.source_kind
            sku = text_of(node.props.get("sku"))
            key = f"{norm_text(name)}|{norm_text(sku or '')}"
            if key not in accs:
                if len(accs) >= _MAX_PRODUCTS:
                    continue
                accs[key] = _ProductAcc(page.url)
                order.append(key)
            acc = accs[key]
            tier = T_STRUCT
            acc.add("name", tier, _fv(name, page.url, kind, f"{type_name}.name"))
            for prop in ("description", "sku", "category"):
                if (v := text_of(node.props.get(prop))) is not None:
                    acc.add(prop, tier, _fv(v, page.url, kind, f"{type_name}.{prop}"))
            if (v := text_of(node.props.get("brand"))) is not None:
                acc.add("brand", tier, _fv(v, page.url, kind, f"{type_name}.brand"))
            prices, currencies, availability = _offer_values(node, page.url)
            group_rank = tier * 1000 + page_index  # 同层里先出现的页面优先
            acc.add_group("price", group_rank, prices)
            acc.add_group("currency", group_rank, currencies)
            acc.add_group("availability", group_rank, availability)
            known = {(s.name, s.value) for s in acc.specs}
            acc.specs.extend(s for s in _specs(node, page.url) if (s.name, s.value) not in known)
            _page_level(acc, page)

        # meta / og 的商品价格:只有本页恰好一个商品(或 og 声明本页是商品页)时才知道它说的是谁
        amount = _meta(page, "product:price:amount") or _meta(page, "og:price:amount")
        currency = _meta(page, "product:price:currency") or _meta(page, "og:price:currency")
        page_keys = {f"{norm_text(text_of(n.props.get('name')) or '')}|{norm_text(text_of(n.props.get('sku')) or '')}"
                     for n in nodes if text_of(n.props.get("name"))}
        target: _ProductAcc | None = None
        if len(page_keys) == 1 and (only := next(iter(page_keys))) in accs:
            target = accs[only]
        elif not page_keys and amount and (og_title := _meta(page, "og:title")) and len(accs) < _MAX_PRODUCTS:
            key = f"{norm_text(og_title)}|"
            if key not in accs:
                accs[key] = _ProductAcc(page.url)
                order.append(key)
            target = accs[key]
            target.add("name", T_META, _fv(og_title, page.url, "og", "og:title"))
            if v := _meta(page, "og:description"):
                target.add("description", T_META, _fv(v, page.url, "og", "og:description"))
            _page_level(target, page)
        if target is not None:
            rank = T_META * 1000 + page_index
            if amount:
                target.add_group("price", rank, [_fv(amount, page.url, "og", "product:price:amount")])
            if currency:
                target.add_group("currency", rank, [_fv(currency, page.url, "og", "product:price:currency")])

    products: list[ProductFacts] = []
    used_ids: set[str] = set()
    for key in order:
        acc = accs[key]
        name = pick_single("name", acc.single.get("name", []))
        base = slugify(name.values[0].value, "product")
        product_id, n = base, 2
        while product_id in used_ids:
            product_id, n = f"{base}-{n}", n + 1
        used_ids.add(product_id)
        products.append(ProductFacts(
            product_id=product_id, page_url=acc.page_url, name=name,
            description=pick_single("description", acc.single.get("description", [])),
            brand=pick_single("brand", acc.single.get("brand", [])),
            sku=pick_single("sku", acc.single.get("sku", [])),
            category=pick_single("category", acc.single.get("category", [])),
            price=pick_group("price", acc.grouped.get("price", []), compare=_number_key),
            currency=pick_group("currency", acc.grouped.get("currency", [])),
            availability=pick_group("availability", acc.grouped.get("availability", [])),
            specs=acc.specs,
            comparison_claims=FactField(key="comparison_claims"),
            updated_date=pick_single("updated_date", acc.single.get("updated_date", []), compare=_date_key),
            author=pick_single("author", acc.single.get("author", [])),
        ))
    return products


# ── 汇总 ─────────────────────────────────────────────────────────────


def match_potato_brand(brands: PotatoBrands, host: str) -> PotatoBrand | None:
    """brands.yml 里哪个品牌是这个站:域名对得上的那个。对不上就不合并,不猜。"""
    for brand in brands.brands:
        if any(host_matches(host, d) for d in brand.domains):
            return brand
    return None


def _gaps(entity: EntityFacts, products: list[ProductFacts], pages: list[PageExtract]) -> list[Gap]:
    gaps: list[Gap] = []
    if not entity.name.values:
        gaps.append(Gap(code="NO_NAME", message="未找到名称(结构化数据、og:site_name、标题里都没有)。"))
    elif entity.name.values[0].source_kind == "title" and "整句" in entity.name.values[0].source_path:
        gaps.append(Gap(code="NAME_FROM_TITLE_ONLY", message="名称只在首页标题里找到,取的是整句标题,可能带标语,建议核对。"))
    if not entity.description.values:
        gaps.append(Gap(code="NO_DESCRIPTION", message="未找到一句话描述(结构化数据、meta description、og:description 里都没有)。"))
    if not entity.category.values:
        gaps.append(Gap(code="NO_CATEGORY", message="未找到类别(网站没有用机器可读的方式声明类别),留空。"))
    if not entity.same_as.values:
        gaps.append(Gap(code="NO_SAME_AS", message="未找到官方渠道(结构化数据 sameAs 为空,首页也没有指向已知平台账号的链接)。"))
    if not products:
        gaps.append(Gap(code="NO_PRODUCT_DATA", message="未找到机器可读的商品数据(JSON-LD / microdata 的 Product),商品类建议补。"))
    for product in products:
        label = product.name.values[0].value if product.name.values else product.product_id
        if not product.price.values:
            gaps.append(Gap(code="PRODUCT_NO_PRICE", message=f"「{label}」未找到价格,商品类建议补。"))
        elif not product.currency.values:
            gaps.append(Gap(code="PRODUCT_NO_CURRENCY", message=f"「{label}」有价格但未找到币种。"))
        if not product.specs:
            gaps.append(Gap(code="PRODUCT_NO_SPECS", message=f"「{label}」未找到机器可读的规格。"))
    if products:
        gaps.append(Gap(code="NO_COMPARISON", message="对比主张没有机器可读的来源,留空;Butter 不从正文里抽对比。"))
    if pages and not any(p.dates for p in pages):
        gaps.append(Gap(code="NO_DATES", message="抓到的页面上没有找到任何日期。"))
    return gaps


def build_factbase(
    snapshot: SiteSnapshot,
    extracts: list[PageExtract],
    *,
    platforms: tuple[Platform, ...] | list[Platform],
    run_id: str,
    now: datetime,
    brands: PotatoBrands | None = None,
    brand_domains: ToastBrandDomains | None = None,
) -> FactBase:
    pages = _ordered_pages(extracts)
    merged: list[MergedInput] = []

    potato: PotatoBrand | None = None
    if brands is not None:
        potato = match_potato_brand(brands, snapshot.host)
        merged.append(MergedInput(
            kind="potato_brands", path=brands.path, status="merged" if potato else "skipped",
            note=f"按域名对上了品牌「{potato.name}」" if potato else f"里面没有域名对得上 {snapshot.host} 的品牌,没有合并"))
    toast: ToastBrandDomains | None = None
    if brand_domains is not None:
        if brand_domains.primary and host_matches(snapshot.host, brand_domains.primary):
            toast = brand_domains
            merged.append(MergedInput(kind="toast_brand_domains", path=brand_domains.path, status="merged",
                                      note=f"主站 {brand_domains.primary} 与本站一致"))
        else:
            merged.append(MergedInput(kind="toast_brand_domains", path=brand_domains.path, status="skipped",
                                      note=f"主站是 {brand_domains.primary or '(空)'},与 {snapshot.host} 对不上,没有合并"))

    entity = _entity(pages, snapshot, platforms, potato, brands.path if brands else "", toast)
    products = _products(pages)
    page_facts = [
        PageFacts(url=p.url, role=p.role, title=p.title, h1=p.h1, h2=p.h2,
                  meta_description=_meta(p, "description", "name"), lang=p.lang, dates=p.dates)
        for p in pages
    ]
    seen_pages: set[str] = set()
    unique_pages = []
    for pf in page_facts:
        if page_key(pf.url) not in seen_pages:
            seen_pages.add(page_key(pf.url))
            unique_pages.append(pf)
    return FactBase(
        site_url=snapshot.site_url, host=snapshot.host, run_id=run_id, generated_at=now, entity=entity,
        products=products, pages=unique_pages, gaps=_gaps(entity, products, pages), merged_inputs=merged,
    )
