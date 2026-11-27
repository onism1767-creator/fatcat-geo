"""K4 的词表:最高级与说服手法(中英)。只是数据和一个查找函数,检查器和标题建议共用。

依据:SCI-Defense、GEO-Flag 把这类措辞当作操纵特征(设计 §7.4)。Butter 自己不写,也不建议站长写。
"""

from __future__ import annotations

import re

_I = re.IGNORECASE

# 最高级
SUPERLATIVE_EN = [
    r"best", r"#\s?1", r"no\.?\s?1", r"number one", r"leading", r"world[- ]class", r"unrivall?ed", r"unbeatable",
    r"unmatched", r"ultimate", r"top[- ]rated", r"top[- ]notch", r"award[- ]winning", r"the only", r"perfect",
    r"greatest", r"finest", r"premier", r"superior", r"revolutionary", r"cutting[- ]edge", r"state[- ]of[- ]the[- ]art",
    r"industry[- ]leading", r"market[- ]leading", r"guaranteed",
]
SUPERLATIVE_ZH = ["最好", "最佳", "最强", "最优", "最值得", "最受", "最专业", "最先进", "第一", "领先", "唯一", "顶级", "顶尖",
                  "首选", "王者", "天花板", "无与伦比", "独一无二", "遥遥领先", "行业标杆", "极致", "完美", "冠军"]
# 说服手法:稀缺、社会证明、权威、催促
PERSUASION_EN = [
    r"limited[- ]time", r"only \d+ left", r"while (?:stocks?|supplies) last", r"act now", r"hurry", r"don'?t miss",
    r"trusted by (?:millions|thousands)", r"millions of (?:users|customers|people)", r"experts? (?:recommend|agree)",
    r"doctors? recommend", r"must[- ]have", r"you (?:need|deserve)", r"once in a lifetime", r"exclusive (?:offer|deal)",
]
PERSUASION_ZH = ["限时", "仅剩", "手慢无", "错过", "万人选择", "万人好评", "专家推荐", "医生推荐", "明星同款", "抢购", "必买",
                 "必备", "闭眼入", "不容错过", "火爆", "爆款", "口碑爆棚", "销量第一"]
# 标题照抄题目前另外要拦的词:出题句式里常见,检查器不查它们(设计 §9.2,11-15)
TITLE_EXTRA_EN = [r"top", r"most", r"recommend(?:ed|s|ation|ations)?", r"better"]
TITLE_EXTRA_ZH = ["推荐", "排行", "排名", "哪个好", "哪家好", "最"]


def _compile(en: list[str], zh: list[str]) -> re.Pattern[str]:
    parts = [rf"(?<![A-Za-z0-9]){w}(?![A-Za-z0-9])" for w in en] + [re.escape(w) for w in zh]
    return re.compile("|".join(parts), _I)


K4_PATTERN = _compile(SUPERLATIVE_EN + PERSUASION_EN, SUPERLATIVE_ZH + PERSUASION_ZH)
TITLE_PATTERN = _compile(SUPERLATIVE_EN + PERSUASION_EN + TITLE_EXTRA_EN,
                         SUPERLATIVE_ZH + PERSUASION_ZH + TITLE_EXTRA_ZH)


def puffery_hits(text: str, *, for_title: bool = False) -> list[tuple[str, int, int]]:
    """返回 (命中的词, 起, 止)。for_title=True 时多拦 top、most、recommend、推荐 这一类。"""
    pattern = TITLE_PATTERN if for_title else K4_PATTERN
    return [(m.group(0), m.start(), m.end()) for m in pattern.finditer(text)]
