"""Butter 出题(设计 §9.2)。纯函数:固定句式里填品类词和品牌名,不调模型,不花钱。

站长手里没有现成的题时用。出来的只是候选:站长勾选、可以改字。Butter 出的题在批次里标出"题从哪来"。
"""

from __future__ import annotations

import re

from butter.contracts import FactBase, PageFacts
from butter.contracts.questions import BUCKETS, GeneratedQuestion, TemplateSet
from butter.generate.headings import tokens

EXPECTED = {"discovery": 8, "comparison": 6, "local": 6, "brand_defense": 4}
_CJK = re.compile(r"[一-鿿]")


class QuestionError(ValueError):
    pass


def check_templates(templates: TemplateSet) -> None:
    """句式文件要满足的硬规矩:8/6/6/4;前 20 道只有 {c},品牌题必须有 {f};不许有别的占位符。"""
    for lang in ("en", "zh"):
        t = getattr(templates, lang)
        for _, intent in BUCKETS:
            items = getattr(t, intent)
            if len(items) != EXPECTED[intent]:
                raise QuestionError(f"{lang}.{intent} 应有 {EXPECTED[intent]} 道,实际 {len(items)} 道")
            for text in items:
                marks = set(re.findall(r"\{[^}]*\}", text))
                if intent == "brand_defense":
                    if "{f}" not in marks or marks - {"{c}", "{f}"}:
                        raise QuestionError(f"{lang}.{intent}:品牌题要有 {{f}},且只能用 {{c}}、{{f}}:{text}")
                elif marks != {"{c}"}:
                    raise QuestionError(f"{lang}.{intent}:中立题只能有 {{c}},不能点名品牌:{text}")


def draft_questions(templates: TemplateSet, *, lang: str, categories: list[str], brand: str = "",
                    ) -> list[GeneratedQuestion]:
    """每个品类 24 道(没有品牌名时不出那 4 道品牌题)。品类词去重、去空;一个都没有时用兜底词。"""
    if lang not in ("en", "zh"):
        raise QuestionError(f"只有英文和中文两套句式,没有 {lang}")
    t = getattr(templates, lang)
    cats: list[str] = []
    for c in categories:
        c = " ".join(c.split())
        if c and c.casefold() not in {x.casefold() for x in cats}:
            cats.append(c)
    cats = cats or [t.category_fallback]
    brand = " ".join(brand.split())
    out: list[GeneratedQuestion] = []
    for n, category in enumerate(cats, start=1):
        for prefix, intent in BUCKETS:
            if intent == "brand_defense" and not brand:
                continue
            for i, template in enumerate(getattr(t, intent), start=1):
                out.append(GeneratedQuestion(
                    id=f"g{n}-{prefix}{i:02d}", intent=intent, lang=lang, category=category,
                    text=template.replace("{c}", category).replace("{f}", brand),
                    names_brand=intent == "brand_defense", needs_edit=intent == "local",
                    template_version=templates.version))
    return out


def categories_from_factbase(fb: FactBase) -> list[str]:
    """事实库里已有的品类词(站点的、各商品的),按出现顺序去重。没有就是空:Butter 不猜,由站长自己填。"""
    out: list[str] = []
    for field in (fb.entity.category, *(p.category for p in fb.products)):
        for v in field.values:
            word = " ".join(v.value.split())
            if word and word.casefold() not in {x.casefold() for x in out}:
                out.append(word)
    return out


def guess_lang(fb: FactBase) -> str:
    """出哪种语言的题:看各页的 lang,中文页占多数就出中文,否则英文。站长可以改。"""
    langs = [(p.lang or "").lower() for p in fb.pages]
    zh = sum(1 for x in langs if x.startswith("zh"))
    if zh or any(langs):
        return "zh" if zh * 2 > len([x for x in langs if x]) else "en"
    text = " ".join(filter(None, (p.title for p in fb.pages)))
    return "zh" if len(_CJK.findall(text)) * 2 > len(re.findall(r"[A-Za-z]", text)) + 1 else "en"


def match_by_category(pages: list[PageFacts], questions: list[GeneratedQuestion]) -> dict[str, list[str]]:
    """Butter 出的题对上哪些页面:出题用的品类词(单复数归一)整个出现在页面的标题或 H1 里就算对上。

    不走"连续词组"那条规则:单个词的品类(headphones、耳机)过不了它;品类词是出题时就定的,不会是碰巧重合。
    品牌题不对页面(只作背景)。返回 {题号: [页面网址]},对不上的题是空列表。
    """
    page_words = {p.url: set().union(*(tokens(f) for f in (p.title or "", *p.h1))) for p in pages}
    out: dict[str, list[str]] = {}
    for q in questions:
        if q.names_brand:
            out[q.id] = []
            continue
        need = tokens(q.category)
        out[q.id] = [url for url, words in page_words.items() if need and need <= words]
    return out


def to_board_questions(questions: list[GeneratedQuestion], edits: dict[str, str] | None = None,
                       picked: set[str] | None = None) -> list[dict[str, str]]:
    """站长勾选、改字之后,变成 Potato questions.yml 里 questions 那一段的形状(id、intent、text)。"""
    edits = edits or {}
    out = []
    for q in questions:
        if picked is not None and q.id not in picked:
            continue
        text = " ".join(edits.get(q.id, q.text).split())
        if text:
            out.append({"id": q.id, "intent": q.intent, "text": text})
    return out


__all__ = ["QuestionError", "categories_from_factbase", "check_templates", "draft_questions", "guess_lang",
           "match_by_category", "to_board_questions"]
