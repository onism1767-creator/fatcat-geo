"""检查五:JSON-LD(含 microdata)与正文一致。

依据(research/04 G8):结构化数据必须与可见内容一致。
比对的是原始 HTML 里的文字(含菜单、折叠区这类界面隐藏文字;不含用藏字手法隐藏的)。
脚本渲染的内容 Butter 看不到:可见文字太少时,和正文的比对记「查不了」,不下结论。
结构化数据自身写得对不对(解析失败、@context 不是 schema.org、@type 大小写、url 不是网址)不需要正文,照查。
(后三条是 2026-11-12 真实运行 yuwell-eu.com 时发现漏报后补的。)
"""

from __future__ import annotations

import re

from butter.audit.base import finding
from butter.contracts import AuditFinding, PageExtract, StructuredNode
from butter.extract.jsonld import ARTICLE_TYPES, ORG_TYPES, PRODUCT_TYPES, WEBSITE_TYPES, as_list, has_type, is_top_level, text_of
from butter.registry.ids import JSONLD_BODY_CONSISTENCY
from butter.util import host_of, norm_text, safe_http_url, same_site, snippet

CHECK = "jsonld_consistency"
_MIN_TEXT = 200
_THOUSANDS = re.compile(r"(?<=\d)[,,](?=\d{3}(?!\d))")


def number_in_text(number: str, text: str) -> bool:
    """这个数在文字里出现过吗。1299、1299.00、1,299 算同一个数。"""
    core = re.sub(r"[,,\s]", "", number)
    if not re.fullmatch(r"\d+(?:\.\d+)?", core):
        return norm_text(number) in norm_text(text)
    if "." in core:
        core = core.rstrip("0").rstrip(".")
    tail = r"0*" if "." in core else r"(?:\.0+)?"
    pattern = rf"(?<![\d.]){re.escape(core)}{tail}(?!\d|\.\d)"
    return re.search(pattern, _THOUSANDS.sub("", text)) is not None


def _label(node: StructuredNode) -> str:
    kind = "JSON-LD" if node.source_kind == "jsonld" else "microdata"
    return f"{kind} 的 {node.types[0]}"


_KNOWN_TYPES = ORG_TYPES | WEBSITE_TYPES | PRODUCT_TYPES | ARTICLE_TYPES
_BY_FOLDED = {t.casefold(): t for t in _KNOWN_TYPES}
_SCHEMA_ORG = re.compile(r"^https?://(?:www\.)?schema\.org/?$", re.IGNORECASE)


def _mentions_schema_org(context: object) -> bool:
    """@context 里有没有 schema.org:字符串、列表、带 @vocab 的对象都认。"""
    if isinstance(context, str):
        return bool(_SCHEMA_ORG.match(context.strip()))
    if isinstance(context, list):
        return any(_mentions_schema_org(item) for item in context)
    if isinstance(context, dict):
        return any(isinstance(v, str) and "schema.org" in v.lower() for v in context.values())
    return False


def _structure_findings(page: PageExtract, host: str) -> list[AuditFinding]:
    """只看结构化数据自身写得对不对,不需要正文:解析失败、@context、@type 的大小写、url 是不是网址。"""
    out: list[AuditFinding] = []
    for block in page.jsonld_blocks:
        where = f"jsonld[{block.index}]"
        if block.error:
            out.append(finding(
                CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                f"第 {block.index + 1} 个 JSON-LD 块解析失败,搜索引擎和 AI 读不了它。{block.error}",
                location=where, snippet=snippet(block.raw, 160)))
            continue
        for top in (block.data if isinstance(block.data, list) else [block.data]):
            if not isinstance(top, dict) or not ("@type" in top or "@graph" in top):
                continue
            context = top.get("@context")
            if context is None:
                out.append(finding(
                    CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                    f"第 {block.index + 1} 个 JSON-LD 块没有写 @context。没有它,解析器不知道这些字段按哪套词表读。",
                    location=f"{where}.@context", expected="https://schema.org"))
            elif not _mentions_schema_org(context):
                shown = context if isinstance(context, str) else str(context)
                out.append(finding(
                    CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                    f"第 {block.index + 1} 个 JSON-LD 块的 @context 写的是「{snippet(shown, 80)}」,不是 https://schema.org。"
                    "按 schema.org 读结构化数据的解析器不会认这一块。",
                    location=f"{where}.@context", observed=shown, expected="https://schema.org"))

    for node in page.nodes:
        if node.source_kind == "jsonld":
            for kind in node.types:
                proper = _BY_FOLDED.get(kind.casefold())
                if proper and proper != kind:
                    out.append(finding(
                        CHECK, JSONLD_BODY_CONSISTENCY, "notice", page.url,
                        f"JSON-LD 的 @type 写的是「{kind}」,schema.org 里的写法是「{proper}」(区分大小写),解析器可能不认。",
                        location=f"{node.path}.@type", observed=kind, expected=proper))
        types = frozenset(_BY_FOLDED.get(t.casefold(), t) for t in node.types)
        if types & (ORG_TYPES | WEBSITE_TYPES) and is_top_level(node):
            url = text_of(node.props.get("url"))
            if not url:
                continue
            safe = safe_http_url(url, page.url)
            if safe is None or "." not in host_of(safe):
                out.append(finding(
                    CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                    f"{_label(node)} 的 url 写的是「{snippet(url, 80)}」,不是一个可用的网址。",
                    location=f"{node.path}.url", observed=url, expected=f"https://{host}/"))
            elif not same_site(host_of(safe), host):
                out.append(finding(CHECK, JSONLD_BODY_CONSISTENCY, "notice", page.url,
                                   f"{_label(node)} 的 url 指向别的主机 {host_of(safe)},不是本站 {host}。",
                                   location=f"{node.path}.url", observed=url, expected=host))
    return out


def _check_node(node: StructuredNode, page: PageExtract, text: str, text_norm: str) -> list[AuditFinding]:
    """结构化数据里的名称、价格、评分、标题,在页面文字里找不找得到。"""
    out: list[AuditFinding] = []
    name = text_of(node.props.get("name"))

    if has_type(node, ORG_TYPES | WEBSITE_TYPES) and is_top_level(node):
        if name and norm_text(name) not in text_norm:
            out.append(finding(CHECK, JSONLD_BODY_CONSISTENCY, "notice", page.url,
                               f"{_label(node)} 写的名称「{snippet(name, 60)}」在本页文字和标题里找不到。",
                               location=f"{node.path}.name", observed=name))

    if has_type(node, PRODUCT_TYPES):
        if name and norm_text(name) not in text_norm:
            out.append(finding(CHECK, JSONLD_BODY_CONSISTENCY, "notice", page.url,
                               f"{_label(node)} 写的商品名「{snippet(name, 60)}」在本页文字里找不到。",
                               location=f"{node.path}.name", observed=name))
        for offer in as_list(node.props.get("offers")):
            if not isinstance(offer, dict):
                continue
            for prop in ("price", "lowPrice", "highPrice"):
                price = text_of(offer.get(prop))
                if price and not number_in_text(price, text):
                    out.append(finding(
                        CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                        f"{_label(node)} 写的价格 {price} 在本页文字里找不到。结构化数据里的价格应当与读者看到的一致。"
                        "(如果价格是脚本渲染出来的,这条可能是误报。)",
                        location=f"{node.path}.offers.{prop}", observed=price, expected="页面文字里出现同一个数"))
        rating = node.props.get("aggregateRating")
        if isinstance(rating, dict):
            for prop in ("ratingValue", "reviewCount", "ratingCount"):
                value = text_of(rating.get(prop))
                if value and not number_in_text(value, text):
                    out.append(finding(
                        CHECK, JSONLD_BODY_CONSISTENCY, "problem", page.url,
                        f"{_label(node)} 的评分数据 {prop}={value} 在本页文字里找不到。评分标记应当对应读者看得到的评价。"
                        "(如果评分是脚本渲染出来的,这条可能是误报。)",
                        location=f"{node.path}.aggregateRating.{prop}", observed=value, expected="页面文字里出现同一个数"))

    if has_type(node, ARTICLE_TYPES):
        headline = text_of(node.props.get("headline")) or name
        if headline:
            h = norm_text(headline)
            heads = [norm_text(t) for t in [page.title or "", *page.h1]]
            if h not in text_norm and not any(h in t or (t and t in h) for t in heads):
                out.append(finding(CHECK, JSONLD_BODY_CONSISTENCY, "notice", page.url,
                                   f"{_label(node)} 写的标题「{snippet(headline, 60)}」与本页的标题、H1 和正文都对不上。",
                                   location=f"{node.path}.headline", observed=headline,
                                   expected=snippet(" / ".join([page.title or "", *page.h1]), 120)))
    return out


def check(extracts: list[PageExtract], host: str) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for page in extracts:
        page_findings: list[AuditFinding] = _structure_findings(page, host)
        if not page.nodes:
            if not page_findings:
                page_findings.append(finding(CHECK, JSONLD_BODY_CONSISTENCY, "not_applicable", page.url,
                                             "本页原始 HTML 里没有结构化数据(JSON-LD / microdata),没有可比对的东西,这一项不适用。"))
            findings.extend(page_findings)
            continue
        if len(page.visible_text) < _MIN_TEXT:
            page_findings.append(finding(
                CHECK, JSONLD_BODY_CONSISTENCY, "not_checkable", page.url,
                f"本页有结构化数据,但原始 HTML 里的可见文字太少({len(page.visible_text)} 字),可能靠脚本渲染:"
                "名称、价格这些和正文对不对得上,比不了。"))
            findings.extend(page_findings)
            continue
        ui_hidden = " ".join(h.text for h in page.hidden_texts if h.technique == "ui_state")
        text = "\n".join([page.title or "", page.visible_text, ui_hidden])
        text_norm = norm_text(text)
        compared = 0
        for node in page.nodes:
            if has_type(node, ORG_TYPES | WEBSITE_TYPES | PRODUCT_TYPES | ARTICLE_TYPES):
                compared += 1
                page_findings.extend(_check_node(node, page, text, text_norm))
        if not page_findings:
            page_findings.append(finding(
                CHECK, JSONLD_BODY_CONSISTENCY, "pass", page.url,
                f"本页 {len(page.nodes)} 个结构化数据节点里比对了 {compared} 个(机构 / 网站 / 商品 / 文章的名称、价格、评分、标题),"
                "未见与页面文字不一致。"))
        findings.extend(page_findings)
    return findings
