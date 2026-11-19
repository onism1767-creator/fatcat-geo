"""起草检查器 K1–K9(纯层,不用模型,不信模型自报的引用)。设计 §7.4。

硬门禁,不打分。每条规则是注册表里的一项:查什么、挂哪个证据条目、不过时怎么办。
  K1–K4 不过:带着原因让模型重写一次,重写稿再过一遍,还不过整条作废;
  K5–K7 不过:直接作废;
  K8、K9 不过:警告,留给站长。
自动修正只限纯格式(多余空白),不改事实。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from butter.audit.instructions import find_instructions
from butter.check.lexicon import puffery_hits
from butter.contracts.draft import (
    DRAFT_FIELDS, CheckContext, CheckResult, DraftProposal, OnFail, RuleId, Verdict,
)
from butter.registry.ids import (
    FAKE_TIMESTAMP, HIDDEN_TEXT_INJECTION, NUMERIC_TRACEABILITY, SCALED_AI_PAGES, STRUCT, TONE_DECLARATIVE,
)

RULE_VERSION = "check/1"
Hit = tuple[str, tuple[int, int] | None]  # (说明, 位置)


def tidy(text: str) -> str:
    """唯一的自动修正:把多余空白收成一个空格、去掉首尾空白。"""
    return re.sub(r"[ \t\r\n　]+", " ", text).strip()


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


# ---- K1 数字可溯源 ----

# 前面紧挨字母的数字是型号的一部分(S380、E10),归 K2;后面可以跟单位。
_NUM = re.compile(r"(?<![A-Za-z0-9.,])\d[\d,]*(?:\.\d+)?")


def _canon(num: str) -> Decimal | None:
    try:
        return Decimal(num.replace(",", "")).normalize()
    except InvalidOperation:
        return None


def numbers_in(text: str) -> list[tuple[Decimal, int, int]]:
    out = []
    for m in _NUM.finditer(_nfkc(text)):
        value = _canon(m.group(0).rstrip(","))
        if value is not None:
            out.append((value, m.start(), m.end()))
    return out


def _source_text(ctx: CheckContext) -> str:
    return "\n".join([ctx.page_text, *(f.value for f in ctx.facts), *ctx.own_names])


def k1_numbers(text: str, ctx: CheckContext) -> list[Hit]:
    allowed = {v for v, _, _ in numbers_in(_source_text(ctx))}
    in_dates = [(a, b) for _, a, b in dates_in(text)]  # 日期里的数字归 K3 管
    return [(f"数字 {_nfkc(text)[a:b]} 在允许的事实和原页面里都找不到", (a, b))
            for v, a, b in numbers_in(text)
            if v not in allowed and not any(x <= a and b <= y for x, y in in_dates)]


# ---- K2 型号与专名 ----

_MODEL = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}[A-Z0-9]*(?![A-Za-z0-9])")


def _contains_name(haystack: str, name: str) -> bool:
    name = name.strip()
    if not name:
        return False
    if re.search(r"[A-Za-z0-9]", name):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", haystack, re.IGNORECASE) is not None
    return name in haystack


def k2_names(text: str, ctx: CheckContext) -> list[Hit]:
    norm = _nfkc(text)
    source = _nfkc(_source_text(ctx)).casefold()
    hits: list[Hit] = []
    seen: set[str] = set()
    for pattern in (_MODEL, _ACRONYM):
        for m in pattern.finditer(norm):
            token = m.group(0)
            if token.casefold() in seen:
                continue
            seen.add(token.casefold())
            if token.casefold() not in source:
                hits.append((f"型号或专名 {token} 在事实库和原页面里都找不到", (m.start(), m.end())))
    for name in ctx.competitor_names:
        if _contains_name(norm, _nfkc(name)):
            hits.append((f"出现了别家的品牌名「{name}」", None))
    return hits


# ---- K3 日期 ----

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"])}
_MONTHS |= {k[:3]: v for k, v in _MONTHS.items()}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[int, int, int | None]]]] = [
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*[日号])?"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]) if m[3] else None)),
    (re.compile(rf"(?<![A-Za-z])({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE),
     lambda m: (int(m[3]), _MONTHS[m[1].lower()], int(m[2]))),
    (re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE),
     lambda m: (int(m[3]), _MONTHS[m[2].lower()], int(m[1]))),
    (re.compile(rf"(?<![A-Za-z])({_MON})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE),
     lambda m: (int(m[2]), _MONTHS[m[1].lower()], None)),
]


def dates_in(text: str) -> list[tuple[tuple[int, int, int | None], int, int]]:
    norm = _nfkc(text)
    out, taken = [], []
    for pattern, parse in _DATE_PATTERNS:
        for m in pattern.finditer(norm):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue  # 已被更具体的写法认走
            taken.append((m.start(), m.end()))
            out.append((parse(m), m.start(), m.end()))
    return out


def _trusted(ctx: CheckContext) -> set[tuple[int, int, int | None]]:
    out: set[tuple[int, int, int | None]] = set()
    for raw in ctx.trusted_dates:
        parts = raw[:10].split("-")
        if len(parts) >= 2 and all(p.isdigit() for p in parts):
            y, mo = int(parts[0]), int(parts[1])
            out.add((y, mo, None))
            if len(parts) == 3:
                out.add((y, mo, int(parts[2])))
    return out


def k3_dates(text: str, ctx: CheckContext) -> list[Hit]:
    ok = _trusted(ctx)
    return [(f"日期 {_nfkc(text)[a:b]} 不是事实库里可信的日期", (a, b)) for d, a, b in dates_in(text) if d not in ok]


# ---- K4 最高级与说服手法 ----

def k4_puffery(text: str, ctx: CheckContext) -> list[Hit]:
    norm = _nfkc(text)
    claims = [_nfkc(c).casefold() for c in ctx.sourced_claims]
    return [(f"用了夸大或劝说的词「{word}」", (a, b)) for word, a, b in puffery_hits(norm)
            if not _restates_claim(norm, a, b, claims)]


def _restates_claim(text: str, a: int, b: int, claims: list[str]) -> bool:
    """这个词所在的那句话,是不是在复述事实库里带来源的对比主张(连着三个词、或连着六个字一样才算)。"""
    start = max(text.rfind(ch, 0, a) for ch in ".!?。!?;;") + 1
    ends = [i for i in (text.find(ch, b) for ch in ".!?。!?;;") if i != -1]
    sentence = text[start:min(ends) if ends else len(text)].casefold()
    words = re.findall(r"[a-z0-9]+", sentence)
    grams = {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
    compact = re.sub(r"\s+", "", sentence)
    cjk = {compact[i:i + 6] for i in range(len(compact) - 5) if _CJK_RUN.fullmatch(compact[i:i + 6])}
    for claim in claims:
        flat = " ".join(re.findall(r"[a-z0-9]+", claim))
        if any(g in flat for g in grams) or any(g in re.sub(r"\s+", "", claim) for g in cjk):
            return True
    return False


_CJK_RUN = re.compile(r"[一-鿿]{6}")


# ---- K5 面向 AI 的指令 ----

def k5_instructions(text: str, ctx: CheckContext) -> list[Hit]:
    return [(f"像是写给 AI 的指令({name}):{snip}", None) for name, snip in find_instructions(_nfkc(text))]


# ---- K6 隐藏与格式 ----

_TAG = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")
_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿­]")


def k6_hidden(text: str, ctx: CheckContext) -> list[Hit]:
    hits: list[Hit] = []
    for m in _TAG.finditer(text):
        hits.append((f"含 HTML 标签或实体 {m.group(0)[:30]}", (m.start(), m.end())))
        break
    m = _ZERO_WIDTH.search(text)
    if m:
        hits.append((f"含看不见的字符 U+{ord(m.group(0)):04X}", (m.start(), m.end())))
    for i, ch in enumerate(text):
        if unicodedata.category(ch) in ("Cc", "Co", "Cn") and ch != " ":
            hits.append((f"含控制字符 U+{ord(ch):04X}", (i, i + 1)))
            break
    if re.search(r"\bstyle\s*=|display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0", text, re.IGNORECASE):
        hits.append(("含样式写法", None))
    return hits


# ---- K7 只加不改 ----

def k7_whitelist(field: str) -> list[Hit]:
    return [] if field in DRAFT_FIELDS else [(f"字段 {field} 不在允许起草的范围里", None)]


# ---- K8 长度、语言与好懂 ----

JARGON = ["赋能", "抓手", "闭环", "颗粒度", "链路", "痛点", "心智", "矩阵", "生态", "一站式", "全方位", "leverage", "synergy",
          "seamless", "robust", "holistic", "empower", "next-generation", "best-in-class", "turnkey", "paradigm"]
_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def k8_readability(field: str, text: str, ctx: CheckContext) -> list[Hit]:
    hits: list[Hit] = []
    lim = ctx.limits
    if field == "title" and len(text) > lim.title_max_chars:
        hits.append((f"标题 {len(text)} 个字符,超过 {lim.title_max_chars}", None))
    if field == "meta_description" and len(text) > lim.meta_max_chars:
        hits.append((f"meta 描述 {len(text)} 个字符,超过 {lim.meta_max_chars}", None))
    cjk, latin = len(_CJK.findall(text)), len(_LATIN.findall(text))
    lang = (ctx.page_lang or "").lower()
    if cjk + latin >= 8:
        share = cjk / (cjk + latin)
        if lang.startswith("zh") and share < 0.3:
            hits.append(("页面是中文,这段文字基本不是中文", None))
        elif lang and not lang.startswith(("zh", "ja", "ko")) and share > 0.2:
            hits.append((f"页面语言是 {ctx.page_lang},这段文字里中文太多", None))
    for sentence in re.split(r"[.!?。!?;;]\s*", text):
        words, chars = len(sentence.split()), len(_CJK.findall(sentence))
        if chars > lim.sentence_max_chars or (chars == 0 and words > lim.sentence_max_words):
            hits.append((f"有一句太长({chars or words} 个{'字' if chars else '词'}),拆短些更好懂", None))
            break
    low = _nfkc(text).casefold()
    used = [w for w in JARGON if w in low]
    if used:
        hits.append((f"用了不好懂的词:{'、'.join(used)}", None))
    return hits


# ---- 注册表 ----

RULES: list[tuple[RuleId, str | None, OnFail, str]] = [
    ("K1", NUMERIC_TRACEABILITY, "rewrite", "数字可溯源"),
    ("K2", STRUCT, "rewrite", "型号与专名"),
    ("K3", FAKE_TIMESTAMP, "rewrite", "日期"),
    ("K4", TONE_DECLARATIVE, "rewrite", "最高级与说服手法"),
    ("K5", HIDDEN_TEXT_INJECTION, "void", "面向 AI 的指令"),
    ("K6", HIDDEN_TEXT_INJECTION, "void", "隐藏与格式"),
    ("K7", None, "void", "只加不改"),
    ("K8", None, "warn", "长度、语言与好懂"),
    ("K9", SCALED_AI_PAGES, "warn", "规模化雷同"),
]
_BY_ID = {r[0]: r for r in RULES}


def _results(rule: RuleId, hits: list[Hit]) -> list[CheckResult]:
    _, entry, on_fail, _ = _BY_ID[rule]
    if not hits:
        return [CheckResult(rule=rule, rule_version=RULE_VERSION, entry_id=entry, outcome="通过", on_fail=on_fail)]
    outcome = "警告" if on_fail == "warn" else "作废"
    return [CheckResult(rule=rule, rule_version=RULE_VERSION, entry_id=entry, outcome=outcome, on_fail=on_fail,
                        detail=detail, span=span) for detail, span in hits]


def check_proposal(proposal: DraftProposal, ctx: CheckContext) -> tuple[str, list[CheckResult]]:
    """查一条草稿(K1–K8)。返回(收拾过空白的文字, 各规则的结果)。K6 查的是收拾空白之前的原文。"""
    raw, text = proposal.text, tidy(proposal.text)
    out: list[CheckResult] = []
    out += _results("K1", k1_numbers(text, ctx))
    out += _results("K2", k2_names(text, ctx))
    out += _results("K3", k3_dates(text, ctx))
    out += _results("K4", k4_puffery(text, ctx))
    out += _results("K5", k5_instructions(text, ctx))
    out += _results("K6", k6_hidden(raw.replace("\n", " ").replace("\r", " ").replace("\t", " "), ctx))
    out += _results("K7", k7_whitelist(proposal.field))
    out += _results("K8", k8_readability(proposal.field, text, ctx) if text else [("草稿是空的", None)])
    return text, out


def decide(results: list[CheckResult], *, already_rewritten: bool) -> Verdict:
    """这一条草稿怎么办:直接作废的规则有一条不过就作废;可重写的规则不过,没重写过就重写,重写过就作废。"""
    failed = [r for r in results if r.outcome == "作废"]
    if any(r.on_fail == "void" for r in failed):
        return "void"
    if failed:
        return "void" if already_rewritten else "rewrite"
    return "accept"


def rewrite_reasons(results: list[CheckResult]) -> list[str]:
    """带回给模型的失败原因(只给可重写的那几条)。"""
    return [f"{r.rule}:{r.detail}" for r in results if r.outcome == "作废" and r.on_fail == "rewrite"]


def needs_page_warning(results: list[CheckResult]) -> bool:
    """K5 / K6 不过时,报告里另提示"该页可能含针对模型的指令"。"""
    return any(r.outcome == "作废" and r.rule in ("K5", "K6") for r in results)


def _shingles(text: str) -> set[str]:
    norm = re.sub(r"[\W_]+", " ", _nfkc(text).casefold()).strip()
    words = norm.split()
    if len(words) >= 4:
        return {" ".join(words[i:i + 2]) for i in range(len(words) - 1)}
    compact = norm.replace(" ", "")
    return {compact[i:i + 3] for i in range(max(1, len(compact) - 2))}


def k9_duplicates(items: list[tuple[str, str, str]], threshold: float = 0.85) -> dict[int, CheckResult]:
    """多页之间比。items = [(页面网址, 字段, 文字)];返回 {序号: 警告}。只比不同页面的同一个字段。"""
    _, entry, on_fail, _ = _BY_ID["K9"]
    sh = [_shingles(t) for _, _, t in items]
    out: dict[int, CheckResult] = {}
    for i, (url_i, field_i, _) in enumerate(items):
        for j in range(i + 1, len(items)):
            url_j, field_j, _ = items[j]
            if url_i == url_j or field_i != field_j or not sh[i] or not sh[j]:
                continue
            sim = len(sh[i] & sh[j]) / len(sh[i] | sh[j])
            if sim >= threshold:
                for a, other in ((i, url_j), (j, url_i)):
                    out.setdefault(a, CheckResult(
                        rule="K9", rule_version=RULE_VERSION, entry_id=entry, outcome="警告", on_fail=on_fail,
                        detail=f"和 {other} 的同一个字段几乎一样(相似度 {sim:.0%})"))
    return out
