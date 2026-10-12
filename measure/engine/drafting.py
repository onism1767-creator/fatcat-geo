"""出题(产品文档 15.2 的五类 prompt;第 17 章盲起草三步序 + 机械质检;v0.4.26 S2)。

默认每单 24 道:品类发现 8 · 问题与方案 4 · 推荐 4 · 对比与替代 4 · 品牌事实 4。
顺序即防线(17.3):
  ① 不点名的三类先行(真盲)——生成 prompt 不含品牌名/别名/网站,生成器不知道谁是"你";
  ② 竞品建议(用带联网检索的 surface)——排除焦点品牌;
  ③ 对比与替代:可点名②里的竞品,仍绝不点名焦点品牌;
  ④ 品牌事实最后注入焦点名,双面措辞。

机械质检(17.4)内嵌在各步的有界重试里:数量核对、JSON 合法、判重、
别名撞车扫描(不点名的题含焦点品牌即打回重生成)。仍失败显式抛错,不静默降级。

生成调用是辅助行为,统一走 engine/ai_task.py:不进任何指标分母。
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
    PromptIntent,
    PromptOrigin,
)

log = structlog.get_logger("engine.drafting")

# 产品文档 15.2 的默认配比(每个型号 24 条)
DEFAULT_COUNTS: dict[PromptIntent, int] = {
    PromptIntent.CATEGORY_DISCOVERY: 8,
    PromptIntent.PROBLEM_SOLUTION: 4,
    PromptIntent.RECOMMENDATION: 4,
    PromptIntent.COMPARISON_ALTERNATIVE: 4,
    PromptIntent.BRAND_FACT: 4,
}
DEFAULT_COMPETITOR_K = 8
_BLIND_INTENTS = (PromptIntent.CATEGORY_DISCOVERY, PromptIntent.PROBLEM_SOLUTION, PromptIntent.RECOMMENDATION)


class PromptCandidate(FrozenModel):
    """一道候选题(待用户在确认页勾选/改写)。"""

    intent: PromptIntent
    text: str
    origin: PromptOrigin  # 系统起草 / 用户手填


# ── 生成 prompt(硬规则与第 17 章逐条对应;含字面花括号,用 replace 注入)──

_BLIND_PROMPT = """你在为一个产品品类起草真实买家会拿去问 AI 助手的问题。
你被刻意隔离:不知道委托方是哪个品牌,也不要问。

品类:{category}
语言:{locale}

恰好生成三类:
- category_discovery {n_discovery} 条(品类发现):开放式"这类里最好的 / 最值得买的有哪些"——覆盖不同买家画像
  (新手、资深、预算敏感、高端、规格/规模、服务保障、耐用性等),两两不重复。
- problem_solution {n_problem} 条(问题与方案):真实买家遇到的具体问题或场景,问"怎么解决 / 怎么选"。
- recommendation {n_recommendation} 条(推荐):直接请 AI 给出推荐,"你会推荐什么 / 帮我推荐"。

硬规则:
- 绝不点名任何品牌、公司或产品线。
- 不堆叠只有单一品牌才能满足的限定条件(隐性偏置)。
- 真实买家口吻;一题只问一个意图;不预设答案("为什么 X 最好"属违规)。
- 严格限定在品类「{category}」之内。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"category_discovery":["..."],"problem_solution":["..."],"recommendation":["..."]}"""

_COMPARISON_PROMPT = """为品类「{category}」恰好起草 {count} 条「对比与替代」问题。

语言:{locale}
可以点名的竞品(真实存在):{competitors}

覆盖:某个竞品的替代品有哪些;两个竞品怎么选;某竞品值不值得买、有没有更好的选择。
硬规则:
- 只能点名上面列出的竞品,不得点名其他任何品牌;一半左右的题可以不点名任何品牌(泛泛问"哪些替代方案")。
- 真实买家口吻;一题一个意图;不预设答案。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"comparison_alternative":["..."]}"""

_BRAND_FACT_PROMPT = """为下面这一个品牌恰好起草 {count} 条「品牌事实」问题(测 AI 怎么介绍它)。

品牌:{brand}
品类:{category}
语言:{locale}

覆盖:它是做什么的 / 主打产品;整体口碑与质量;与同类相比如何;性价比与适合谁。
硬规则:
- 每条只点名 {brand},绝不点名任何竞品。
- 措辞双面中立——同时邀请优点与缺点。禁止单边引导("为什么 {brand} 最好"/"{brand} 是不是很差"都违规)。
- 真实买家口吻;一题一个意图。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"brand_fact":["..."]}"""

_COMPETITOR_PROMPT = """利用联网检索,列出品类「{category}」下最主流的真实竞品品牌,
排除「{brand}」(委托方自己)。每个给出常用名称与官方网站主域名。
只要真实、知名的品牌,不得编造。

只返回合法 JSON(至多 {k} 条),不要 markdown,不要解释,形如:
{"competitors":[{"name":"...","domain":"example.com"}]}"""

# ── en-US 语境的英文版(同一套硬规则与质检,16.5)────────────────────────

_BLIND_PROMPT_EN = """You are drafting the questions a real buyer asks an AI assistant about a product category.
You are deliberately NOT given any specific brand. Do not ask for one.

Category: {category}
Language: {locale}

Draft EXACTLY three groups:
- category_discovery: {n_discovery} (category discovery) — open "what are the best / most worth buying in this
  category" across different buyer personas (beginner, experienced, budget, premium, scale, service). No duplicates.
- problem_solution: {n_problem} (problem & solution) — a concrete problem or situation a real buyer faces,
  asking "how do I solve / choose".
- recommendation: {n_recommendation} (recommendation) — directly asking the AI to recommend: "what would you recommend".

HARD RULES:
- NEVER name any brand, company, or product line.
- Do not stack qualifiers that only ONE brand could satisfy (covert bias).
- Real buyer voice; one intent per question; no presupposed answer ("why is X best").
- Stay strictly inside the category "{category}".

Return ONLY valid JSON, no markdown, no comments, exactly:
{"category_discovery":["..."],"problem_solution":["..."],"recommendation":["..."]}"""

_COMPARISON_PROMPT_EN = """Draft EXACTLY {count} "comparison & alternatives" questions for the category "{category}".

Language: {locale}
Competitors you may name (real brands): {competitors}

Cover: alternatives to a competitor; how to choose between two competitors; whether a competitor is worth it
or there is something better.
HARD RULES:
- Name ONLY the competitors listed above, never any other brand; about half of the questions may name no brand
  at all ("what alternatives are there").
- Real buyer voice; one intent each; no presupposed answer.

Return ONLY valid JSON, no markdown, no comments:
{"comparison_alternative":["..."]}"""

_BRAND_FACT_PROMPT_EN = """Draft EXACTLY {count} "brand fact" questions about ONE specific brand
(they test how an AI describes it).

Brand: {brand}
Category: {category}
Language: {locale}

Cover: what it does / flagship products; overall reputation and quality; how it compares with peers;
value for money and who it suits.
HARD RULES:
- Each question names ONLY {brand}. NEVER name a competitor.
- Framing TWO-SIDED and neutral — explicitly invite BOTH strengths and weaknesses.
- Real buyer voice; one intent each.

Return ONLY valid JSON, no markdown, no comments:
{"brand_fact":["..."]}"""

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

    return run_task(adapter, prompt, parse, label="出题", prompt_id="__drafting__", locale=locale,
                    ledger=ledger).value


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("期望字符串数组")
    return [item.strip() for item in value if item.strip()]


# ── 质检原语(17.4)───────────────────────────────────────────────────


def _brand_tokens(brand: BrandSpec) -> list[str]:
    tokens = [brand.name, *brand.aliases, *brand.domains]
    return [t.strip().lower() for t in tokens if t.strip()]


def manual_intent(text: str, brand: BrandSpec) -> PromptIntent:
    """用户手填的题归哪个题型:点了品牌名或别名的就是「品牌事实」,否则按「品类发现」。
    点名的题里品牌提及率天然接近 100%,混进不点名的题型会把「AI 会主动推荐你」的结论虚高。"""
    return PromptIntent.BRAND_FACT if find_first_alias(text, brand) is not None else PromptIntent.CATEGORY_DISCOVERY


def brand_collisions(texts: list[str], brand: BrandSpec) -> list[str]:
    """别名撞车扫描:返回含焦点品牌名/别名/域名的文本(不点名的题命中即违规)。"""
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


# ── 四步序 ────────────────────────────────────────────────────────────


def _draft_blind(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str,
    counts: dict[PromptIntent, int],
    ledger: Ledger,
) -> list[PromptCandidate]:
    """① 真盲:prompt 不含任何品牌信息;质检(数量+撞车)失败即整批重生成。"""
    template = _BLIND_PROMPT_EN if _is_english(locale) else _BLIND_PROMPT
    prompt = (
        template
        .replace("{category}", category)
        .replace("{locale}", locale)
        .replace("{n_discovery}", str(counts[PromptIntent.CATEGORY_DISCOVERY]))
        .replace("{n_problem}", str(counts[PromptIntent.PROBLEM_SOLUTION]))
        .replace("{n_recommendation}", str(counts[PromptIntent.RECOMMENDATION]))
    )

    def check(data: Any) -> None:
        for intent in _BLIND_INTENTS:
            texts = _str_list(data[intent.value])
            if len(texts) != counts[intent]:
                raise ValueError(f"{intent.value} 应恰为 {counts[intent]} 条,实得 {len(texts)}")
        all_texts = [t for intent in _BLIND_INTENTS for t in _str_list(data[intent.value])]
        if len(_dedupe(all_texts)) != len(all_texts):
            raise ValueError("题目存在重复")
        leaked = brand_collisions(all_texts, brand)
        if leaked:
            raise ValueError(f"不点名的题泄漏焦点品牌(违反真盲):{leaked[:2]}")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    return [
        PromptCandidate(origin=PromptOrigin.ASSISTED, intent=intent, text=text)
        for intent in _BLIND_INTENTS
        for text in _str_list(data[intent.value])
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


def _draft_comparison(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    competitors: list[CompetitorSuggestion],
    locale: str,
    count: int,
    ledger: Ledger,
) -> list[PromptCandidate]:
    """③ 对比与替代:可点名②里的竞品,仍绝不点名焦点品牌。"""
    english = _is_english(locale)
    template = _COMPARISON_PROMPT_EN if english else _COMPARISON_PROMPT
    if competitors:
        names = (", " if english else "、").join(c.name for c in competitors)
    else:
        names = "(none; name no brand at all)" if english else "(无;全部题目不点名任何品牌)"
    prompt = (
        template
        .replace("{category}", category)
        .replace("{locale}", locale)
        .replace("{count}", str(count))
        .replace("{competitors}", names)
    )

    def check(data: Any) -> None:
        texts = _str_list(data[PromptIntent.COMPARISON_ALTERNATIVE.value])
        if len(texts) != count:
            raise ValueError(f"对比与替代应恰为 {count} 条,实得 {len(texts)}")
        if len(_dedupe(texts)) != len(texts):
            raise ValueError("题目存在重复")
        leaked = brand_collisions(texts, brand)
        if leaked:
            raise ValueError(f"对比与替代题点名了焦点品牌:{leaked[:2]}")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    return [PromptCandidate(origin=PromptOrigin.ASSISTED, intent=PromptIntent.COMPARISON_ALTERNATIVE, text=text)
            for text in _str_list(data[PromptIntent.COMPARISON_ALTERNATIVE.value])]


def _draft_brand_facts(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str,
    count: int,
    ledger: Ledger,
) -> list[PromptCandidate]:
    """④ 品牌事实:此时才把焦点名交给生成器;每题必须点名焦点品牌。"""
    template = _BRAND_FACT_PROMPT_EN if _is_english(locale) else _BRAND_FACT_PROMPT
    prompt = (
        template
        .replace("{count}", str(count))
        .replace("{brand}", brand.name)
        .replace("{category}", category)
        .replace("{locale}", locale)
    )

    def check(data: Any) -> None:
        texts = _str_list(data[PromptIntent.BRAND_FACT.value])
        if len(texts) != count:
            raise ValueError(f"品牌事实题应恰为 {count} 条,实得 {len(texts)}")
        missing = [t for t in texts if brand.name.lower() not in t.lower()]
        if missing:
            raise ValueError(f"品牌事实题未点名焦点品牌:{missing[:2]}")

    data = _ask_json(adapter, prompt, check, locale=locale, ledger=ledger)
    return [PromptCandidate(origin=PromptOrigin.ASSISTED, intent=PromptIntent.BRAND_FACT, text=text)
            for text in _str_list(data[PromptIntent.BRAND_FACT.value])]


def draft_prompt_candidates(
    adapter: SurfaceAdapter,
    *,
    category: str,
    brand: BrandSpec,
    locale: str = "zh-CN",
    counts: dict[PromptIntent, int] | None = None,
    competitor_k: int = DEFAULT_COMPETITOR_K,
    ledger: Ledger,
) -> tuple[list[PromptCandidate], list[CompetitorSuggestion]]:
    """四步序编排。候选供确认页展示;用户勾选/改写后才成为 Prompt。"""
    counts = counts or DEFAULT_COUNTS
    blind = _draft_blind(adapter, category=category, brand=brand, locale=locale, counts=counts, ledger=ledger)
    competitors = suggest_competitors(adapter, category=category, brand=brand,
                                      k=competitor_k, locale=locale, ledger=ledger)
    comparison = _draft_comparison(adapter, category=category, brand=brand, competitors=competitors,
                                   locale=locale, count=counts[PromptIntent.COMPARISON_ALTERNATIVE], ledger=ledger)
    facts = _draft_brand_facts(adapter, category=category, brand=brand, locale=locale,
                               count=counts[PromptIntent.BRAND_FACT], ledger=ledger)
    candidates = blind + comparison + facts
    log.info("prompt_candidates_drafted", category=category, blind=len(blind), comparison=len(comparison),
             facts=len(facts), competitors=len(competitors))
    return candidates, competitors
