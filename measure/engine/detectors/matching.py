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


# 不以空格分词的文字(中文、日文假名、韩文、泰文等):这些字符两侧不查词边界,按子串匹配(产品文档 D18)。
_UNSPACED_RANGES = (
    (0x0E00, 0x0EFF),  # 泰文、老挝文
    (0x1000, 0x109F),  # 缅甸文
    (0x1100, 0x11FF),  # 韩文字母
    (0x1780, 0x17FF),  # 高棉文
    (0x2E80, 0x2FDF),  # CJK 部首补充、康熙部首
    (0x3040, 0x30FF),  # 日文平假名、片假名
    (0x3130, 0x318F),  # 韩文兼容字母
    (0x31F0, 0x31FF),  # 片假名语音扩展
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0xAC00, 0xD7AF),  # 韩文音节
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0xFF66, 0xFF9F),  # 半角片假名
    (0x20000, 0x3134F),  # CJK 扩展 B–G
)


def _is_spaced_script_char(ch: str) -> bool:
    """拉丁字母、数字这类「以空格分词」的字符:品牌名以它开头 / 结尾时,那一侧要查词边界,
    免得把 "Aurora" 认进 "Auroras"、把 "CRM" 认进 "CRMS"。"""
    if not ch.isalnum():
        return False
    code = ord(ch)
    return not any(lo <= code <= hi for lo, hi in _UNSPACED_RANGES)


def _bounded_at(lowered: str, start: int, needle: str) -> bool:
    """命中位置两侧是否成词边界。只对 needle 以「空格分词」字符开头 / 结尾的那一侧检查:
    "极光CRM" 后面接汉字(「极光CRM系统」)算命中,接拉丁字母(「极光CRMS」)不算。"""
    end = start + len(needle)
    if _is_spaced_script_char(needle[0]) and start > 0 and _is_spaced_script_char(lowered[start - 1]):
        return False
    if _is_spaced_script_char(needle[-1]) and end < len(lowered) and _is_spaced_script_char(lowered[end]):
        return False
    return True


def find_first_alias(text: str, brand: BrandSpec) -> tuple[int, str] | None:
    """在文本中定位最早出现的品牌名/别名。返回 (字符偏移, 命中别名) 或 None。
    不区分大小写;拉丁字母的名字按词边界,中文等不以空格分词的文字按子串(D18,v2)。
    用 lower() 而非 casefold():保证偏移与原文一一对应。"""
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for alias in dict.fromkeys([brand.name, *brand.aliases]):
        needle = alias.strip().lower()
        if not needle:
            continue
        pos = lowered.find(needle)
        while pos >= 0 and not _bounded_at(lowered, pos, needle):
            pos = lowered.find(needle, pos + 1)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, alias)
    return best
