"""把一个页面的原始 HTML 变成 PageExtract。网页内容是不可信数据:这里只做规则抽取,不执行任何东西。

不执行脚本、不读外链样式表:原始 HTML 里没有的内容看不到,只靠外链 CSS 隐藏的文字也认不出来。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

from butter.contracts import (
    AttrText, FetchedPage, HiddenText, JsonLdBlock, Link, MetaTag, PageExtract, RawDate, SitemapEntry,
    StructuredNode, TextBlock,
)
from butter.extract import dates as dates_mod
from butter.extract.jsonld import clean_type, nodes_from_data, parse_block, text_of
from butter.util import collapse_ws, page_key, safe_http_url

MAX_PARSE_CHARS = 2_000_000
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head", "svg", "iframe", "object", "canvas"})
_BLOCK_TAGS = frozenset({
    "p", "li", "td", "th", "dd", "dt", "blockquote", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "div",
    "section", "article", "aside", "header", "footer", "main", "nav", "caption", "summary", "details", "address",
    "pre", "label", "body",
})
_MIN_ATTR_TEXT = 20

# (手法类别, 正则, 说明)。ui_state = 界面状态(菜单、弹窗、动画初始态);suspicious = 典型的藏字手法。
_STYLE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("ui_state", re.compile(r"display\s*:\s*none"), "display:none"),
    ("ui_state", re.compile(r"visibility\s*:\s*hidden"), "visibility:hidden"),
    ("ui_state", re.compile(r"(?<![\w-])opacity\s*:\s*0(?:\.0+)?\s*(?:;|$|!)"), "opacity:0"),
    ("suspicious", re.compile(r"text-indent\s*:\s*-\d{3,}"), "text-indent 负值移出可视区"),
    ("suspicious", re.compile(r"(?<![\w-])(?:left|top|margin-left|margin-top)\s*:\s*-\d{4,}px"), "定位移出屏幕"),
    ("suspicious", re.compile(r"clip\s*:\s*rect\(\s*0|clip-path\s*:\s*inset\(\s*(?:100%|50%)"), "clip 裁成不可见"),
]
_FONT_ZERO = re.compile(r"font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|$|!)")
_COLOR_TRANSPARENT = re.compile(r"(?<![\w-])color\s*:\s*transparent")
_BG_CLIP_TEXT = re.compile(r"background-clip\s*:\s*text")
_ZERO_BOX = re.compile(r"(?<![\w-])(?:height|width|max-height|max-width)\s*:\s*0(?:px)?\s*(?:;|$|!)")
_OVERFLOW_HIDDEN = re.compile(r"overflow(?:-[xy])?\s*:\s*hidden")
_COLOR = re.compile(r"(?<![\w-])color\s*:\s*([^;!]+)")
_BACKGROUND = re.compile(r"background(?:-color)?\s*:\s*([^;!]+)")
_SIMPLE_SELECTOR = re.compile(r"^[.#][\w-]+$")


def _style_verdict(style: str) -> tuple[str, str] | None:
    """一段样式声明是否把元素藏起来。返回 (手法类别, 说明)。"""
    style = style.lower()
    for technique, pattern, label in _STYLE_RULES:
        if pattern.search(style):
            return technique, label
    if _COLOR_TRANSPARENT.search(style) and not _BG_CLIP_TEXT.search(style):
        return "suspicious", "color:transparent"
    if _ZERO_BOX.search(style) and _OVERFLOW_HIDDEN.search(style):
        return "suspicious", "尺寸为 0 且 overflow:hidden"
    color, background = _COLOR.search(style), _BACKGROUND.search(style)
    if color and background and color.group(1).strip() == background.group(1).strip():
        return "suspicious", "文字颜色与背景色相同"
    return None


def _parse_style_blocks(css: str) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """只认顶层的单个类 / id 选择器;@media 之类的块整个跳过(那里的隐藏是有条件的)。
    返回 (选择器 → 隐藏判定, font-size:0 的选择器)。"""
    hidden: dict[str, tuple[str, str]] = {}
    font_zero: set[str] = set()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace == -1:
            break
        selector_text = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        body = css[brace + 1:j - 1]
        i = j
        if selector_text.startswith("@") or "{" in body:
            continue
        verdict = _style_verdict(body)
        is_font_zero = bool(_FONT_ZERO.search(body.lower()))
        if not verdict and not is_font_zero:
            continue
        for selector in (s.strip() for s in selector_text.split(",")):
            if _SIMPLE_SELECTOR.match(selector):
                if verdict:
                    hidden[selector] = (verdict[0], f"页内样式 {selector} {{{verdict[1]}}}")
                if is_font_zero:
                    font_zero.add(selector)
    return hidden, font_zero


def _selectors_of(el: Tag) -> list[str]:
    out = [f".{c}" for c in (el.get("class") or [])]
    if el.get("id"):
        out.append(f"#{el.get('id')}")
    return out


def _location(el: Tag) -> str:
    loc = el.name
    if el.get("id"):
        loc += f"#{el.get('id')}"
    classes = el.get("class") or []
    if classes:
        loc += "." + ".".join(classes[:2])
    return loc[:80]


class _Dom:
    """一次遍历里要反复问的两件事:这个元素在不在隐藏区、它归哪个文字块。带缓存。"""

    def __init__(self, soup: BeautifulSoup) -> None:
        css = "\n".join(tag.get_text() for tag in soup.find_all("style"))
        self.css_hidden, self.css_font_zero = _parse_style_blocks(css)
        self._hidden_cache: dict[int, tuple[Tag, str, str] | None] = {}
        self._skip_cache: dict[int, bool] = {}

    def own_verdict(self, el: Tag) -> tuple[str, str] | None:
        if el.has_attr("hidden"):
            return "ui_state", "hidden 属性"
        style = el.get("style")
        if isinstance(style, str) and style:
            if verdict := _style_verdict(style):
                return verdict[0], f"行内样式 {verdict[1]}"
        for selector in _selectors_of(el):
            if selector in self.css_hidden:
                return self.css_hidden[selector]
        return None

    def font_zero(self, el: Tag) -> bool:
        style = el.get("style")
        if isinstance(style, str) and _FONT_ZERO.search(style.lower()):
            return True
        return any(s in self.css_font_zero for s in _selectors_of(el))

    def hidden_root(self, el: Tag | None) -> tuple[Tag, str, str] | None:
        """el 所在的最外层隐藏元素及原因;不在隐藏区返回 None。"""
        if el is None or not isinstance(el, Tag):
            return None
        key = id(el)
        if key not in self._hidden_cache:
            outer = self.hidden_root(el.parent) if isinstance(el.parent, Tag) else None
            if outer is None and (verdict := self.own_verdict(el)):
                outer = (el, verdict[0], verdict[1])
            self._hidden_cache[key] = outer
        return self._hidden_cache[key]

    def skipped(self, el: Tag | None) -> bool:
        """在 script / style / head 这类不算正文的标签里。"""
        if el is None or not isinstance(el, Tag):
            return False
        key = id(el)
        if key not in self._skip_cache:
            self._skip_cache[key] = el.name in _SKIP_TAGS or self.skipped(el.parent)
        return self._skip_cache[key]

    @staticmethod
    def block_of(el: Tag) -> Tag:
        node: Tag | None = el
        while node is not None and node.name not in _BLOCK_TAGS:
            node = node.parent if isinstance(node.parent, Tag) else None
        return node if node is not None else el


def _attr_str(el: Tag, name: str) -> str:
    value = el.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _metas(soup: BeautifulSoup) -> list[MetaTag]:
    out: list[MetaTag] = []
    for tag in soup.find_all("meta"):
        content = _attr_str(tag, "content").strip()
        if not content:
            continue
        for attr in ("name", "property", "http-equiv", "itemprop"):
            key = _attr_str(tag, attr).strip().lower()
            if key:
                out.append(MetaTag(key=key, content=content, attr=attr))  # type: ignore[arg-type]
    return out


def _microdata_items(soup: BeautifulSoup, base_url: str) -> list[dict]:
    def value_of(el: Tag) -> str:
        name = el.name
        if name == "meta":
            return _attr_str(el, "content").strip()
        if name in ("a", "link", "area"):
            return safe_http_url(_attr_str(el, "href"), base_url) or _attr_str(el, "href").strip()
        if name in ("img", "audio", "video", "source", "embed", "iframe"):
            return safe_http_url(_attr_str(el, "src"), base_url) or ""
        if name == "time":
            return _attr_str(el, "datetime").strip() or collapse_ws(el.get_text(" "))
        if name in ("data", "meter"):
            return _attr_str(el, "value").strip() or collapse_ws(el.get_text(" "))
        return collapse_ws(el.get_text(" "))

    def item_of(el: Tag, depth: int) -> dict:
        props: dict[str, list] = {}

        def visit(node: Tag) -> None:
            for child in node.find_all(True, recursive=False):
                if child.has_attr("itemprop"):
                    nested = child.has_attr("itemscope")
                    value = item_of(child, depth + 1) if nested and depth < 6 else value_of(child)
                    for prop in _attr_str(child, "itemprop").split():
                        props.setdefault(prop, []).append(value)
                    if not nested:
                        visit(child)
                elif not child.has_attr("itemscope"):
                    visit(child)

        visit(el)
        item: dict = {"@type": [clean_type(t) for t in _attr_str(el, "itemtype").split()]}
        for key, values in props.items():
            item[key] = values[0] if len(values) == 1 else values
        return item

    tops = [el for el in soup.find_all(attrs={"itemscope": True}) if not el.has_attr("itemprop")]
    return [item_of(el, 0) for el in tops[:50]]


_TIME_MODIFIED = re.compile(r"modif|updat", re.IGNORECASE)
_TIME_PUBLISHED = re.compile(r"publish|post|creat", re.IGNORECASE)


def _time_label(el: Tag) -> str:
    hint = " ".join([_attr_str(el, "itemprop"), _attr_str(el, "class"), _attr_str(el, "id")])
    if _TIME_MODIFIED.search(hint):
        return "modified"
    if _TIME_PUBLISHED.search(hint):
        return "published"
    return "unknown"


_META_DATE_KEYS = {
    "article:published_time": ("meta_published", "published"),
    "article:modified_time": ("meta_modified", "modified"),
    "og:updated_time": ("og_updated", "modified"),
    "datepublished": ("meta_published", "published"),
    "datemodified": ("meta_modified", "modified"),
    "last-modified": ("meta_modified", "modified"),
    "pubdate": ("meta_published", "published"),
    "publishdate": ("meta_published", "published"),
    "date": ("meta_published", "unknown"),
}


def _collect_dates(
    url: str, metas: list[MetaTag], nodes: list[StructuredNode], visible_text: str,
    time_elements: list[tuple[str, str]], last_modified: str, sitemap_entry: SitemapEntry | None,
) -> list[RawDate]:
    out: list[RawDate] = []

    def add(kind: str, raw: str, label: str, path: str, source_url: str = url) -> None:
        raw = raw.strip()
        if raw:
            out.append(RawDate(kind=kind, raw=raw, iso_date=dates_mod.to_iso_date(raw), label=label,  # type: ignore[arg-type]
                               source_url=source_url, source_path=path))

    for node in nodes:
        type_name = node.types[0] if node.types else "?"
        for prop, kind, label in (("datePublished", "jsonld_published", "published"),
                                  ("dateModified", "jsonld_modified", "modified")):
            if (value := text_of(node.props.get(prop))) is not None:
                add(kind, value, label, f"{node.source_kind}:{type_name}.{prop}")
    for meta in metas:
        if meta.attr != "itemprop" and meta.key in _META_DATE_KEYS:
            kind, label = _META_DATE_KEYS[meta.key]
            add(kind, meta.content, label, f"meta[{meta.attr}={meta.key}]")
    for raw, label in time_elements:
        add("time_element", raw, label, "time")
    for fragment, date_raw, label in dates_mod.find_labeled_dates(visible_text):
        add("visible", date_raw, label, f"正文「{collapse_ws(fragment)[:60]}」")
    if last_modified:
        add("http_last_modified", last_modified, "modified", "HTTP Last-Modified")
    if sitemap_entry is not None and sitemap_entry.lastmod:
        add("sitemap_lastmod", sitemap_entry.lastmod, "modified", "sitemap lastmod", sitemap_entry.source_url)
    return out


def extract_page(page: FetchedPage, sitemap_by_key: dict[str, SitemapEntry] | None = None) -> PageExtract:
    url = page.final_url or page.url
    html = page.html
    truncated = len(html) > MAX_PARSE_CHARS
    soup = BeautifulSoup(html[:MAX_PARSE_CHARS], "lxml")
    dom = _Dom(soup)

    title_tag = (soup.head.find("title") if soup.head else None) or soup.find("title")
    title = collapse_ws(title_tag.get_text(" ")) if title_tag else None
    html_tag = soup.find("html")
    lang = _attr_str(html_tag, "lang").strip() or None if isinstance(html_tag, Tag) else None
    metas = _metas(soup)

    canonical = None
    for link in soup.find_all("link", href=True):
        if "canonical" in _attr_str(link, "rel").lower().split():
            canonical = safe_http_url(_attr_str(link, "href"), url)
            break

    links: list[Link] = []
    seen_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = safe_http_url(_attr_str(a, "href"), url)
        if href and href not in seen_links:
            seen_links.add(href)
            links.append(Link(href=href, text=collapse_ws(a.get_text(" "))[:120], rel=_attr_str(a, "rel").lower()))

    blocks_raw: list[JsonLdBlock] = []
    for script in soup.find_all("script"):
        if _attr_str(script, "type").split(";")[0].strip().lower() == "application/ld+json":
            blocks_raw.append(parse_block(script.string or script.get_text(), len(blocks_raw)))
    nodes: list[StructuredNode] = []
    for block in blocks_raw:
        if block.data is not None:
            nodes.extend(nodes_from_data(block.data, "jsonld", f"jsonld[{block.index}]"))
    for i, item in enumerate(_microdata_items(soup, url)):
        nodes.extend(nodes_from_data(item, "microdata", f"microdata[{i}]"))

    # 文字:按「是否隐藏」分流,并按所在块聚合
    root = soup  # 整个文档都看:注释可能写在 <body> 外面;<head> 里的文字由 skipped() 挡掉
    block_order: list[int] = []
    block_text: dict[int, list[str]] = {}
    block_el: dict[int, Tag] = {}
    hidden_parts: dict[int, tuple[Tag, str, str, list[str]]] = {}
    attr_texts: list[AttrText] = []
    for node in root.find_all(string=True):
        parent = node.parent
        if not isinstance(parent, Tag):
            continue
        if isinstance(node, Comment):
            text = collapse_ws(str(node))
            if len(text) >= _MIN_ATTR_TEXT:
                attr_texts.append(AttrText(carrier="comment", text=text[:2000]))
            continue
        if type(node) is not NavigableString or dom.skipped(parent):
            continue
        text = collapse_ws(str(node))
        if not text:
            continue
        outer = dom.hidden_root(parent)
        if outer is None and dom.font_zero(parent):
            # font-size:0 常用于消除行内块间隙,子元素会自己重设字号:只把它直接包着的文字算作隐藏
            outer = (parent, "suspicious", "font-size:0(只计它直接包着的文字)")
        if outer is not None:
            el, technique, reason = outer
            hidden_parts.setdefault(id(el), (el, technique, reason, []))[3].append(text)
            continue
        block = dom.block_of(parent)
        if id(block) not in block_text:
            block_order.append(id(block))
            block_text[id(block)] = []
            block_el[id(block)] = block
        block_text[id(block)].append(text)

    blocks: list[TextBlock] = []
    for key in block_order:
        el = block_el[key]
        has_cite = el.find(["cite", "sup"]) is not None or bool(
            re.search(r"footnote|reference|citation|source", _attr_str(el, "class"), re.IGNORECASE))
        blocks.append(TextBlock(text=" ".join(block_text[key]), has_link=el.find("a", href=True) is not None,
                                has_cite=has_cite, tag=el.name))
    visible_text = "\n".join(b.text for b in blocks)
    hidden_texts = [
        HiddenText(text=" ".join(parts)[:4000], reason=reason, technique=technique, location=_location(el))  # type: ignore[arg-type]
        for el, technique, reason, parts in hidden_parts.values()
    ]

    def visible(el: Tag) -> bool:
        return not dom.skipped(el) and dom.hidden_root(el) is None

    h1 = [t for el in soup.find_all("h1") if visible(el) and (t := collapse_ws(el.get_text(" ")))]
    h2 = [t for el in soup.find_all("h2") if visible(el) and (t := collapse_ws(el.get_text(" ")))]

    for el in soup.find_all(True):
        for attr in ("alt", "title", "aria-label"):
            text = collapse_ws(_attr_str(el, attr))
            if len(text) >= _MIN_ATTR_TEXT:
                attr_texts.append(AttrText(carrier=f"{el.name}[{attr}]", text=text[:2000]))
    for meta in metas:
        if meta.key in ("description", "keywords", "og:description", "twitter:description"):
            attr_texts.append(AttrText(carrier=f"meta[{meta.attr}={meta.key}]", text=meta.content[:2000]))

    nosnippet_chars = 0
    for el in soup.find_all(attrs={"data-nosnippet": True}):
        if not any(isinstance(p, Tag) and p.has_attr("data-nosnippet") for p in el.parents):
            nosnippet_chars += len(collapse_ws(el.get_text(" ")))

    time_elements = [
        (_attr_str(el, "datetime").strip() or collapse_ws(el.get_text(" ")), _time_label(el))
        for el in soup.find_all("time") if visible(el)
    ]
    sitemap_entry = (sitemap_by_key or {}).get(page_key(url))
    dates = _collect_dates(url, metas, nodes, visible_text, time_elements,
                           page.headers.get("last-modified", ""), sitemap_entry)

    return PageExtract(
        url=url, role=page.role, title=title, lang=lang, canonical=canonical, metas=metas, links=links, h1=h1, h2=h2,
        jsonld_blocks=blocks_raw, nodes=nodes, visible_text=visible_text, blocks=blocks, hidden_texts=hidden_texts,
        attr_texts=attr_texts, data_nosnippet_chars=nosnippet_chars, dates=dates,
        x_robots_tag=page.headers.get("x-robots-tag", ""), parse_truncated=truncated,
    )


def extract_site(pages: list[FetchedPage], sitemap_entries: list[SitemapEntry]) -> list[PageExtract]:
    """只处理拿到了 HTML 的页面;sitemap 的 lastmod 按地址配到对应页面上。
    联网抓的要 2xx;本地导入的文件没有 HTTP 状态,有内容就处理。"""
    by_key = {page_key(e.loc): e for e in sitemap_entries if e.loc}
    return [extract_page(p, by_key) for p in pages
            if p.html and (p.local_file or (p.status is not None and 200 <= p.status < 300))]
