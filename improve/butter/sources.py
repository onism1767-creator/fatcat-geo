"""只读:兄弟模块的文件(Potato 的 brands.yml / questions.yml、Toast 导出的 brand-domains.json)和 Butter 自带的数据表。

不 import aivis,不 import Toast 的任何模块:只把它们的文件当数据读,形状不对就如实报错,不猜。
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from butter.contracts import (
    ExcludedVendor, Platform, PotatoBrand, PotatoBrands, Question, QuestionBoard, ToastBrandDomains, UaEntry,
)


class SourceFileError(ValueError):
    """给的文件读不了或形状不对。"""


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourceFileError(f"读不了 {path}:{exc}") from exc


def _str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def load_brands_yml(path: str | Path) -> PotatoBrands:
    path = Path(path)
    data = _read_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get("peers"), list):
        raise SourceFileError(f"{path} 不像 Potato 的 brands.yml(缺 peers 列表)")
    brands = [
        PotatoBrand(name=str(p["name"]).strip(), aliases=_str_list(p.get("aliases")),
                    domains=_str_list(p.get("domains")), is_focal=bool(p.get("is_focal")))
        for p in data["peers"] if isinstance(p, dict) and p.get("name")
    ]
    focal = data.get("focal_domain")
    return PotatoBrands(path=str(path), focal_domain=str(focal).strip() if focal else None, brands=brands)


def load_questions_yml(path: str | Path) -> QuestionBoard:
    path = Path(path)
    data = _read_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise SourceFileError(f"{path} 不像 Potato 的 questions.yml(缺 questions 列表)")
    questions = [
        Question(id=str(q["id"]), intent=str(q.get("intent", "")), text=str(q["text"]).strip())
        for q in data["questions"] if isinstance(q, dict) and q.get("id") and q.get("text")
    ]
    version = data.get("prompt_version")
    locale = data.get("locale")
    return QuestionBoard(path=str(path), prompt_version=str(version) if version else None,
                         locale=str(locale) if locale else None, questions=questions)


def load_brand_domains_json(path: str | Path) -> ToastBrandDomains:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFileError(f"读不了 {path}:{exc}") from exc
    if not isinstance(data, dict) or "brand" not in data:
        raise SourceFileError(f"{path} 不像 Toast 导出的 brand-domains.json(缺 brand)")
    brand = data.get("brand") if isinstance(data.get("brand"), dict) else {}
    listing = data.get("list") if isinstance(data.get("list"), dict) else {}
    domains = [d for d in listing.get("domains", []) if isinstance(d, dict) and d.get("domain")]
    channels = [str(c["match"]) for c in listing.get("official_channels", []) if isinstance(c, dict) and c.get("match")]
    name = brand.get("name")
    primary = listing.get("primary") or next(iter(_str_list(brand.get("domains"))), None)
    return ToastBrandDomains(
        path=str(path), brand_name=str(name).strip() if name else None, brand_aliases=_str_list(brand.get("aliases")),
        primary=str(primary) if primary else None,
        confirmed_domains=[str(d["domain"]) for d in domains if d.get("status") == "confirmed"],
        unconfirmed_domains=[str(d["domain"]) for d in domains if d.get("status") == "unconfirmed"],
        official_channels=channels or _str_list(brand.get("official_channels")),
    )


# ── Butter 自带的数据表 ───────────────────────────────────────────────


def _data(name: str) -> Any:
    return yaml.safe_load(resources.files("butter").joinpath("data", name).read_text(encoding="utf-8"))


@cache
def load_ua_table() -> tuple[UaEntry, ...]:
    return tuple(UaEntry.model_validate(item) for item in _data("ua_table.yaml")["entries"])


@cache
def load_excluded_vendors() -> tuple[ExcludedVendor, ...]:
    return tuple(ExcludedVendor.model_validate(item) for item in _data("excluded_vendors.yaml")["vendors"])


@cache
def load_platforms() -> tuple[Platform, ...]:
    return tuple(Platform.model_validate(item) for item in _data("platforms.yaml")["platforms"])
