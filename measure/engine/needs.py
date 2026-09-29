"""需求辅助生成(第 17 章):盲起草三步序 + 机械质检。

顺序即防线(17.3):
  ① 中立题先行(真盲)——生成 prompt 不含品牌名/别名/网站,生成器不知道谁是"你";
  ② 竞品建议(用带联网检索的 surface)——排除焦点品牌;
  ③ brand_probe 最后注入焦点名,双面措辞。

机械质检(17.4)内嵌在各步的有界重试里:配比核对、JSON 合法、判重、
别名撞车扫描(中立题含焦点品牌即打回重生成)。仍失败显式抛错,不静默降级。

生成调用是辅助行为,统一走 engine/ai_task.py:不进任何指标分母(与 variants.py 同一纪律)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from adapters.base import SurfaceAdapter
from engine.ai_task import Ledger, extract_json, run_task
from engine.detectors.matching import find_first_alias
from engine.models import (
    BrandSpec,
    CompetitorSuggestion,
    FrozenModel,
    NeedIntent,
)

log = structlog.get_logger("engine.needs")


# 17.2 建议配比 3:2:2:1 → 默认候选池 discovery 6 / comparison 4 / scenario 4 / probe 2
DEFAULT_NEUTRAL_COUNTS: dict[NeedIntent, int] = {
    NeedIntent.DISCOVERY: 6,
    NeedIntent.COMPARISON: 4,
    NeedIntent.SCENARIO: 4,
}
DEFAULT_PROBE_COUNT = 2
DEFAULT_COMPETITOR_K = 8


class NeedCandidate(FrozenModel):
    """一条候选需求(待用户在确认页勾选/改写)。"""

    intent: NeedIntent
    text: str


# ── 生成 prompt(硬规则与第 17 章逐条对应;含字面花括号,用 replace 注入)──

_NEUTRAL_PROMPT = """你在为一个产品品类起草中立的市场调研问题——真实买家会拿去问 AI 助手的那种。
你被刻意隔离:不知道委托方是哪个品牌,也不要问。

品类:{category}
语言:{locale}

恰好生成:discovery {n_discovery} 条、comparison {n_comparison} 条、scenario {n_scenario} 条。
- discovery:开放式"什么/哪家最好、最推荐"——覆盖不同买家画像(新手、资深、预算敏感、
  高端、规格/规模、服务保障、耐用性等),两两不重复。
- comparison:"主流品牌在 X 维度上怎么比"——每条一个真实存在多品牌竞争的不同维度。
- scenario:六(或指定数)个彼此不同的、真实买家会遇到的具体场景。

硬规则:
- 绝不点名任何品牌、公司或产品线。
- 不堆叠只有单一品牌才能满足的限定条件(隐性偏置)。
- 真实买家口吻;一题只问一个意图;不预设答案("为什么 X 最好"属违规)。
- 严格限定在品类「{category}」之内。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"discovery":["..."],"comparison":["..."],"scenario":["..."]}"""

_PROBE_PROMPT = """为下面这一个品牌恰好起草 {count} 条「品牌探针」问题(测 AI 如何描述它)。

品牌:{brand}
品类:{category}
语言:{locale}

覆盖:整体口碑/质量、与同类相比如何、用户/社区评价、性价比。
硬规则:
- 每条只点名 {brand},绝不点名任何竞品。
- 措辞双面中立——同时邀请优点与缺点。禁止单边引导("为什么 {brand} 最好"/"{brand} 是不是很差"都违规)。
- 真实买家口吻;一题一个意图。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"brand_probe":["..."]}"""

_COMPETITOR_PROMPT = """利用联网检索,列出品类「{category}」下最主流的真实竞品品牌,
排除「{brand}」(委托方自己)。每个给出常用名称与官方网站主域名。
只要真实、知名的品牌,不得编造。

只返回合法 JSON(至多 {k} 条),不要 markdown,不要解释,形如:
{"competitors":[{"name":"...","domain":"example.com"}]}"""

# ── en-US 语境的英文版(同一套硬规则与质检,16.5)────────────────────────

_NEUTRAL_PROMPT_EN = """You are drafting neutral market-research questions about a product category —
the kind a real buyer asks an AI assistant. You are deliberately NOT given any
specific brand. Do not ask for one.

Category: {category}
Language: {locale}

Draft EXACTLY: discovery {n_discovery}, comparison {n_comparison}, scenario {n_scenario}.
- discovery: open "what/which is best/recommended for…" across different buyer
  personas (beginner, experienced, budget, premium, scale, service). No duplicates.
- comparison: "how do top brands compare on X" — each a DIFFERENT axis that
  multiple brands genuinely compete on.
- scenario: distinct, realistic buyer situations within the category.

HARD RULES:
- NEVER name any brand, company, or product line.
- Do not stack qualifiers that only ONE brand could satisfy (covert bias).
- Real buyer voice; one intent per question; no presupposed answer ("why is X best").
- Stay strictly inside the category "{category}".

Return ONLY valid JSON, no markdown, no comments, exactly:
{"discovery":["..."],"comparison":["..."],"scenario":["..."]}"""

_PROBE_PROMPT_EN = """Draft EXACTLY {count} brand-probe questions about ONE specific brand
(they test how an AI describes it).

Brand: {brand}
Category: {category}
Language: {locale}

Cover: overall reputation/quality, comparison with peers, community opinion, value for money.
HARD RULES:
- Each question names ONLY {brand}. NEVER name a competitor.
- Framing TWO-SIDED and neutral — explicitly invite BOTH strengths and weaknesses.
- Real buyer voice; one intent each.

Return ONLY valid JSON, no markdown, no comments:
{"brand_probe":["..."]}"""

_COMPETITOR_PROMPT_EN = """Using web search, list the most prominent REAL competitor brands in the
"{category}" category, EXCLUDING {brand} (the client's own brand).
Give each brand's common name and primary official website domain.
Only real, well-known brands. Do not invent.

Return ONLY valid JSON (at most {k} items), no markdown, no comments:
{"competitors":[{"name":"...","domain":"example.com"}]}"""


def _is_english(locale: str) -> bool:
    return locale.lower().startswith("en")


# ── 通用:让 AI 办件事(engine/ai_task.py)→ 抠 JSON → 校验,不合格重问 ─────────────


def _ask_json(
    adapter: SurfaceAdapter,
    prompt: str,
    check: Callable[[Any], None],
    *,
    locale: str,
    ledger: Ledger,
) -> Any:
    def parse(text: str) -> Any:
        data = extract_json(text)
        check(data)
        return data

    return run_task(adapter, prompt, parse, label="需求生成", need_id="__need_generation__", locale=locale,
                    ledger=ledger).value


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("期望字符串数组")
    return [item.strip() for item in value if item.strip()]


# ── 质检原语(17.4)───────────────────────────────────────────────────


def _brand_tokens(brand: BrandSpec) -> list[str]:
    tokens = [brand.name, *brand.aliases, *brand.domains]
    return [t.strip().lower() for t in tokens if t.strip()]


def manual_intent(text: str, brand: BrandSpec) -> NeedIntent:
    """用户手填的问题归哪个场景:点了品牌名或别名的就是「直接询问品牌」,否则按「没有点名品牌」。
    点名的问题里品牌提及率天然接近 100%,混进「没点名」场景会把「AI 会主动推荐你」的结论虚高。"""
    return NeedIntent.BRAND_PROBE if find_first_alias(text, brand) is not None else NeedIntent.DISCOVERY


def brand_collisions(texts: list[str], brand: BrandSpec) -> list[str]:
    """别名撞车扫描:返回含焦点品牌名/别名/域名的文本(中立题命中即违规)。"""
    tokens = _brand_tokens(brand)
    return [text for text in texts if any(token in text.lower() for token in tokens)]


def _dedupe(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


# ── 三步序 ────────────────────────────────────────────────────────────


def _draft_neutral(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str,
    counts: dict[NeedIntent, int],
    ledger: Ledger,
) -> list[NeedCandidate]:
    """① 真盲:prompt 不含任何品牌信息;质检(配比+撞车)失败即整批重生成。"""
    template = _NEUTRAL_PROMPT_EN if _is_english(locale) else _NEUTRAL_PROMPT
    prompt = (
        template
        .replace("{category}", category)
        .replace("{locale}", locale)
        .replace("{n_discovery}", str(counts[NeedIntent.DISCOVERY]))
        .replace("{n_comparison}", str(counts[NeedIntent.COMPARISON]))
        .replace("{n_scenario}", str(counts[NeedIntent.SCENARIO]))
    )
    keys = {NeedIntent.DISCOVERY: "discovery", NeedIntent.COMPARISON: "comparison",
            NeedIntent.SCENARIO: "scenario"}

    def check(data: Any) -> None:
        for intent, key in keys.items():
            texts = _str_list(data[key])
            if len(texts) != counts[intent]:
                raise ValueError(f"{key} 应恰为 {counts[intent]} 条,实得 {len(texts)}")
        all_texts = [t for key in keys.values() for t in _str_list(data[key])]
        if len(_dedupe(all_texts)) != len(all_texts):
            raise ValueError("中立题存在重复")
        leaked = brand_collisions(all_texts, brand)
        if leaked:
            raise ValueError(f"中立题泄漏焦点品牌(违反真盲):{leaked[:2]}")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    return [
        NeedCandidate(intent=intent, text=text)
        for intent, key in keys.items()
        for text in _str_list(data[key])
    ]


def suggest_competitors(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    k: int = DEFAULT_COMPETITOR_K,
    locale: str = "zh-CN",
    ledger: Ledger,
) -> list[CompetitorSuggestion]:
    """② 竞品建议:建议用带联网检索的 surface 调用;排除焦点、去重、封顶 k。"""
    template = _COMPETITOR_PROMPT_EN if _is_english(locale) else _COMPETITOR_PROMPT
    prompt = (
        template
        .replace("{category}", category)
        .replace("{brand}", brand.name)
        .replace("{k}", str(k))
    )

    def check(data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("competitors"), list):
            raise ValueError("competitors 须为数组")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    focal = brand.name.strip().lower()
    seen: set[str] = set()
    out: list[CompetitorSuggestion] = []
    for item in data["competitors"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        domain = str(item.get("domain", "")).strip()
        key = name.lower()
        if not name or not domain or key == focal or key in seen:
            continue
        seen.add(key)
        out.append(CompetitorSuggestion(name=name, domain=domain))
        if len(out) >= k:
            break
    return out


def _draft_probes(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str,
    count: int,
    ledger: Ledger,
) -> list[NeedCandidate]:
    """③ 品牌探针:此时才把焦点名交给生成器;探针必须点名焦点品牌。"""
    template = _PROBE_PROMPT_EN if _is_english(locale) else _PROBE_PROMPT
    prompt = (
        template
        .replace("{count}", str(count))
        .replace("{brand}", brand.name)
        .replace("{category}", category)
        .replace("{locale}", locale)
    )

    def check(data: Any) -> None:
        texts = _str_list(data["brand_probe"])
        if len(texts) != count:
            raise ValueError(f"探针题应恰为 {count} 条,实得 {len(texts)}")
        missing = [t for t in texts if brand.name.lower() not in t.lower()]
        if missing:
            raise ValueError(f"探针题未点名焦点品牌:{missing[:2]}")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    return [NeedCandidate(intent=NeedIntent.BRAND_PROBE, text=text)
            for text in _str_list(data["brand_probe"])]


def draft_need_candidates(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str = "zh-CN",
    neutral_counts: dict[NeedIntent, int] | None = None,
    probe_count: int = DEFAULT_PROBE_COUNT,
    competitor_k: int = DEFAULT_COMPETITOR_K,
    ledger: Ledger,
) -> tuple[list[NeedCandidate], list[CompetitorSuggestion]]:
    """盲起草三步序编排。候选供确认页展示;用户勾选/改写后才成为 InformationNeed。"""
    counts = neutral_counts or DEFAULT_NEUTRAL_COUNTS
    neutral = _draft_neutral(adapter, category=category, brand=brand, locale=locale, counts=counts,
                             ledger=ledger)
    competitors = suggest_competitors(adapter, category=category, brand=brand,
                                      k=competitor_k, locale=locale, ledger=ledger)
    probes = _draft_probes(adapter, category=category, brand=brand, locale=locale,
                           count=probe_count, ledger=ledger)
    candidates = neutral + probes
    log.info("need_candidates_drafted", category=category,
             neutral=len(neutral), probes=len(probes), competitors=len(competitors))
    return candidates, competitors
