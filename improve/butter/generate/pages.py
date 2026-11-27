"""事实页 HTML 与价格 / 规格事实块 HTML。

内容只来自事实库,Jinja2 自动转义(事实库的值源自不可信的网页)。
页面上的日期写的是「信息读取日期」,不写「最后更新」:Butter 不知道这些事实是哪天更新的,不替站长编一个。
"""

from __future__ import annotations

from jinja2 import Environment, StrictUndefined
from markupsafe import Markup

from butter.contracts import ChangeItem, FactBase, FactRef, NotGenerated, ProductFacts
from butter.generate.jsonld import organization, to_script
from butter.registry.ids import FACT_PAGE, PRICE_SPECS_COMPARISON
from butter.util import host_matches, host_of, safe_http_url

_ENV = Environment(autoescape=True, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)

LABELS = {
    "zh": {
        "title": "{name}:基本事实", "about": "关于", "site": "官方网站", "aliases": "又称", "category": "类别",
        "channels": "官方渠道", "products": "产品", "product": "产品", "price": "价格", "availability": "库存状态",
        "specs": "规格", "read_on": "以上信息于 {date} 取自 {host}。", "item": "项目", "value": "内容",
    },
    "en": {
        "title": "{name}: Facts", "about": "About", "site": "Official website", "aliases": "Also known as",
        "category": "Category", "channels": "Official channels", "products": "Products", "product": "Product",
        "price": "Price", "availability": "Availability", "specs": "Specifications",
        "read_on": "The information above was read from {host} on {date}.", "item": "Item", "value": "Value",
    },
}

_FACT_PAGE = _ENV.from_string("""<!doctype html>
<html lang="{{ lang }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
{% if description %}
<meta name="description" content="{{ description }}">
{% endif %}
{{ org_script }}
</head>
<body>
<main>
<h1>{{ title }}</h1>
{% if description %}
<p>{{ description }}</p>
{% endif %}
<h2>{{ L.about }} {{ name }}</h2>
<dl>
<dt>{{ L.site }}</dt>
<dd><a href="{{ site_url }}">{{ host }}</a></dd>
{% if aliases %}
<dt>{{ L.aliases }}</dt>
<dd>{{ aliases | join(" / ") }}</dd>
{% endif %}
{% if category %}
<dt>{{ L.category }}</dt>
<dd>{{ category }}</dd>
{% endif %}
</dl>
{% if channels %}
<h2>{{ L.channels }}</h2>
<ul>
{% for url in channels %}
<li><a href="{{ url }}" rel="me">{{ url }}</a></li>
{% endfor %}
</ul>
{% endif %}
{% if products %}
<h2>{{ L.products }}</h2>
{% for p in products %}
<h3>{{ p.name }}</h3>
{% if p.description %}
<p>{{ p.description }}</p>
{% endif %}
{% if p.rows %}
<table>
<thead><tr><th scope="col">{{ L.item }}</th><th scope="col">{{ L.value }}</th></tr></thead>
<tbody>
{% for label, value in p.rows %}
<tr><th scope="row">{{ label }}</th><td>{{ value }}</td></tr>
{% endfor %}
</tbody>
</table>
{% endif %}
{% endfor %}
{% endif %}
<p><small>{{ read_on }}</small></p>
</main>
</body>
</html>
""")

_FACT_BLOCK = _ENV.from_string("""<table>
<caption>{{ name }}</caption>
<thead><tr><th scope="col">{{ L.item }}</th><th scope="col">{{ L.value }}</th></tr></thead>
<tbody>
{% for label, value in rows %}
<tr><th scope="row">{{ label }}</th><td>{{ value }}</td></tr>
{% endfor %}
</tbody>
</table>
""")


def pick_lang(fb: FactBase) -> str:
    """跟着首页的 lang 走:zh 开头用中文标签,其余用英文。"""
    lang = next((p.lang for p in fb.pages if p.lang), None) or ""
    return "zh" if lang.lower().startswith("zh") else "en"


def _price_text(p: ProductFacts) -> str | None:
    prices = [v.value for v in p.price.values]
    currencies = [v.value for v in p.currency.values]
    if not prices:
        return None
    unit = f" {currencies[0]}" if len({c.upper() for c in currencies}) == 1 else ""
    return " / ".join(f"{x}{unit}" for x in prices)


def product_rows(p: ProductFacts, labels: dict[str, str]) -> tuple[list[tuple[str, str]], list[FactRef]]:
    prefix = f"products[{p.product_id}]"
    rows: list[tuple[str, str]] = []
    refs: list[FactRef] = []
    if (price := _price_text(p)) is not None:
        rows.append((labels["price"], price))
        refs += [FactRef(field=f"{prefix}.price", value=v.value, source_url=v.source_url) for v in p.price.values]
        refs += [FactRef(field=f"{prefix}.currency", value=v.value, source_url=v.source_url) for v in p.currency.values]
    if len(p.availability.values) == 1:
        rows.append((labels["availability"], p.availability.values[0].value))
        refs.append(FactRef(field=f"{prefix}.availability", value=p.availability.values[0].value,
                            source_url=p.availability.values[0].source_url))
    for spec in p.specs:
        rows.append((spec.name, spec.value))
        refs.append(FactRef(field=f"{prefix}.specs[{spec.name}]", value=spec.value, source_url=spec.source_url))
    return rows, refs


def build_pages(
    fb: FactBase, origin: str, read_date: str, reference_hosts: frozenset[str] = frozenset(), read_note: str = "",
) -> tuple[dict[str, str], list[ChangeItem], list[NotGenerated]]:
    files: dict[str, str] = {}
    items: list[ChangeItem] = []
    missing: list[NotGenerated] = []
    lang = pick_lang(fb)
    labels = LABELS[lang]
    e = fb.entity

    # ── 事实块(每个有价格或规格的商品一张表) ──
    product_views = []
    for p in fb.products:
        if not p.name.values:
            continue
        rows, refs = product_rows(p, labels)
        name = p.name.values[0].value
        product_views.append({"name": name, "rows": rows,
                              "description": p.description.values[0].value if p.description.values else None,
                              "refs": refs, "id": p.product_id})
        if not rows:
            missing.append(NotGenerated(what=f"「{name}」的价格 / 规格事实块",
                                        reason="事实库里这个商品既没有价格也没有规格,没有生成空表。"))
            continue
        path = f"blocks/product-{p.product_id}.html"
        files[path] = _FACT_BLOCK.render(name=name, rows=rows, L=labels)
        notes = [f"{f.key}:{f.conflict_note}" for f in (p.price, p.currency, p.availability) if f.conflict and f.conflict_note]
        items.append(ChangeItem(
            item_id=f"block-{p.product_id}", kind="fact_block", entry_id=PRICE_SPECS_COMPARISON, action="add",
            target=f"{p.page_url} 的正文(读者看得到的位置)", files=[path],
            rationale="把结构化数据里已有的价格 / 规格用一张读者看得到的表格写出来,让机器读到的和人看到的一致。"
                      "表里只有事实库里已有的值。",
            facts_used=refs, validation_state="假设", needs_human=notes))
    if fb.products:
        missing.append(NotGenerated(what="对比表事实块", reason="事实库里没有对比主张(没有机器可读的来源,Butter 不从正文里抽),没有生成。"))
    else:
        missing.append(NotGenerated(what="价格 / 规格 / 对比表事实块", reason="事实库里没有商品,没有生成。"))

    # ── 事实页 ──
    name = e.name.values[0].value if e.name.values else None
    if name is None:
        missing.append(NotGenerated(what="事实页 HTML", reason="事实库里没有名称,没有生成。"))
        return files, items, missing
    org, org_refs = organization(fb, origin)
    # 百科、企业名录这类参考页可以进 JSON-LD 的 sameAs,但不是品牌自己的渠道,不列在「官方渠道」下
    channels = [u for v in e.same_as.values if (u := safe_http_url(v.value))
                and not any(host_matches(host_of(u), h) for h in reference_hosts)]
    description = e.description.values[0].value if e.description.values else None
    files["pages/facts.html"] = _FACT_PAGE.render(
        lang="zh-CN" if lang == "zh" else "en", L=labels, name=name, title=labels["title"].format(name=name),
        description=description, org_script=Markup(to_script(org)) if org else "",
        site_url=f"{origin}/", host=fb.host, aliases=[v.value for v in e.aliases.values],
        category=e.category.values[0].value if e.category.values else None, channels=channels,
        products=product_views, read_on=labels["read_on"].format(date=read_date, host=fb.host),
    )
    refs = list(org_refs)
    if e.category.values:
        refs.append(FactRef(field="entity.category", value=e.category.values[0].value,
                            source_url=e.category.values[0].source_url))
    for view in product_views:
        refs += view["refs"]
    needs = ["这是一张新页面:放上去之前请通读一遍,确认每一条都与现实一致;以后事实变了要跟着改。",
             "页面底部写的是信息读取日期,不是「最后更新」。想显示更新日期,请写你真实更新的那天。",
             "建议路径 /facts(或你站上已有的「关于」页);页面样式沿用你站点的模板即可,这里只给内容骨架。"]
    if read_note:
        needs.insert(2, read_note)
    needs += [f"entity.{f.key}:{f.conflict_note}" for f in (e.name, e.description) if f.conflict and f.conflict_note]
    items.append(ChangeItem(
        item_id="fact-page", kind="fact_page", entry_id=FACT_PAGE, action="add", target="新页面,建议路径 /facts",
        files=["pages/facts.html"],
        rationale="把品牌的基本事实(名称、官网、官方渠道、产品的价格与规格)集中写在一张稳定的页面上。"
                  "内容全部来自事实库,事实库里没有的不写。",
        facts_used=refs, validation_state="假设", needs_human=needs))
    return files, items, missing
