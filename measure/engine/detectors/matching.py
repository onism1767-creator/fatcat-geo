"""判定器共享的匹配原语:域名归属与品牌别名定位。

规则变更影响所有使用方的判定结果,故任何行为改动都要求相关判定器
提升 detector_version 并全量重算——改这里之前先想清楚。
"""

from __future__ import annotations

from engine.models import BrandSpec


def normalize_domain(host: str) -> str:
    return host.lower().removeprefix("www.")


def domain_matches(candidate: str, brand_domain: str) -> bool:
    """相等或为品牌域名的子域(完整域名段)。
    "acme.com.evil.net" 不命中 "acme.com"。"""
    cand, brand = normalize_domain(candidate), normalize_domain(brand_domain)
    return cand == brand or cand.endswith("." + brand)


def matches_any_brand_domain(candidate: str, brand: BrandSpec) -> bool:
    return any(domain_matches(candidate, domain) for domain in brand.domains)


def find_first_alias(text: str, brand: BrandSpec) -> tuple[int, str] | None:
    """在文本中定位最早出现的品牌名/别名。返回 (字符偏移, 命中别名) 或 None。
    用 lower() 而非 casefold():保证偏移与原文一一对应。"""
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for alias in dict.fromkeys([brand.name, *brand.aliases]):
        needle = alias.strip().lower()
        if not needle:
            continue
        pos = lowered.find(needle)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, alias)
    return best
