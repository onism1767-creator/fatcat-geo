"""切任务(纯层):每页一个起草任务,只带「本页能用的事实」和「对上本页的题」。设计 §7.1、§7.2。

写哪些位置(全部是加法):
  meta_description   每页都写(有事实可用时)
  fact_block_lead    本页有商品,且商品有价格或规格时
  answer_first       本页对上了题时,只回应最贴的那一道
  title、h1          本页对上了题时,给一个备选写法(主建议仍是规则出的那条,§7.1)
没有任何事实可用的页面不起草;超出页数上限的页面不起草。
"""

from __future__ import annotations

from butter.contracts import (
    CheckContext, DraftFact, DraftLimits, DraftTask, FactBase, PageExtract, PageFacts, PotatoBrands, ProductFacts,
    Question,
)
from butter.audit.numeric_traceability import boilerplate_blocks
from butter.check.lexicon import puffery_hits
from butter.util import norm_text

MAX_SPECS = 20
PAGE_TEXT_LIMIT = 60_000  # 给检查器对照用的原页文字,不送给模型


def _first(field) -> str | None:
    return field.values[0].value if field.values else None


def _product_facts(p: ProductFacts, add) -> None:
    for label, field in (("product_name", p.name), ("brand", p.brand), ("sku", p.sku), ("category", p.category),
                         ("availability", p.availability), ("product_description", p.description)):
        for v in field.values[:1]:
            add(label, v.value, v.source_url)
    price, currency = _first(p.price), _first(p.currency)
    if price:
        add("price", f"{price} {currency}" if currency else price, p.price.values[0].source_url)
    for spec in p.specs[:MAX_SPECS]:
        add(f"spec {spec.name}", spec.value, spec.source_url)
    for v in p.comparison_claims.values:
        add("sourced_comparison", v.value, v.source_url)


def _page_order(page: PageFacts, has_product: bool) -> int:
    return 0 if page.role == "home" else 1 if page.role == "given" else 2 if has_product else 3


def build_tasks(
    fb: FactBase, questions_by_page: dict[str, list[Question]], *, max_pages: int = 10,
    limits: DraftLimits = DraftLimits(),
) -> tuple[list[DraftTask], list[str]]:
    """返回(任务, 没起草的页面与原因)。"""
    products_by_page: dict[str, list[ProductFacts]] = {}
    for product in fb.products:
        products_by_page.setdefault(product.page_url, []).append(product)
    pages = sorted(fb.pages, key=lambda p: _page_order(p, p.url in products_by_page))
    tasks: list[DraftTask] = []
    skipped: list[str] = []
    for page in pages:
        if len(tasks) >= max_pages:
            skipped.append(f"{page.url}:超过了起草的页数上限({max_pages} 页)")
            continue
        facts: list[DraftFact] = []

        def add(label: str, value: str, source_url: str) -> None:
            value = (value or "").strip()
            if value and not any(f.value == f"{label}: {value}" for f in facts):
                facts.append(DraftFact(fact_id=f"f{len(facts) + 1}", value=f"{label}: {value}", source_url=source_url))

        for label, field in (("site_name", fb.entity.name), ("site_category", fb.entity.category),
                             ("site_description", fb.entity.description)):
            for v in field.values[:1]:
                add(label, v.value, v.source_url)
        products = products_by_page.get(page.url, [])
        for product in products[:3]:
            _product_facts(product, add)
        matched = questions_by_page.get(page.url, [])
        # 优先回应不带「最好、推荐」这类词的题;只有这类题对上时才用它(模型照样不许写夸大词)
        matched = sorted(matched, key=lambda q: bool(puffery_hits(q.text, for_title=True)))
        has_page_facts = bool(products) or page.role in ("home", "given") or bool(matched)
        if not facts or not has_page_facts:
            skipped.append(f"{page.url}:这页没有可以用来起草的事实,也没有对上的题")
            continue
        fields: list[str] = ["meta_description"]
        if any(_first(p.price) or p.specs for p in products):
            fields.append("fact_block_lead")
        if matched:
            fields += ["answer_first", "title", "h1"]
        current = {"title": page.title or "", "h1": " / ".join(page.h1), "h2": " / ".join(page.h2[:8]),
                   "meta_description": page.meta_description or ""}
        tasks.append(DraftTask(
            task_id=f"t{len(tasks) + 1}", page_url=page.url, lang=page.lang or "", fields=fields, current=current,
            question_ids=[q.id for q in matched[:1]], questions={q.id: q.text for q in matched[:1]}, facts=facts,
            limits=limits))
    return tasks, skipped


def build_contexts(
    fb: FactBase, tasks: list[DraftTask], extracts: list[PageExtract], brands: PotatoBrands | None,
) -> dict[str, CheckContext]:
    """每个任务一份检查用的对照(键是 task_id)。竞品名来自 Potato 品牌表里不是本站的那些。

    「原页面的文字」只算这一页自己的内容。在多个页面上原样重复的文字块(页头、页脚、弹窗)不算:
    导航里一句「90 天无风险试用」,不能让草稿里随便一个 90 都过关(2026-11-17 真实起草时看到的口子)。
    认法和审计「数值可溯源」用的是同一个(boilerplate_blocks):3 页以上都有就算;只读到两页时两页都有才算;
    只读到一页时分不出来,整页都算。
    """
    boilerplate = boilerplate_blocks(extracts)
    text_by_url = {e.url: "\n".join(b.text for b in e.blocks if norm_text(b.text) not in boilerplate) for e in extracts}
    own = [v.value for f in (fb.entity.name, fb.entity.aliases) for v in f.values]
    own_norm = {norm_text(n) for n in own}
    competitors: list[str] = []
    for brand in (brands.brands if brands else []):
        names = [brand.name, *brand.aliases]
        if brand.is_focal or any(norm_text(n) in own_norm for n in names):
            continue
        competitors += [n for n in names if n.strip()]
    products_by_page: dict[str, list[ProductFacts]] = {}
    for product in fb.products:
        products_by_page.setdefault(product.page_url, []).append(product)
    out: dict[str, CheckContext] = {}
    for task in tasks:
        products = products_by_page.get(task.page_url, [])
        names = list(own)
        dates, claims = [], []
        for p in products:
            names += [v.value for f in (p.name, p.brand, p.sku) for v in f.values] + [s.value for s in p.specs]
            dates += [v.value for v in p.updated_date.values]
            claims += [v.value for v in p.comparison_claims.values]
        page_text = "\n".join([*task.current.values(), *task.questions.values(),
                               text_by_url.get(task.page_url, "")[:PAGE_TEXT_LIMIT]])
        out[task.task_id] = CheckContext(
            page_url=task.page_url, page_lang=task.lang or None, page_text=page_text, facts=task.facts,
            own_names=names, competitor_names=competitors, trusted_dates=dates, sourced_claims=claims,
            limits=task.limits)
    return out
