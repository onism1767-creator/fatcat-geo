"""与事实库一致的 Organization / Product JSON-LD。

只写事实库里有的字段;没有的不写、不猜。不带评分 / 评论数。
每个写出去的值都记下它在事实库里的出处(FactRef),供改动清单溯源。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from butter.contracts import ChangeItem, FactBase, FactField, FactRef, NotGenerated, PageExtract, ProductFacts, StructuredNode
from butter.extract.jsonld import ORG_TYPES, PRODUCT_TYPES, as_list, has_type, is_top_level, text_of, texts_of
from butter.registry.ids import STRUCT
from butter.util import norm_text, page_key, safe_http_url

_AVAILABILITY = frozenset({
    "InStock", "OutOfStock", "PreOrder", "PreSale", "BackOrder", "Discontinued", "InStoreOnly", "OnlineOnly",
    "LimitedAvailability", "SoldOut", "MadeToOrder", "Reserved",
})
_NATIVE_SPEC_PROPS = frozenset({"model", "color", "material", "size", "pattern", "gtin", "gtin8", "gtin12", "gtin13",
                                "gtin14", "mpn"})
SNIPPETS_FILE = "jsonld/snippets.html"


def to_json(data: Any) -> str:
    """值来自不可信的网页,而这段 JSON 是要贴进 <script> 的:把 < > & 和行分隔符写成 \\uXXXX。
    解析出来的值一字不差,但字符串里再也凑不出 </script> 或任何标签。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    for ch in ("<", ">", "&", " ", " "):
        text = text.replace(ch, f"\\u{ord(ch):04x}")
    return text


def to_script(data: Any) -> str:
    """可直接贴进页面的 <script>。"""
    return f'<script type="application/ld+json">\n{to_json(data)}\n</script>'


def _first(field: FactField) -> str | None:
    return field.values[0].value if field.values else None


def _refs(prefix: str, field: FactField) -> list[FactRef]:
    return [FactRef(field=f"{prefix}.{field.key}", value=v.value, source_url=v.source_url) for v in field.values]


def _conflicts(prefix: str, fields: list[FactField]) -> list[str]:
    return [f"{prefix}.{f.key}:{f.conflict_note}" for f in fields if f.conflict and f.conflict_note]


def organization(fb: FactBase, origin: str) -> tuple[dict[str, Any] | None, list[FactRef]]:
    e = fb.entity
    name = _first(e.name)
    if name is None:
        return None, []
    data: dict[str, Any] = {"@context": "https://schema.org", "@type": "Organization", "name": name}
    refs = _refs("entity", e.name)
    if e.aliases.values:
        aliases = [v.value for v in e.aliases.values]
        data["alternateName"] = aliases[0] if len(aliases) == 1 else aliases
        refs += _refs("entity", e.aliases)
    data["url"] = f"{origin}/"
    refs.append(FactRef(field="entity.official_domain", value=f"{origin}/", source_url=fb.site_url))
    if (description := _first(e.description)) is not None:
        data["description"] = description
        refs += _refs("entity", e.description)
    if (logo := _first(e.logo)) is not None:
        data["logo"] = logo
        refs += _refs("entity", e.logo)
    if e.same_as.values:
        data["sameAs"] = [v.value for v in e.same_as.values]
        refs += _refs("entity", e.same_as)
    return data, refs


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def product(p: ProductFacts) -> tuple[dict[str, Any] | None, list[FactRef], list[str]]:
    name = _first(p.name)
    if name is None:
        return None, [], []
    prefix = f"products[{p.product_id}]"
    notes: list[str] = []
    data: dict[str, Any] = {"@context": "https://schema.org", "@type": "Product", "name": name}
    refs = _refs(prefix, p.name)
    for key, field in (("description", p.description), ("sku", p.sku), ("category", p.category)):
        if (value := _first(field)) is not None:
            data[key] = value
            refs += _refs(prefix, field)
    if (brand := _first(p.brand)) is not None:
        data["brand"] = {"@type": "Brand", "name": brand}
        refs += _refs(prefix, p.brand)
    data["url"] = p.page_url

    extra = []
    for spec in p.specs:
        ref = FactRef(field=f"{prefix}.specs[{spec.name}]", value=spec.value, source_url=spec.source_url)
        if spec.name in _NATIVE_SPEC_PROPS and spec.source_path.endswith(f".{spec.name}"):
            data[spec.name] = spec.value
        else:
            extra.append({"@type": "PropertyValue", "name": spec.name, "value": spec.value})
        refs.append(ref)
    if extra:
        data["additionalProperty"] = extra

    prices = [v.value for v in p.price.values]
    currencies = [v.value for v in p.currency.values]
    if prices and not currencies:
        notes.append("有价格但事实库里没有币种,没有生成 offers(不猜币种)。")
    elif prices and len({c.upper() for c in currencies}) > 1:
        notes.append(f"事实库里有多个币种({'、'.join(currencies)}),没有生成 offers。")
    elif prices:
        offer: dict[str, Any]
        if len(prices) == 1:
            offer = {"@type": "Offer", "price": prices[0], "priceCurrency": currencies[0]}
        else:
            parsed = [(_decimal(x), x) for x in prices]
            if any(d is None for d, _ in parsed):
                offer = {}
                notes.append("有多个报价且其中有认不出的数字,没有生成 offers。")
            else:
                low, high = min(parsed, key=lambda t: t[0]), max(parsed, key=lambda t: t[0])  # type: ignore[arg-type, return-value]
                offer = {"@type": "AggregateOffer", "lowPrice": low[1], "highPrice": high[1],
                         "priceCurrency": currencies[0], "offerCount": str(len(prices))}
                notes.append(f"事实库里有 {len(prices)} 个报价,合成了价格区间(AggregateOffer)。")
        if offer:
            availability = [v.value for v in p.availability.values]
            if len(availability) == 1 and availability[0] in _AVAILABILITY and offer["@type"] == "Offer":
                offer["availability"] = f"https://schema.org/{availability[0]}"
                refs += _refs(prefix, p.availability)
            offer["url"] = p.page_url
            data["offers"] = offer
            refs += _refs(prefix, p.price) + _refs(prefix, p.currency)
    notes += _conflicts(prefix, [p.name, p.description, p.brand, p.sku, p.price, p.currency, p.availability])
    return data, refs, notes


# ── 与站上现有的结构化数据比对 ───────────────────────────────────────


def _comparable(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(norm_text(t) for t in texts_of(value))
    if isinstance(value, dict):
        return norm_text(text_of(value) or "")
    return norm_text(str(value))


def diff_against(existing: StructuredNode | None, generated: dict[str, Any], base_url: str = "") -> tuple[str, list[str]]:
    """返回 (动作, 差异说明)。只比生成稿里有的字段;offers 只比价格与币种;地址类字段先补成绝对地址再比。"""
    if existing is None:
        return "add", []

    def absolute(value: Any) -> str:
        text = text_of(value.get("url") or value.get("contentUrl")) if isinstance(value, dict) else text_of(value)
        return page_key(safe_http_url(text or "", base_url or None) or (text or ""))

    diffs: list[str] = []
    for key, value in generated.items():
        if key in ("@context", "@type"):
            continue
        current = existing.props.get(key)
        if key == "offers":
            cur_offer = next((o for o in as_list(current) if isinstance(o, dict)), {})
            for sub in ("price", "lowPrice", "highPrice", "priceCurrency"):
                if sub in value and _comparable(value[sub]) != _comparable(cur_offer.get(sub, "")):
                    diffs.append(f"offers.{sub}:站上是「{text_of(cur_offer.get(sub)) or '(无)'}」,事实库是「{value[sub]}」")
            continue
        if key == "additionalProperty":
            cur_names = {norm_text(text_of(p.get("name")) or "") for p in as_list(current) if isinstance(p, dict)}
            missing = [p["name"] for p in value if norm_text(p["name"]) not in cur_names]
            if missing:
                diffs.append(f"additionalProperty:站上缺 {'、'.join(missing[:6])}")
            continue
        if current is None:
            diffs.append(f"{key}:站上没有")
        elif key in ("url", "logo"):
            if absolute(current) != absolute(value):
                diffs.append(f"{key}:站上是「{text_of(current)}」,事实库是「{value}」")
        elif key == "sameAs":
            have = {page_key(u) for t in texts_of(current) if (u := safe_http_url(t, base_url or None))}
            lacking = [u for u in value if page_key(u) not in have]
            if lacking:
                diffs.append(f"sameAs:站上缺 {'、'.join(lacking[:6])}")
        elif _comparable(current) != _comparable(value):
            diffs.append(f"{key}:站上是「{(text_of(current) or '')[:60]}」,事实库是「{str(text_of(value) or value)[:60]}」")
    return ("none" if not diffs else "replace"), diffs


def _existing_org(extracts: list[PageExtract]) -> tuple[StructuredNode | None, str | None]:
    for page in extracts:  # 调用方已按首页优先排好
        for node in page.nodes:
            if node.source_kind == "jsonld" and has_type(node, ORG_TYPES) and is_top_level(node):
                return node, page.url
    return None, None


def _existing_product(extracts: list[PageExtract], p: ProductFacts) -> StructuredNode | None:
    name = norm_text(p.name.values[0].value) if p.name.values else ""
    for page in extracts:
        if page_key(page.url) != page_key(p.page_url):
            continue
        for node in page.nodes:
            if (node.source_kind == "jsonld" and has_type(node, PRODUCT_TYPES)
                    and norm_text(text_of(node.props.get("name")) or "") == name):
                return node
    return None


_ACTION_TEXT = {"add": "本次读到的页面的 HTML 里没有这段 JSON-LD,新增。",
                "replace": "站上已有这段 JSON-LD,下面列出它与事实库不同或缺少的字段;在现有那段里改这些字段即可。",
                "none": "站上已有,且与事实库一致,无需改动(文件仅供对照)。"}
_KEEP_EXISTING = ("生成稿只含事实库里有的字段。站上现有那段里的其他字段(如 image、评分)不在生成稿里:"
                  "不要整段覆盖,只改上面列出的差异。")


def build_jsonld(
    fb: FactBase, extracts: list[PageExtract], origin: str,
) -> tuple[dict[str, str], list[ChangeItem], list[NotGenerated]]:
    files: dict[str, str] = {}
    items: list[ChangeItem] = []
    missing: list[NotGenerated] = []
    snippets: list[str] = ["<!-- Butter(FatCat GEO Improve)生成的结构化数据片段。每段上面的注释写明放在哪一页。 -->", ""]

    org, org_refs = organization(fb, origin)
    if org is None:
        missing.append(NotGenerated(what="Organization JSON-LD", reason="事实库里没有名称,没有生成(不猜名称)。"))
    else:
        existing, existing_url = _existing_org(extracts)
        action, diffs = diff_against(existing, org, existing_url or origin)
        files["jsonld/organization.json"] = to_json(org) + "\n"
        snippets += [f"<!-- Organization:放在首页 {origin}/ 的 <head> 里"
                     + (";站上已有 Organization,对照着只改差异字段,不要整段覆盖" if action == "replace" else "")
                     + " -->", to_script(org), ""]
        e = fb.entity
        needs = _conflicts("entity", [e.name, e.description, e.logo])
        if e.same_as.candidates:
            needs.append(f"另有 {len(e.same_as.candidates)} 个只在内页出现的平台链接没有写进 sameAs(见事实库 same_as.candidates),"
                         "确认是官方账号的可以手动加上。")
        if action == "replace":
            needs.insert(0, _KEEP_EXISTING + (f"现有的 Organization 在 {existing_url}。" if existing_url else ""))
        if action == "add" and not any(page_key(p.url) == page_key(f"{origin}/") for p in extracts):
            needs.insert(0, "本次没有读到首页:放之前请先看一眼首页源码,确认上面没有 Organization JSON-LD,免得重复。")
        items.append(ChangeItem(
            item_id="jsonld-organization", kind="jsonld_organization", entry_id=STRUCT, action=action,  # type: ignore[arg-type]
            target=f"首页 {origin}/ 的 <head>", files=["jsonld/organization.json", SNIPPETS_FILE],
            rationale=_ACTION_TEXT[action] + ("差异:" + ";".join(diffs) + "。" if diffs else "")
                      + "字段全部来自事实库;事实库里没有的字段不写。",
            facts_used=org_refs, validation_state="假设", needs_human=needs))

    for p in fb.products:
        data, refs, notes = product(p)
        if data is None:
            continue
        action, diffs = diff_against(_existing_product(extracts, p), data, p.page_url)
        if action == "replace":
            notes.insert(0, _KEEP_EXISTING)
        path = f"jsonld/product-{p.product_id}.json"
        files[path] = to_json(data) + "\n"
        label = p.name.values[0].value.replace("--", "- -")  # HTML 注释里不能出现 --
        snippets += [f"<!-- Product「{label}」:放在 {p.page_url} 的 <head> 里"
                     + (";站上已有 Product,对照着只改差异字段,不要整段覆盖" if action == "replace" else "")
                     + " -->", to_script(data), ""]
        items.append(ChangeItem(
            item_id=f"jsonld-product-{p.product_id}", kind="jsonld_product", entry_id=STRUCT, action=action,  # type: ignore[arg-type]
            target=f"{p.page_url} 的 <head>", files=[path, SNIPPETS_FILE],
            rationale=_ACTION_TEXT[action] + ("差异:" + ";".join(diffs) + "。" if diffs else "")
                      + "不带评分 / 评论数;价格、规格只取结构化数据和 meta 里已有的。",
            facts_used=refs, validation_state="假设", needs_human=notes))
    if not fb.products:
        missing.append(NotGenerated(what="Product JSON-LD",
                                    reason="事实库里没有商品(本次读到的页面里没有机器可读的商品数据),没有生成。"
                                           "Butter 不从正文里抽价格规格。"))
    if len(snippets) > 2:
        files[SNIPPETS_FILE] = "\n".join(snippets)
    return files, items, missing
