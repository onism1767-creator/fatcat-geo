"""结构化数据:解析 JSON-LD 块,把 JSON-LD 与 microdata 摊平成同一种节点。

数字按原样保留成字符串(1299.00 不会变成 1299.0):事实库要求原样保存,不改写数字格式。
解析失败不修补,如实记错:坏掉的 JSON-LD 是审计要报的问题。
"""

from __future__ import annotations

import json
import re
from typing import Any

from butter.contracts import JsonLdBlock, StructuredNode

_SCHEMA_PREFIX = re.compile(r"^(?:https?://schema\.org/|schema:)", re.IGNORECASE)
_WRAPPERS = re.compile(r"^\s*(?:<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)|(?:-->|//\s*\]\]>|\]\]>)\s*$")
MAX_NODES = 500
MAX_DEPTH = 12

ORG_TYPES = frozenset({
    "Organization", "Corporation", "LocalBusiness", "OnlineBusiness", "OnlineStore", "Store", "Brand", "NGO",
    "EducationalOrganization", "CollegeOrUniversity", "School", "GovernmentOrganization", "MedicalOrganization",
    "NewsMediaOrganization", "ProfessionalService", "Restaurant", "Hotel", "Airline", "Consortium",
    "FundingScheme", "LibrarySystem", "MusicGroup", "PerformingGroup", "Project", "ResearchOrganization",
    "SportsOrganization", "WorkersUnion", "FinancialService", "TravelAgency", "AutoDealer", "LegalService",
    "SoftwareCompany", "TechnologyCompany",
})
WEBSITE_TYPES = frozenset({"WebSite"})
PRODUCT_TYPES = frozenset({"Product", "ProductGroup", "IndividualProduct", "ProductModel", "Vehicle", "Car"})
ARTICLE_TYPES = frozenset({
    "Article", "NewsArticle", "BlogPosting", "TechArticle", "Report", "ScholarlyArticle", "AnalysisNewsArticle",
})


def clean_type(value: Any) -> str:
    return _SCHEMA_PREFIX.sub("", str(value)).strip()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def text_of(value: Any) -> str | None:
    """取一个属性的文字值:字符串 / 数字原样;对象取 @value 或 name;列表取第一个取得到的。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("@value", "name", "url", "@id"):
            if (inner := text_of(value.get(key))) is not None:
                return inner
        return None
    if isinstance(value, list):
        for item in value:
            if (inner := text_of(item)) is not None:
                return inner
    return None


def texts_of(value: Any) -> list[str]:
    return [t for item in as_list(value) if (t := text_of(item)) is not None]


def parse_block(raw: str, index: int) -> JsonLdBlock:
    cleaned = _WRAPPERS.sub("", raw.strip()).strip()
    cleaned = _WRAPPERS.sub("", cleaned).strip()
    if not cleaned:
        return JsonLdBlock(index=index, raw=raw, error="空的 JSON-LD 块")
    try:
        data = json.loads(cleaned, parse_float=str, parse_int=str)
    except (json.JSONDecodeError, RecursionError) as exc:
        return JsonLdBlock(index=index, raw=raw, error=f"JSON 解析失败:{exc}")
    return JsonLdBlock(index=index, raw=raw, data=data)


def nodes_from_data(data: Any, source_kind: str, base_path: str) -> list[StructuredNode]:
    """摊平:每个带 @type 的对象(含 @graph 里的、嵌套的)各成一个节点。"""
    nodes: list[StructuredNode] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH or len(nodes) >= MAX_NODES:
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]", depth + 1)
        elif isinstance(value, dict):
            types = [clean_type(t) for t in as_list(value.get("@type")) if isinstance(t, str)]
            if types:
                props = {k: v for k, v in value.items() if k not in ("@context", "@type")}
                nodes.append(StructuredNode(types=types, props=props, source_kind=source_kind, path=path))  # type: ignore[arg-type]
            for key, inner in value.items():
                if key != "@context" and isinstance(inner, (dict, list)):
                    walk(inner, f"{path}.{key}", depth + 1)

    walk(data, base_path, 0)
    return nodes


def has_type(node: StructuredNode, types: frozenset[str]) -> bool:
    return any(t in types for t in node.types)


def is_top_level(node: StructuredNode) -> bool:
    """是不是页面直接声明的节点(不是别的节点的属性值)。@graph 里的算顶层。"""
    tail = re.sub(r"^(?:jsonld|microdata)\[\d+\]", "", node.path)
    tail = re.sub(r"^(?:\[\d+\])?(?:\.@graph\[\d+\])?", "", tail)
    return tail == ""
