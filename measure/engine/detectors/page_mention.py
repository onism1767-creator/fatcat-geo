"""网页正文提及判定器 v1:一个抓回的网页正文里有没有写到品牌(v0.4.26 S6.1;产品文档 D23)。

「写到你」= 抽出的正文(去掉导航、页脚、侧栏、评论)里出现品牌名或已确认的别名,匹配规则与回答里的提及判定同一套
(engine/detectors/matching.py:拉丁字母按词边界、中文按子串)。另记命中位置:标题 / 首段 / 正文——
主视图只用「有没有」,「标题或首段就出现」进技术栏。抓不到正文的网页不可判定,返回 None(看不到)。
判定对象是存档的 PageCheck(只追加),规则升级提版本后可对历史网页重算,不用重抓。
"""

from __future__ import annotations

from enum import StrEnum

from engine.detectors.matching import find_first_alias
from engine.models import BrandSpec, FrozenModel, PageCheck, PageFetchStatus

DETECTOR_NAME = "page_mention_rules"
DETECTOR_VERSION = 1


class MentionPosition(StrEnum):
    TITLE = "title"
    LEAD = "lead"  # 正文首段
    BODY = "body"


class PageMention(FrozenModel):
    found: bool
    position: MentionPosition | None = None
    alias: str | None = None
    detector_version: int = DETECTOR_VERSION


def lead_paragraph(text: str) -> str:
    """正文首段 = 第一段非空文字。"""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def page_mention(check: PageCheck, brand: BrandSpec) -> PageMention | None:
    """抓到正文才判;标题 → 首段 → 正文依次找,记最靠前的位置。"""
    if check.status is not PageFetchStatus.OK or not check.text:
        return None
    if check.title and (hit := find_first_alias(check.title, brand)) is not None:
        return PageMention(found=True, position=MentionPosition.TITLE, alias=hit[1])
    if (hit := find_first_alias(lead_paragraph(check.text), brand)) is not None:
        return PageMention(found=True, position=MentionPosition.LEAD, alias=hit[1])
    if (hit := find_first_alias(check.text, brand)) is not None:
        return PageMention(found=True, position=MentionPosition.BODY, alias=hit[1])
    return PageMention(found=False)
