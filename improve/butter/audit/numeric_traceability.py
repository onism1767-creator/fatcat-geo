"""检查三:数值可溯源(规则启发式,只出提示)。

在可见正文里找统计型数字(百分比、倍数、大数量),看它所在的那一块文字里有没有来源:
链接、<cite> / 脚注、或「来源 / 根据 / Source / according to」一类字样。价格不算(价格由检查五对照结构化数据)。
"""

from __future__ import annotations

import re

from butter.audit.base import finding
from butter.contracts import AuditFinding, PageExtract
from butter.registry_ids import STATS
from butter.util import snippet

CHECK = "numeric_traceability"
_MIN_TEXT = 50
_CLAIMS: list[tuple[str, re.Pattern[str]]] = [
    ("百分比", re.compile(r"\d+(?:\.\d+)?\s?[%％]")),
    ("倍数", re.compile(r"\d+(?:\.\d+)?\s?(?:倍|×(?!\s?\d)|x(?![\w\d]))", re.IGNORECASE)),
    ("大数量", re.compile(r"\d[\d,,.]*\s?(?:千万|万|亿|million|billion|k\+)(?![a-z])|\d[\d,,]{2,}\+", re.IGNORECASE)),
]
_SOURCE_MARK = re.compile(
    r"来源|数据来自|数据来源|出处|引自|根据|据.{1,14}?(?:统计|报告|数据|调查|研究|显示)|source|according to|study|survey|"
    r"report(?:ed)? by|research by|\[\d+\]|※|注\s?\d*\s?[::]", re.IGNORECASE)
_CURRENCY = re.compile(r"[¥¥$€£]|元|美元|人民币|USD|CNY|RMB|EUR|GBP", re.IGNORECASE)


def _claims_in(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _CLAIMS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue
            if _CURRENCY.search(text[max(0, m.start() - 3): m.end() + 3]):
                continue  # 价格
            taken.append((m.start(), m.end()))
            out.append((kind, snippet(text[max(0, m.start() - 50): m.end() + 50], 140)))
    return out


def check(extracts: list[PageExtract]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for page in extracts:
        if len(page.visible_text) < _MIN_TEXT:
            findings.append(finding(CHECK, STATS, "not_checkable", page.url,
                                    f"原始 HTML 里的可见文字太少({len(page.visible_text)} 字),查不了。可能靠脚本渲染。"))
            continue
        total = 0
        untraced: list[tuple[str, str]] = []
        for block in page.blocks:
            claims = _claims_in(block.text)
            if not claims:
                continue
            total += len(claims)
            if not (block.has_link or block.has_cite or _SOURCE_MARK.search(block.text)):
                untraced.extend(claims)
        if total == 0:
            findings.append(finding(CHECK, STATS, "pass", page.url, "正文里没有找到统计型数字(百分比、倍数、大数量)。"))
        elif not untraced:
            findings.append(finding(CHECK, STATS, "pass", page.url,
                                    f"正文里 {total} 处统计型数字,所在段落都带来源链接或出处字样。"))
        else:
            evidence = {f"example_{i}": f"[{kind}] {text}" for i, (kind, text) in enumerate(untraced[:5], start=1)}
            findings.append(finding(
                CHECK, STATS, "notice", page.url,
                f"正文里 {total} 处统计型数字,其中 {len(untraced)} 处所在段落没有来源链接或出处字样。"
                "这是规则判断,可能误报;确有出处的建议把出处写在数字旁边。", **evidence))
    return findings
