"""事实库(对外契约 factbase/1)。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from butter.contracts.common import Frozen, PageRole
from butter.contracts.extract import RawDate

SourceKind = Literal[
    "jsonld", "microdata", "meta", "og", "title", "link", "sitemap", "url", "potato_brands", "toast_brand_domains",
    "owner_edit",  # 站长在 Butter 里改的:优先级最高(设计 §5.2 FactEdit)
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


class FactEdit(Frozen):
    """站长改的一个事实(factedit/1)。只追加进 events/facts.jsonl;同一字段以最后一次为准。"""

    schema_version: Literal["factedit/1"] = "factedit/1"
    site_host: str
    field: str        # 如 entity.name、products[acme-kettle].price
    value: str        # 空 = 这个值不要用
    note: str = ""
    at: str           # ISO 时间
