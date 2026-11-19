"""页面抽取结果(extract 的产物)。"""

from __future__ import annotations

from typing import Any, Literal

from butter.contracts.common import Frozen, PageRole


class MetaTag(Frozen):
    key: str  # name / property / http-equiv 的值,小写
    content: str
    attr: Literal["name", "property", "http-equiv", "itemprop"]


class Link(Frozen):
    href: str  # 绝对地址,只留 http / https
    text: str = ""
    rel: str = ""


class JsonLdBlock(Frozen):
    index: int
    raw: str
    data: Any = None
    error: str | None = None  # 解析失败的原因


class StructuredNode(Frozen):
    """一个结构化数据节点,JSON-LD 与 microdata 统一成同一形状。"""

    types: list[str]  # 去掉 schema.org 前缀
    props: dict[str, Any]
    source_kind: Literal["jsonld", "microdata"]
    path: str  # 如 jsonld[0] 或 jsonld[0].@graph[2].offers


class HiddenText(Frozen):
    text: str
    reason: str  # 判为隐藏的依据,如 "行内样式 font-size:0"
    technique: Literal["ui_state", "suspicious"]  # display:none / hidden 属性是界面状态;其余是典型藏字手法
    location: str  # 元素的粗略位置,如 div#promo.note


class TextBlock(Frozen):
    text: str
    has_link: bool = False
    has_cite: bool = False  # cite / sup / 脚注一类
    tag: str = ""


class AttrText(Frozen):
    carrier: str  # 如 img[alt]、meta[name=description]、comment
    text: str


class RawDate(Frozen):
    kind: Literal[
        "visible", "time_element", "jsonld_published", "jsonld_modified", "meta_published", "meta_modified",
        "og_updated", "sitemap_lastmod", "http_last_modified",
    ]
    raw: str
    iso_date: str | None = None  # YYYY-MM-DD;认不出就是 None
    label: Literal["published", "modified", "unknown"] = "unknown"
    source_url: str
    source_path: str = ""
    suspect: str = ""  # 非空 = 这个日期不可信的原因(如 sitemap lastmod 全站同一天);事实库不采用,审计不当作本页日期


class PageExtract(Frozen):
    url: str
    role: PageRole
    title: str | None = None
    lang: str | None = None
    canonical: str | None = None
    metas: list[MetaTag] = []
    links: list[Link] = []
    alternates: list[str] = []  # <link rel=alternate hreflang=…> 指向的语言 / 地区副本(绝对地址)
    h1: list[str] = []
    h2: list[str] = []
    jsonld_blocks: list[JsonLdBlock] = []
    nodes: list[StructuredNode] = []
    visible_text: str = ""
    blocks: list[TextBlock] = []
    hidden_texts: list[HiddenText] = []
    attr_texts: list[AttrText] = []  # 人眼看不到但机器读得到的文字:注释、alt、meta
    data_nosnippet_chars: int = 0
    dates: list[RawDate] = []
    x_robots_tag: str = ""
    parse_truncated: bool = False
