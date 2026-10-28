"""品牌提及判定器·模糊版 v2(S9.2,中档;架构书附录 v0.4.27 第 4 节 + 用户 2026-10-14 拍板的「规则 + 模糊匹配」路线;
v2 = 2026-10-27 审核黄 9 用户定稿「只认尾巴不同 + 去重音」)。

三道关,全部跑完取回答里最早出现的那一处(同一位置按 精确 > 规整 > 模糊):
1. 精确:与规则判定器(v2)同一套原语——别名表 + 大小写不敏感,拉丁字母按词边界、中文按子串;
2. 规整:逐字 NFKD + 去重音(é → e、ë → e;全角 → 半角)+ 小写,去掉名字与正文里的空格 / 连字符 / 点号之类的分隔再比
   (「极光 CRM」「Aurora-CRM」「ＡｕｒｏｒａＣＲＭ」「Citroen」对「Citroën」都认),词边界规则照旧;
3. 模糊:只对纯拉丁 / 数字的名字,只在正文里「以空格分词的词」拼出的候选片段上比,**只认尾巴不同**——候选 = 名字再多或少几个
   尾字符(复数 / 所有格 / 形容词尾:「Notions」「auroracrms」「Patagonian」认);中间换一个字母就是别的词
   (「Colombia」不是「Columbia」,「AuroraCRN」也不认——AI 回答几乎不会把品牌名中间拼错,
   860 条真实回答里这类容错一次没派上用场)。
   允许差几个尾字符按名字长度:≤ 5 个字符不模糊(短名字一改就是别的词),6–9 允许差 1,≥ 10 允许差 2;
   候选还必须和名字同前三个字符。中文等不以空格分词的文字不做模糊(按子串,用户定)。
结果另记命中片段与认法(match_kind / matched_text / edit_distance = 尾巴差几个字符),供单条回答页;与规则判定器并排存档
(各带 detector_name / version),两者的分歧率留给自动校准(S9.4)。门槛是本模块常量,校准后调;不装模型(GLiNER 留到校准时试)。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from engine.detectors.matching import find_first_alias, is_spaced_script_char
from engine.models import BrandMentionObservation, BrandSpec, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "brand_mention_fuzzy"
DETECTOR_VERSION = 2

# 规整时去掉的分隔符(NFKC 之后全角空格、全角连字符等已变成这些半角字符)
_SEPARATORS = frozenset(" \t\r\n-_.·•・/\\'’\"")
_PREFIX_CHARS = 3  # 模糊候选必须和名字同前几个字符
_KIND_ORDER = {"exact": 0, "normalized": 1, "fuzzy": 2}


def edit_budget(length: int) -> int:
    """名字(规整后)多长允许差几个字母:短名字不模糊。校准(S9.4)后按实测调。"""
    if length <= 5:
        return 0
    return 1 if length <= 9 else 2


@dataclass(frozen=True)
class _Hit:
    position: int  # 原文字符偏移
    alias: str  # 命中的登记名 / 别名(原样)
    text: str  # 原文里的命中片段
    kind: str  # exact / normalized / fuzzy
    distance: int


def _chars(text: str) -> list[tuple[str, int]]:
    """逐字规整(NFKD 拆出重音符号后丢掉 + 全角转半角 + 小写),每个规整后的字符记住来自原文哪个位置——偏移永远指回原文。"""
    out: list[tuple[str, int]] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch).lower():
            if unicodedata.combining(c):
                continue  # 去重音:Citroën / Citroen、Moët / Moet 是同一个名字(v2,用户 2026-10-27 定)
            out.append((c, i))
    return out


def _collapse(alias: str) -> str:
    return "".join(c for c, _ in _chars(alias) if c not in _SEPARATORS)


def _names(brand: BrandSpec) -> list[str]:
    return [alias for alias in dict.fromkeys([brand.name, *brand.aliases]) if alias.strip()]


def _exact_hit(text: str, brand: BrandSpec) -> _Hit | None:
    found = find_first_alias(text, brand)
    if found is None:
        return None
    position, alias = found
    return _Hit(position, alias, text[position:position + len(alias.strip())], "exact", 0)


def _collapsed(chars: list[tuple[str, int]]) -> list[tuple[str, int, bool]]:
    """去掉分隔符后的正文:(字符, 原文位置, 前面是否隔着分隔符)。去掉的分隔符仍算词边界,
    「Try Aurora-CRM」里 Try 和 Aurora 之间的空格没了也不能把它们当成一个词。"""
    out: list[tuple[str, int, bool]] = []
    separated = True
    for c, i in chars:
        if c in _SEPARATORS:
            separated = True
            continue
        out.append((c, i, separated))
        separated = False
    return out


def _bounded_collapsed(collapsed: list[tuple[str, int, bool]], start: int, length: int) -> bool:
    """与 matching.bounded_at 同一条规则(只对以空格分词的字符查词边界),外加「中间隔着分隔符就算边界」。"""
    end = start + length
    if start > 0 and not collapsed[start][2]:
        if is_spaced_script_char(collapsed[start][0]) and is_spaced_script_char(collapsed[start - 1][0]):
            return False
    if end < len(collapsed) and not collapsed[end][2]:
        if is_spaced_script_char(collapsed[end - 1][0]) and is_spaced_script_char(collapsed[end][0]):
            return False
    return True


def _normalized_hit(text: str, chars: list[tuple[str, int]], brand: BrandSpec) -> _Hit | None:
    collapsed = _collapsed(chars)
    haystack = "".join(c for c, _, _ in collapsed)
    best: _Hit | None = None
    for alias in _names(brand):
        needle = _collapse(alias)
        if not needle:
            continue
        pos = haystack.find(needle)
        while pos >= 0 and not _bounded_collapsed(collapsed, pos, len(needle)):
            pos = haystack.find(needle, pos + 1)
        if pos < 0:
            continue
        start, end = collapsed[pos][1], collapsed[pos + len(needle) - 1][1] + 1
        if best is None or start < best.position:
            best = _Hit(start, alias, text[start:end], "normalized", 0)
    return best


def _tokens(chars: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """正文里以空格分词的词(拉丁字母 / 数字连成的一段):(规整后的词, 原文起点, 原文终点)。"""
    tokens: list[tuple[str, int, int]] = []
    word, start, end = "", -1, -1
    for c, i in chars:
        if is_spaced_script_char(c):
            if not word:
                start = i
            word, end = word + c, i + 1
        elif word:
            tokens.append((word, start, end))
            word = ""
    if word:
        tokens.append((word, start, end))
    return tokens


def _tail_distance(candidate: str, needle: str) -> int | None:
    """只认尾巴不同:候选 = 名字再多几个尾字符(复数 / 所有格 / 形容词尾),或名字少几个尾字符;
    返回尾巴差几个字符。中间差一个字母就是别的词(Colombia 不是 Columbia)→ None,不认。"""
    if candidate.startswith(needle) or needle.startswith(candidate):
        return abs(len(candidate) - len(needle))
    return None


def _fuzzy_hit_for(alias: str, text: str, tokens: list[tuple[str, int, int]]) -> _Hit | None:
    needle = _collapse(alias)
    budget = edit_budget(len(needle))
    if not budget or not all(is_spaced_script_char(c) for c in needle):
        return None  # 短名字不模糊;含中文等不以空格分词文字的名字不模糊(按子串)
    for i, (token, start, _) in enumerate(tokens):
        if token[:_PREFIX_CHARS] != needle[:_PREFIX_CHARS]:
            continue
        candidate = ""
        for j in range(i, len(tokens)):  # 从这个词起往后拼,拼到比名字长出预算就停
            candidate += tokens[j][0]
            if len(candidate) > len(needle) + budget:
                break
            if len(candidate) < len(needle) - budget:
                continue
            distance = _tail_distance(candidate, needle)
            if distance is not None and distance <= budget:
                return _Hit(start, alias, text[start:tokens[j][2]], "fuzzy", distance)
    return None


def _fuzzy_hit(text: str, chars: list[tuple[str, int]], brand: BrandSpec) -> _Hit | None:
    tokens = _tokens(chars)
    hits = [hit for alias in _names(brand) if (hit := _fuzzy_hit_for(alias, text, tokens)) is not None]
    return min(hits, key=lambda h: (h.position, h.distance)) if hits else None


class FuzzyBrandMentionDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        """回答文本中是否提及品牌(三道关取最早的一处)。没有有效回答时 None——缺席就是缺席。"""
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        text = run.answer.answer_text
        chars = _chars(text)
        passes = (_exact_hit(text, brand), _normalized_hit(text, chars, brand), _fuzzy_hit(text, chars, brand))
        hits = [h for h in passes if h is not None]
        common = dict(run_id=run.run_id, detector_name=self.name, detector_version=self.version,
                      computed_at=datetime.now(UTC))
        if not hits:
            return BrandMentionObservation(mentioned=False, **common)
        hit = min(hits, key=lambda h: (h.position, _KIND_ORDER[h.kind]))
        return BrandMentionObservation(
            mentioned=True, matched_alias=hit.alias, first_position=hit.position,
            matched_text=hit.text, match_kind=hit.kind, edit_distance=hit.distance, **common,
        )
