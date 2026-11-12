"""数据契约:所有跨模块传递的数据形状只在这里定义一次。

三个对外契约是 FactBase(事实库)、AuditFinding / AuditReport(审计)、ChangePackage(改动包清单);
其余是它们的零件和中间产物(站点快照、页面抽取结果、兄弟模块文件的只读形状)。
全部不可变、未知字段拒收;改这里 = 改所有下游。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── 站点快照(fetch 的产物;抓到的内容一律是不可信数据) ─────────────────

PageRole = Literal["given", "home", "discovered"]
StopReason = Literal["completed", "challenge", "refused", "time_budget", "off_host", "error"]
RobotsState = Literal["ok", "missing", "unreadable"]


class FetchedPage(Frozen):
    url: str  # 请求的地址
    final_url: str  # 同主机跳转后的地址
    role: PageRole
    status: int | None = None  # None = 没连上
    content_type: str = ""
    headers: dict[str, str] = {}  # 只留审计要用的几个头,键小写
    html: str = ""
    truncated: bool = False
    error: str | None = None
    fetched_at: datetime


class RobotsFetch(Frozen):
    url: str
    state: RobotsState
    status: int | None = None
    text: str = ""
    note: str | None = None


class SitemapEntry(Frozen):
    loc: str
    lastmod: str | None = None
    source_url: str  # 读自哪个 sitemap 文件


class SiteSnapshot(Frozen):
    schema_version: Literal["snapshot/1"] = "snapshot/1"
    site_url: str  # 用户给的网址(补全协议后)
    host: str
    origin: str  # scheme://host
    fetched_at: datetime
    user_agent: str
    pages: list[FetchedPage] = []
    robots: RobotsFetch
    sitemap_urls: list[str] = []  # 实际读到的 sitemap 文件
    sitemap_entries: list[SitemapEntry] = []
    stop_reason: StopReason = "completed"
    notes: list[str] = []  # 跳过了什么、停在哪、为什么


# ── 页面抽取结果(extract 的产物) ────────────────────────────────────


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


class PageExtract(Frozen):
    url: str
    role: PageRole
    title: str | None = None
    lang: str | None = None
    canonical: str | None = None
    metas: list[MetaTag] = []
    links: list[Link] = []
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


# ── 兄弟模块文件的只读形状(sources 的产物) ──────────────────────────


class PotatoBrand(Frozen):
    name: str
    aliases: list[str] = []
    domains: list[str] = []
    is_focal: bool = False


class PotatoBrands(Frozen):
    path: str
    focal_domain: str | None = None
    brands: list[PotatoBrand] = []


class Question(Frozen):
    id: str
    intent: str = ""
    text: str


class QuestionBoard(Frozen):
    path: str
    prompt_version: str | None = None
    locale: str | None = None
    questions: list[Question] = []


class ToastBrandDomains(Frozen):
    path: str
    brand_name: str | None = None
    brand_aliases: list[str] = []
    primary: str | None = None
    confirmed_domains: list[str] = []
    unconfirmed_domains: list[str] = []
    official_channels: list[str] = []  # 「主机名 + 路径前缀」


# ── 事实库 ───────────────────────────────────────────────────────────

SourceKind = Literal[
    "jsonld", "microdata", "meta", "og", "title", "link", "sitemap", "url", "potato_brands", "toast_brand_domains",
]


class FactValue(Frozen):
    value: str  # 原样保存,不改写数字格式
    source_url: str  # 读自哪个页面,或哪个本机文件
    source_kind: SourceKind
    source_path: str = ""  # 如 Organization.name、meta[name=description]


class FactField(Frozen):
    key: str
    values: list[FactValue] = []  # 采用的值;空 = 空缺
    alternatives: list[FactValue] = []  # 同字段的其他说法(没采用)
    candidates: list[FactValue] = []  # 只提示、不采用(如只在内页出现的平台链接)
    conflict: bool = False
    conflict_note: str | None = None


class SpecFact(Frozen):
    name: str
    value: str
    source_url: str
    source_kind: SourceKind
    source_path: str = ""


class EntityFacts(Frozen):
    name: FactField
    aliases: FactField
    category: FactField
    official_domain: FactField
    same_as: FactField
    description: FactField
    logo: FactField


class ProductFacts(Frozen):
    product_id: str
    page_url: str
    name: FactField
    description: FactField
    brand: FactField
    sku: FactField
    category: FactField
    price: FactField
    currency: FactField
    availability: FactField
    specs: list[SpecFact] = []
    comparison_claims: FactField
    updated_date: FactField
    author: FactField


class PageFacts(Frozen):
    url: str
    role: PageRole
    title: str | None = None
    h1: list[str] = []
    h2: list[str] = []
    meta_description: str | None = None
    lang: str | None = None
    dates: list[RawDate] = []


class Gap(Frozen):
    code: str
    message: str


class MergedInput(Frozen):
    kind: Literal["potato_brands", "toast_brand_domains"]
    path: str
    status: Literal["merged", "skipped"]
    note: str = ""


class FactBase(Frozen):
    schema_version: Literal["factbase/1"] = "factbase/1"
    site_url: str
    host: str
    run_id: str
    generated_at: datetime
    entity: EntityFacts
    products: list[ProductFacts] = []
    pages: list[PageFacts] = []
    gaps: list[Gap] = []
    merged_inputs: list[MergedInput] = []


# ── 审计 ─────────────────────────────────────────────────────────────

CheckName = Literal["meta_robots", "visible_date", "numeric_traceability", "hidden_text", "jsonld_consistency"]
FindingLevel = Literal["problem", "notice", "pass", "not_checkable"]


class AuditFinding(Frozen):
    finding_id: str
    check: CheckName
    entry_id: str  # 证据注册表条目 ID
    level: FindingLevel
    page_url: str | None = None  # None = 全站层面
    message: str
    evidence: dict[str, str] = {}  # 位置、原文片段、看到的值、对照值


class AuditReport(Frozen):
    schema_version: Literal["audit/1"] = "audit/1"
    site_url: str
    run_id: str
    generated_at: datetime
    pages_checked: list[str] = []
    findings: list[AuditFinding] = []
    limitations: list[str] = []


# ── 改动包 ───────────────────────────────────────────────────────────

ChangeKind = Literal["robots", "jsonld_organization", "jsonld_product", "fact_page", "fact_block", "heading_suggestion"]
ChangeAction = Literal["add", "replace", "none", "review"]
# 本窗口内任何方法的验证态最高为「假设」;技术修复类只说官方文档要求。不得出现「有效」。
ValidationState = Literal["官方文档要求,必要非充分", "假设"]


class FactRef(Frozen):
    field: str  # 如 entity.name、products[acme-kettle].price
    value: str
    source_url: str


class ChangeItem(Frozen):
    item_id: str
    kind: ChangeKind
    entry_id: str
    action: ChangeAction
    target: str  # 放到站上哪里
    files: list[str] = []  # 改动包内的相对路径
    rationale: str
    facts_used: list[FactRef] = []
    validation_state: ValidationState
    needs_human: list[str] = []  # 需要站长自己判断的事


class NotGenerated(Frozen):
    what: str
    reason: str


class ChangePackage(Frozen):
    schema_version: Literal["changepkg/1"] = "changepkg/1"
    site_url: str
    run_id: str
    generated_at: datetime
    items: list[ChangeItem] = []
    not_generated: list[NotGenerated] = []


class PackageBundle(Frozen):
    """生成器的全部输出:清单 + 文件内容(相对路径 → 文本)。由 store 落盘。"""

    package: ChangePackage
    files: dict[str, str] = {}


# ── UA 表(data/ 下的配置数据) ───────────────────────────────────────

UaPurpose = Literal["search_index", "training", "user_fetch", "ai_uses_token"]


class UaEntry(Frozen):
    vendor: str
    name: str
    robots_token: str
    user_agent: str | None = None  # 官方文档给出的 UA 字符串;没给就是 None
    # verbatim 当日整段照抄 / stitched 分段取回后拼接、未逐字对照 / short_form 只确认了不带括号的短串 /
    # not_given 文档没给完整串 / unverified 出自旧文档、是否现行未核
    user_agent_checked: Literal["verbatim", "stitched", "short_form", "not_given", "unverified"]
    purpose: UaPurpose
    robots_stance: str  # 官方对「是否遵守 robots.txt」的说法(转述)
    docs_url: str
    checked_on: str  # YYYY-MM-DD
    checked_how: str  # 当日打开 / 沿用哪次调研
    note: str = ""


class ExcludedVendor(Frozen):
    vendor: str
    products: str = ""
    reason: str
    looked_at: str = ""  # 查过哪些地方
    checked_on: str
    source: str  # 依据哪份调研记录


class Platform(Frozen):
    name: str
    kind: str
    hosts: list[str]
