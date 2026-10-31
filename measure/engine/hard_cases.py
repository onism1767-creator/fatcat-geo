"""校准难题集(BR1;调研报告 docs/研究/品牌名识别-方法调研-2026-10-28.md 第 5.2 节):
程序造「长得像你、其实不是你」的写法和配套正例,答案由构造决定,不靠人工。

只量判法会不会犯这类错,不是真实回答上错多少(CheckList 的提醒);真实底本要跑几单难品牌(BR3)。
难名字和常用词是配置资产(config/calibration_hard.yaml),机械变形在这里:韩文收音、日文浊点、越南文声调、跨标点。
BR3 的题目也在这里读(config/calibration_real.yaml):题怎么出决定答案,不靠人工。
本模块无厂商名,不连库。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml

from engine.models import FrozenModel, Prompt, PromptIntent, PromptOrigin

DEFAULT_HARD_PATH = Path(__file__).resolve().parents[1] / "config" / "calibration_hard.yaml"
DEFAULT_REAL_PATH = Path(__file__).resolve().parents[1] / "config" / "calibration_real.yaml"
_REAL_PROMPTS = uuid5(NAMESPACE_URL, "geo-toast/calibration-real")
NAMING = "点名"
REAL_KINDS = {PromptIntent.BRAND_FACT: NAMING, PromptIntent.PROBLEM_SOLUTION: "反例",
              PromptIntent.CATEGORY_DISCOVERY: "不点名"}  # 题型 → 校准里的说法

_HANGUL_FIRST, _HANGUL_COUNT, _FINALS = 0xAC00, 11172, 28  # 韩文音节 = 声母 × 588 + 韵母 × 28 + 收音
_ADDED_FINALS = (17, 4, 16)  # 没有收音时加的收音:ㅂ、ㄴ、ㅁ(기아 → 기압、기안、기암)
_KANA = ("぀", "ヿ")
_KANA_MARKS = ("", "゙", "゚")  # 不带、浊点、半浊点
_TONES = ("̀", "́", "̃", "̉", "̣")  # 越南文五个声调符号(分解形式)
_PUNCTUATION = (", ", ". ", "; ")


class HardBrand(FrozenModel):
    """一个难名字:当成被测品牌,塞它的难题和正例。字段说明见 config/calibration_hard.yaml 文件头。"""

    name: str
    aliases: list[str] = []
    skip_bases: list[str] = []
    negatives: dict[str, list[str]] = {}
    positives: dict[str, list[str]] = {}
    tone_language: str | None = None
    category: str = ""
    peers: list[str] = []


class HardCase(FrozenModel):
    kind: str
    text: str
    expected: bool  # 塞进去之后「提到了」的正确答案
    locale: str | None = None  # 这条按哪种回答语言判(越南文的口径看语言,Q2);None = 底本原样


class RealBrand(FrozenModel):
    """一个要拿真实回答的难名字(BR3)。字段说明见 config/calibration_real.yaml 文件头。"""

    name: str
    aliases: list[str] = []
    locale: str
    category: str = ""
    peers: list[str] = []
    naming: list[str]
    counter: list[str]
    open: list[str]


def load_hard_brands(path: Path = DEFAULT_HARD_PATH) -> list[HardBrand]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [HardBrand.model_validate(entry) for entry in data["brands"]]


def load_real_brands(path: Path = DEFAULT_REAL_PATH) -> list[RealBrand]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [RealBrand.model_validate(entry) for entry in data["brands"]]


def prompts_of(brand: RealBrand) -> list[Prompt]:
    """一个难名字的全部题。题号由品牌和题面算出来:再跑一遍还是这几道题,已问过的不重问;改了题面就是新题。"""
    def prompt(text: str, intent: PromptIntent) -> Prompt:
        return Prompt(prompt_id=str(uuid5(_REAL_PROMPTS, f"{brand.name}\n{text}")), text=text, intent=intent,
                      origin=PromptOrigin.ASSISTED, locale=brand.locale)

    return ([prompt(text, PromptIntent.BRAND_FACT) for text in brand.naming]
            + [prompt(text, PromptIntent.PROBLEM_SOLUTION) for text in brand.counter]
            + [prompt(text, PromptIntent.CATEGORY_DISCOVERY) for text in brand.open])


def strip_marks(name: str) -> str:
    """去掉全部附加符号(Citroën → Citroen、Phở Hòa → Pho Hoa)。"""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))


def korean_neighbours(name: str) -> list[str]:
    """最后一个音节加或去收音:기아 → 기압、기안、기암;삼성 → 삼서。不是韩文音节结尾的名字没有。"""
    index = ord(name[-1]) - _HANGUL_FIRST
    if not 0 <= index < _HANGUL_COUNT:
        return []
    final = index % _FINALS
    bare = ord(name[-1]) - final
    return [name[:-1] + chr(bare + added) for added in (_ADDED_FINALS if final == 0 else (0,))]


def kana_neighbours(name: str) -> list[str]:
    """逐个假名换浊点 / 半浊点:ドコモ → トコモ、ドゴモ。"""
    out: list[str] = []
    for i, ch in enumerate(name):
        if not _KANA[0] <= ch <= _KANA[1]:
            continue
        bare = unicodedata.normalize("NFD", ch)[0]
        for mark in _KANA_MARKS:
            changed = unicodedata.normalize("NFC", bare + mark)
            if len(changed) == 1 and changed != ch:  # 没有对应浊音的假名合不成一个字,跳过
                out.append(name[:i] + changed + name[i + 1:])
    return out


def tone_neighbour(name: str) -> str | None:
    """换掉第一个声调:Phở Hòa → Phờ Hòa(越南文里是另一个词)。没有声调的名字没有。"""
    decomposed = unicodedata.normalize("NFD", name)
    for i, c in enumerate(decomposed):
        if c in _TONES:
            other = next(tone for tone in _TONES if tone != c)
            return unicodedata.normalize("NFC", decomposed[:i] + other + decomposed[i + 1:])
    return None


def punctuation_splits(name: str) -> list[str]:
    """多段的名字在段间插标点,再接一个残缺的小写词:Meta AI → Meta, AI、Meta. AI、Meta; AI、Meta, a company;
    Monday.com → Monday, Com…… 中文等不以空格分词的名字没有这个问题,不造。"""
    parts = [part for part in re.split(r"[ .]", name) if part]
    if len(parts) < 2 or not all(part.isascii() and part.isalnum() for part in parts):
        return []
    head, tail = " ".join(parts[:-1]), parts[-1]
    written = tail if tail.isupper() else tail.capitalize()
    return [*(f"{head}{mark}{written}" for mark in _PUNCTUATION), f"{head}, {tail[:-1].lower()} company"]


def cases_of(brand: HardBrand) -> list[HardCase]:
    """一个难名字的全部难题与配套正例:配置里写的 + 程序变形出来的。"""
    cases = [HardCase(kind=kind, text=text, expected=False)
             for kind, texts in brand.negatives.items() for text in texts]
    cases += [HardCase(kind=kind, text=text, expected=True)
              for kind, texts in brand.positives.items() for text in texts]
    cases += [HardCase(kind="韩文近形", text=text, expected=False) for text in korean_neighbours(brand.name)]
    cases += [HardCase(kind="日文近形", text=text, expected=False) for text in kana_neighbours(brand.name)]
    cases += [HardCase(kind="跨标点", text=text, expected=False) for text in punctuation_splits(brand.name)]
    swapped = tone_neighbour(brand.name)
    if brand.tone_language is not None and swapped is not None:  # 越南文按回答语言分(用户 2026-10-29 定 Q2)
        bare, language = strip_marks(brand.name), brand.tone_language
        cases += [HardCase(kind="越南文换声调", text=swapped, expected=False, locale=language),
                  HardCase(kind="越南文去声调(越南语单)", text=bare, expected=False, locale=language),
                  HardCase(kind="越南文去声调(别的语言单)", text=bare, expected=True, locale="en-US")]
    return cases
