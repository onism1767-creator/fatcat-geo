"""Butter 出题的数据形状(设计 §9.2)。questions/1。"""

from __future__ import annotations

from typing import Literal

from butter.contracts.common import Frozen

# 用 Potato 的四种意图(Butter 只记 Potato 的意图,给 Toast 的提案里再换算)
QuestionIntent = Literal["discovery", "comparison", "local", "brand_defense"]
BUCKETS: tuple[tuple[str, QuestionIntent], ...] = (
    ("d", "discovery"), ("c", "comparison"), ("l", "local"), ("b", "brand_defense"))


class QuestionTemplates(Frozen):
    """一种语言的固定句式。{c} 填品类词,{f} 填本品牌名。"""

    source: str
    category_fallback: str
    discovery: list[str]
    comparison: list[str]
    local: list[str]
    brand_defense: list[str]


class TemplateSet(Frozen):
    version: str
    en: QuestionTemplates
    zh: QuestionTemplates


class GeneratedQuestion(Frozen):
    id: str                       # 如 g1-d01:第 1 个品类的发现题第 1 道
    intent: QuestionIntent
    text: str
    lang: Literal["en", "zh"]
    category: str                 # 出题用的品类词;对页面时就按它对
    names_brand: bool = False     # 品牌题:只作背景,不进处理组也不进对照组
    needs_edit: bool = False      # 场景那 6 道比较泛,界面提示站长改得更贴自己的行业
    source: Literal["butter_template"] = "butter_template"
    template_version: str
