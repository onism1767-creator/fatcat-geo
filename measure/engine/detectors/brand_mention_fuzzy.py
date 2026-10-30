"""品牌提及判定器·模糊版 v3(S9.2,中档;架构书附录 v0.4.27 第 4 节 + 用户 2026-10-14 拍板的「规则 + 模糊匹配」路线;
v2 = 2026-10-27 审核黄 9 用户定稿;v3 = BR2,调研报告 docs/研究/品牌名识别-方法调研-2026-10-28.md 方向 A
+ 用户 2026-10-29 定的 Q1、Q2)。

三道关,全部跑完取回答里最早出现的那一处(同一位置按 精确 > 规整 > 模糊):
1. 精确:与规则判定器(v2)同一套原语——别名表 + 大小写不敏感,拉丁字母按词边界、中文按子串;
2. 规整:NFKC + 小写(全角 → 半角、半角片假名 → 全角,韩文音节不拆),只有拉丁字母去重音(Citroen 认 Citroën;Q2:越南语单不去,
   声调不同是别的词,Pho Hoa 不算 Phở Hòa,这种单连 Citroen 也不认)。回答里名字中间只认空格 / 连字符,不跨逗号句号
   (「极光 CRM」「Aurora-CRM」认,「Meta, AI」「Meta. AI」不认);名字自己带的分隔符照原样写也认(「monday.com」),
   不写也认(「Dr Martens」对「Dr. Martens」)。词边界规则照旧;
3. 模糊:只对纯拉丁 / 数字、≥ 6 个字符的名字,只认多一个复数尾巴 s / es(所有格的撇号本来就是词边界,第一道关认)。
   多别的尾巴是别的词(Columbian 不是 Columbia),少尾巴是别家(Canva 不是 Canvas),中间换字母也是别的词;
   Q1:加尾后成了普通词的不认(targets、notions、mondays)——英文词频表 ≥ 百万分之一就算普通词(wordfreq,版本由锁文件固定)。
   短名字一加就是别的词,不认尾巴;中文等不以空格分词的文字不做模糊(按子串,用户定)。
结果另记命中片段与认法(match_kind / matched_text / edit_distance = 尾巴多几个字符),供单条回答页;与规则判定器并排存档
(各带 detector_name / version),两者的分歧率留给自动校准(S9.4)。门槛是本模块常量,校准后调;不装模型(GLiNER 留到校准时试)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby

from wordfreq import zipf_frequency

from engine.detectors.matching import bounded_at, find_first_alias, is_spaced_script_char
from engine.models import BrandMentionObservation, BrandSpec, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "brand_mention_fuzzy"
DETECTOR_VERSION = 3

# 名字里可写可不写的分隔符(NFKC 之后全角空格、全角连字符等已变成这些半角字符)
_SEPARATORS = " \t\r\n-_.·•・/\\'\""
_JOIN = "[ \\-\u2010]*"  # 回答里名字中间能隔着的:空格、连字符(NFKC 之后的半角)
_TAIL = "(?:es|s)"  # 模糊只认这两种尾巴,封闭清单
_MIN_TAIL_LETTERS = 6  # 短于这个的名字不认尾巴
_COMMON_WORD_ZIPF = 3.0  # 词频表刻度:3 = 百万分之一,这个刻度以上的词收在小表里
_TONE_LANGUAGES = frozenset({"vi"})  # 声调区别词义的语言:这些语言的单不去重音(Q2)
_KIND_ORDER = {"exact": 0, "normalized": 1, "fuzzy": 2}


@dataclass(frozen=True)
class _Hit:
    position: int  # 原文字符偏移
    alias: str  # 命中的登记名 / 别名(原样)
    text: str  # 原文里的命中片段
    kind: str  # exact / normalized / fuzzy
    distance: int


def _keeps_marks(locale: str) -> bool:
    return locale.split("-")[0].lower() in _TONE_LANGUAGES


def _composes(cluster: str, ch: str) -> bool:
    """ch 和前面的字是不是合成一个字:组合重音、半角浊点、拆开写的韩文字母。"""
    if ch.isascii():
        return False
    if unicodedata.combining(ch):
        return True
    return len(unicodedata.normalize("NFKC", cluster + ch)) <= len(unicodedata.normalize("NFKC", cluster))


def _fold(cluster: str, keep_marks: bool) -> str:
    composed = unicodedata.normalize("NFKC", cluster)
    decomposed = unicodedata.normalize("NFD", composed)
    if not keep_marks and decomposed[0].isascii() and decomposed[0].isalpha():  # 只动拉丁字母:기아 不能拆成 기압 的前缀
        composed = "".join(c for c in decomposed if not unicodedata.combining(c))
    return composed.lower().replace("’", "'")


@dataclass(frozen=True)
class _Folded:
    """规整后的文字:folded 的每个字符在 spans 里有对应的(原文起点, 原文终点)——偏移永远指回原文。"""

    text: str
    folded: str
    spans: list[tuple[int, int]]

    def hit(self, match: re.Match[str], alias: str, kind: str, distance: int) -> _Hit:
        start, end = self.spans[match.start()][0], self.spans[match.end() - 1][1]
        return _Hit(start, alias, self.text[start:end], kind, distance)


def _folded(text: str, keep_marks: bool) -> _Folded:
    """合成一个字的几个字符一起规整:ﾄﾞ 才是 ド。"""
    folded: list[str] = []
    spans: list[tuple[int, int]] = []
    start = 0
    for end in range(1, len(text) + 1):
        if end < len(text) and _composes(text[start:end], text[end]):
            continue
        piece = _fold(text[start:end], keep_marks)
        folded.append(piece)
        spans.extend([(start, end)] * len(piece))
        start = end
    return _Folded(text, "".join(folded), spans)


def _names(brand: BrandSpec) -> list[str]:
    return [alias for alias in dict.fromkeys([brand.name, *brand.aliases]) if alias.strip()]


def _exact_hit(text: str, brand: BrandSpec) -> _Hit | None:
    found = find_first_alias(text, brand)
    if found is None:
        return None
    position, alias = found
    return _Hit(position, alias, text[position:position + len(alias.strip())], "exact", 0)


def _name_regex(name: str) -> str:
    """规整后的名字写成比对式:字与字之间能隔空格 / 连字符;名字自带的分隔符要么原样、要么换成空格 / 连字符或不写。
    「Monday.com」认 monday com,不认 Monday. Com。"""
    parts: list[str] = []
    for is_separator, group in groupby(name, lambda c: c in _SEPARATORS):
        run = "".join(group)
        parts.append(f"(?:{re.escape(run)}|{_JOIN})" if is_separator else _JOIN.join(map(re.escape, run)))
    return "".join(parts)


def _matches(pattern: str, haystack: str) -> Iterator[re.Match[str]]:
    """逐个命中,只留词边界成立的(与 matching.bounded_at 同一条规则)。"""
    compiled = re.compile(pattern)
    match = compiled.search(haystack)
    while match is not None:
        if bounded_at(haystack, match.start(), match.group()):
            yield match
        match = compiled.search(haystack, match.start() + 1)


def _letters(text: str) -> str:
    return "".join(c for c in text if c not in _SEPARATORS)


def _is_common_word(word: str) -> bool:
    return zipf_frequency(word, "en", wordlist="small") >= _COMMON_WORD_ZIPF


def _normalized_hit_for(alias: str, name: str, body: _Folded) -> _Hit | None:
    match = next(_matches(_name_regex(name), body.folded), None)
    return None if match is None else body.hit(match, alias, "normalized", 0)


def _fuzzy_hit_for(alias: str, name: str, body: _Folded) -> _Hit | None:
    letters = _letters(name)
    if len(letters) < _MIN_TAIL_LETTERS or not all(is_spaced_script_char(c) for c in letters):
        return None  # 短名字不认尾巴;含中文等不以空格分词文字的名字不模糊(按子串)
    for match in _matches(_name_regex(name) + _TAIL, body.folded):
        word = _letters(match.group())
        if not _is_common_word(word):
            return body.hit(match, alias, "fuzzy", len(word) - len(letters))
    return None


def _earliest(hits: Iterator[_Hit | None]) -> _Hit | None:
    return min((h for h in hits if h is not None), key=lambda h: h.position, default=None)


def find_mention(text: str, brand: BrandSpec, locale: str) -> _Hit | None:
    """一段文字里最早的一处品牌提及:三道关全跑。回答走这里,出题质检(engine/drafting.py)也走这里,口径同一份。"""
    keep_marks = _keeps_marks(locale)
    body = _folded(text, keep_marks)
    named = [(alias, _folded(alias, keep_marks).folded.strip(_SEPARATORS)) for alias in _names(brand)]
    named = [(alias, name) for alias, name in named if name]
    passes = (
        _exact_hit(text, brand),
        _earliest(_normalized_hit_for(alias, name, body) for alias, name in named),
        _earliest(_fuzzy_hit_for(alias, name, body) for alias, name in named),
    )
    hits = [h for h in passes if h is not None]
    return min(hits, key=lambda h: (h.position, _KIND_ORDER[h.kind])) if hits else None


class FuzzyBrandMentionDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        """回答文本中是否提及品牌(三道关取最早的一处)。没有有效回答时 None——缺席就是缺席。"""
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        hit = find_mention(run.answer.answer_text, brand, run.locale)
        common = dict(run_id=run.run_id, detector_name=self.name, detector_version=self.version,
                      computed_at=datetime.now(UTC))
        if hit is None:
            return BrandMentionObservation(mentioned=False, **common)
        return BrandMentionObservation(
            mentioned=True, matched_alias=hit.alias, first_position=hit.position,
            matched_text=hit.text, match_kind=hit.kind, edit_distance=hit.distance, **common,
        )
