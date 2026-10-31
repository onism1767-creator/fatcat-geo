"""有风险的名字怎么判(BR4;调研报告 docs/研究/品牌名识别-方法调研-2026-10-28.md 方向 B、E1 + 用户 2026-10-29 定的 Q3)。

字面上出现了名字,不等于提到了你:On、Notion、小米都是普通的字。规则和模糊判定器找到每一处之后都交给这里,结论三选一:
提到了 / 没提到 / 说不准。说不准 = mentioned 为 None,不进分母(和裁判看不到同一规矩,硬边界 7),中档再请裁判。

名字分三档:
- 没风险:写出来就算;
- 词组(每个词都是普通词,如 Meta AI、On Running):只看大小写,要和登记的一样;
- 单词(太短:不足 3 个字母或 3 个汉字;普通词:英文词频表 ≥ 百万分之一):大小写要对,附近还要有线索才算,没线索就说不准。
  词频表分不清 Notion 和 Nike,所以纯品牌名也可能进这一档,代价是个别回答说不准,不会认错。

大小写不对就是没提到,不是说不准:小写的 on、notion 是普通用法,算说不准的话英文回答几乎条条都要请裁判。
中文没有大小写,看名字是不是嵌在别的词里:前后的字和名字拼成词(小米粥、三星级),或者边界切开了别的词(完美|的、美|的确)。
嵌在词里又没线索 = 没提到;嵌在词里但有线索(「选美的空调」里的「选美」)= 说不准;没嵌在词里的走上面的线索规则。
线索 = 同一句或前后一句里有别的登记名、同一张名单里的别家、品类词,或者这条回答写了、引用了官网。
只管中文和英文(用户 2026-10-30 定先适配这两种);韩文、日文的名字不分档。判定器零厂商名,词表随 uv.lock 固定。
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from wordfreq import get_frequency_dict, zipf_frequency

from engine.detectors.matching import bounded_at, is_spaced_script_char, matches_any_brand_domain, sentence_spans
from engine.models import BrandSpec, MentionContext, SurfaceRun

_COMMON_WORD_ZIPF = 3.0  # 词频表刻度:3 = 百万分之一,这个刻度以上的词收在小表里
_MIN_SAFE_LETTERS = 3  # 字母和数字少于这个算太短
_MIN_SAFE_HANZI = 3  # 汉字少于这个算太短:两个字的名字太容易和别的词撞上
_KIND_ORDER = {"exact": 0, "normalized": 1, "fuzzy": 2}
_WORD_SPLIT = re.compile(r"[\s\-_/]+")
_CATEGORY_SPLIT = re.compile(r"[\s,,、;;/|]+")
_CATEGORY_STOPWORDS = frozenset({"and", "the", "for", "with", "brand", "brands", "product", "products", "service",
                                 "services", "tool", "tools"})


class Risk(StrEnum):
    NONE = "none"
    PHRASE = "phrase"
    WORD = "word"


class Reading(StrEnum):
    YES = "yes"
    UNSURE = "unsure"
    NO = "no"


@dataclass(frozen=True)
class Hit:
    position: int  # 原文字符偏移
    alias: str  # 命中的登记名 / 别名(原样)
    text: str  # 原文里的命中片段
    kind: str  # exact / normalized / fuzzy
    distance: int = 0


@dataclass(frozen=True)
class Settled:
    """一条回答的结论:mentioned 为 None 是说不准,hit 是算数的那一处,clue 是靠什么线索算的。"""

    mentioned: bool | None
    hit: Hit | None = None
    clue: str | None = None


def is_common_word(word: str) -> bool:
    return zipf_frequency(word, "en", wordlist="small") >= _COMMON_WORD_ZIPF


def _is_hanzi(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def risk_of(alias: str) -> Risk:
    name = alias.strip()
    if any(_is_hanzi(c) for c in name):
        return Risk.WORD if len(name) < _MIN_SAFE_HANZI else Risk.NONE
    if any(c.isalnum() and not is_spaced_script_char(c) for c in name):
        return Risk.NONE  # 韩文、日文等,这轮不分档
    words = [w for w in _WORD_SPLIT.split(name) if w]
    letters = "".join(c for c in name if c.isalnum())
    if len(letters) < _MIN_SAFE_LETTERS:
        return Risk.WORD
    if len(words) == 1:
        return Risk.WORD if is_common_word(letters.lower()) else Risk.NONE
    return Risk.PHRASE if all(is_common_word(w.lower()) for w in words) else Risk.NONE


def is_risky(brand: BrandSpec) -> bool:
    """这张品牌卡里有没有要靠线索才算的名字:决定中档要不要预留裁判的钱。"""
    return any(risk_of(alias) is Risk.WORD for alias in [brand.name, *brand.aliases] if alias.strip())


def _pieces(text: str) -> list[str]:
    return [w for w in _WORD_SPLIT.split(unicodedata.normalize("NFKC", text)) if w]


def _hit_risk(hit: Hit) -> Risk:
    """名字本身没风险,但回答里是拆开的几个普通词(品牌 Salesforce、回答写 sales force):按单词算。"""
    risk = risk_of(hit.alias)
    pieces = _pieces(hit.text)
    if (risk is Risk.NONE and len(pieces) > 1 and hit.alias.strip().isalnum()
            and all(p.isascii() and is_common_word(p.lower()) for p in pieces)):
        return Risk.WORD
    return risk


def _squeezed(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def _case_fits(hit: Hit, risk: Risk) -> bool:
    """大小写对不对:全大写算对;词组要和登记的一样(On running 不是 On Running);单词原样或每段首字母大写。"""
    written, alias = _squeezed(hit.text), _squeezed(hit.alias)
    if written.isupper() or (risk is Risk.PHRASE and written.startswith(alias)):
        return True
    return risk is Risk.WORD and (written == alias or all(p[0].isupper() for p in _pieces(hit.text)))


def _zh_words() -> dict[str, float]:
    return get_frequency_dict("zh", "small")  # wordfreq 自己缓存


def _zh_compounds() -> dict[str, float]:
    return get_frequency_dict("zh", "large")


def _inside_a_word(text: str, start: int, end: int) -> bool:
    """中文名字是不是嵌在别的词里:前后的字和名字拼成词,或者名字的边界切开了别的词。"""
    name, before, after = text[start:end], text[max(0, start - 1):start], text[end:end + 1]
    return (
        (_is_hanzi(before[:1]) and (before + name in _zh_compounds() or before + name[0] in _zh_words()))
        or (_is_hanzi(after[:1]) and (name + after in _zh_compounds() or name[-1] + after in _zh_words()))
    )


def _written_in(window: str, name: str) -> bool:
    position = window.find(name)
    while position >= 0:
        if bounded_at(window, position, name):
            return True
        position = window.find(name, position + 1)
    return False


def category_terms(category: str) -> list[re.Pattern[str]]:
    """品类里能当线索的词:英文按词、认单复数;中文整段或最后两个字(中心词在后:家用空调 → 空调)。"""
    terms: list[re.Pattern[str]] = []
    for part in (p for p in _CATEGORY_SPLIT.split(category.lower()) if p):
        if any(_is_hanzi(c) for c in part):
            terms += [re.compile(re.escape(t)) for t in dict.fromkeys((part, part[-2:]))]
        elif len(part) >= _MIN_SAFE_LETTERS and part not in _CATEGORY_STOPWORDS:
            stem = part.removesuffix("s") if len(part) > _MIN_SAFE_LETTERS else part
            terms.append(re.compile(rf"(?<![a-z0-9]){re.escape(stem)}(?:s|es)?(?![a-z0-9])"))
    return terms


def _official_clue(run: SurfaceRun, brand: BrandSpec) -> str | None:
    """整条回答的线索:正文写了官网,或者引用了官网(跳转仪器的真实域名在 title 里)。"""
    assert run.answer is not None
    lowered = run.answer.answer_text.lower()
    for domain in brand.domains:
        if domain.lower() in lowered:
            return domain
    for citation in run.answer.citations:
        for host in (citation.domain, citation.title or ""):
            if matches_any_brand_domain(host, brand):
                return host
    return None


class _Reader:
    """读一条回答:算一次的东西(分句、官网线索、品类词)存着,逐处判。"""

    def __init__(self, run: SurfaceRun, brand: BrandSpec, context: MentionContext) -> None:
        assert run.answer is not None
        self._text = run.answer.answer_text
        self._names = [a.strip() for a in dict.fromkeys([brand.name, *brand.aliases]) if a.strip()]
        self._peers = context.peers
        self._terms = category_terms(context.category)
        self._official = _official_clue(run, brand)
        self._spans = sentence_spans(self._text)
        self._starts = [start for start, _ in self._spans]

    def _window(self, position: int) -> str:
        index = max(0, bisect.bisect_right(self._starts, position) - 1)
        first, last = max(0, index - 1), min(len(self._spans) - 1, index + 1)
        return self._text[self._spans[first][0]:self._spans[last][1]]

    def _clue(self, hit: Hit) -> str | None:
        if self._official is not None:
            return self._official
        window, alias = self._window(hit.position), hit.alias.strip().lower()
        for name in self._names:
            if name.lower() != alias and _written_in(window, name):
                return name
        for peer in self._peers:  # 别家名字里本来就有你的名字(撞名)时不算线索
            if alias not in peer.lower() and _written_in(window, peer):
                return peer
        lowered = window.lower()
        for term in self._terms:
            match = term.search(lowered)
            if match is not None:
                return match.group()
        return None

    def read(self, hit: Hit) -> tuple[Reading, str | None]:
        risk = _hit_risk(hit)
        if risk is Risk.NONE:
            return Reading.YES, None
        start, end = hit.position, hit.position + len(hit.text)
        if any(_is_hanzi(c) for c in hit.text):
            clue = self._clue(hit)
            if _inside_a_word(self._text, start, end):
                return (Reading.UNSURE, None) if clue else (Reading.NO, None)
            return (Reading.YES, clue) if clue else (Reading.UNSURE, None)
        if not _case_fits(hit, risk):
            return Reading.NO, None
        if risk is Risk.PHRASE:
            return Reading.YES, None
        clue = self._clue(hit)
        return (Reading.YES, clue) if clue else (Reading.UNSURE, None)


def hit_order(hit: Hit) -> tuple[int, int]:
    return hit.position, _KIND_ORDER[hit.kind]


def settle(hits: Iterable[Hit], run: SurfaceRun, brand: BrandSpec, context: MentionContext) -> Settled:
    """逐处判:有一处算提到就是提到了(取最早的);一处都不算,但有说不准的,就是说不准;全不算才是没提到。"""
    ordered = sorted(hits, key=hit_order)
    if not ordered:
        return Settled(False)
    reader = _Reader(run, brand, context)
    unsure: Hit | None = None
    for hit in ordered:
        reading, clue = reader.read(hit)
        if reading is Reading.YES:
            return Settled(True, hit, clue)
        if reading is Reading.UNSURE and unsure is None:
            unsure = hit
    return Settled(None, unsure) if unsure is not None else Settled(False)
