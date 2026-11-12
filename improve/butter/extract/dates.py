"""日期识别:把页面各处的日期写法认成 YYYY-MM-DD。认不出就返回 None,不猜。"""

from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

_MONTHS = {
    name: i for i, names in enumerate([
        ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
        ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
        ("november", "nov"), ("december", "dec"),
    ], start=1) for name in names
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))

# 各种写法的日期(不含标签);按出现顺序尝试
DATE_PATTERN = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{4}/\d{1,2}/\d{1,2}"
    r"|\d{4}\.\d{1,2}\.\d{1,2}"
    r"|\d{4}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日"
    rf"|(?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES})\.?,?\s+\d{{4}})"
)

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_CJK = re.compile(r"^(\d{4})\s?年\s?(\d{1,2})\s?月\s?(\d{1,2})\s?日$")
_MDY = re.compile(rf"^({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})$", re.IGNORECASE)
_DMY = re.compile(rf"^(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+(\d{{4}})$", re.IGNORECASE)


def _make(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def to_iso_date(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if m := _ISO.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3]))
    if m := _SLASH.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3]))
    if m := _CJK.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3]))
    if m := _MDY.match(text):
        return _make(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    if m := _DMY.match(text):
        return _make(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    try:  # HTTP 头的 RFC 1123 写法
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None


# 可见日期只认带标签的:正文里随便一个日期可能是任何东西
_MODIFIED_LABELS = r"最后更新|最近更新|更新时间|更新日期|更新于|修改时间|修订|更新|last\s+updated|updated(?:\s+on)?|last\s+modified|modified|revised"
_PUBLISHED_LABELS = r"发布时间|发布日期|发布于|发表于|发表时间|发布|published(?:\s+on)?|posted(?:\s+on)?|date\s+published"
LABELED_DATE = re.compile(
    rf"(?P<label>{_MODIFIED_LABELS}|{_PUBLISHED_LABELS})\s*[::]?\s*(?P<date>{DATE_PATTERN})",
    re.IGNORECASE,
)
_MODIFIED_RE = re.compile(rf"^(?:{_MODIFIED_LABELS})$", re.IGNORECASE)


def label_kind(label: str) -> str:
    return "modified" if _MODIFIED_RE.match(label.strip()) else "published"


def find_labeled_dates(text: str) -> list[tuple[str, str, str]]:
    """在可见文字里找「标签 + 日期」。返回 (原文片段, 日期原文, published / modified)。"""
    found = []
    for m in LABELED_DATE.finditer(text):
        found.append((m.group(0), m.group("date"), label_kind(m.group("label"))))
    return found
