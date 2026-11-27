"""品牌常见写法(S9.2b,用户定 2026-10-20「甲」):出题时让 AI 联网列出品牌最多 8 种常见写法,每种一句理由;
再拿它这次引用与搜到的网页 + 官网首页,数每种写法实际出现的次数——「最常见」是数出来的,不是 AI 的印象(铁律 7)。
确认页按次数排序列出:出现过的预勾,没出现过的和太短的(≤ 3 个字符,容易把别家认成你)不勾,用户再增减;
勾上的进 BrandSpec.aliases,随任务冻结。

辅助调用走 engine/ai_task.py(不进任何指标分母);网页用核实器开(第三方尊重 robots,官网不看 robots,D13),
同一网页 7 天内复用、同样落 page_check 表。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

import structlog

from adapters.base import SurfaceAdapter
from engine.ai_task import Ledger, extract_json, run_task
from engine.detectors.matching import count_alias
from engine.models import BrandSpec, FrozenModel, PageFetchStatus, SurfaceRun
from engine.page_check import PageIndex, PageRequest, normalize_url

log = structlog.get_logger("engine.spellings")

MAX_SPELLINGS = 8  # AI 最多列几种(用户定)
MAX_PAGES = 10  # 最多开几个网页数次数(官网首页 + 引用的 + 搜到的)
MIN_CHARS_TO_PRESELECT = 4  # 短于这个的写法不预勾:三个字符以内太容易撞上别的词


class PageOpener(Protocol):
    def check(self, requests: Iterable[PageRequest]) -> PageIndex: ...


class SpellingCandidate(FrozenModel):
    """AI 列的一种写法 + 在网页里数出来的次数(确认页展示;勾上才进别名表)。"""

    text: str
    reason: str  # AI 的一句理由:英文名 / 简称 / 连写 / 常见错写……
    pages_seen: int  # 数了几个网页(开成功的)
    pages_with: int  # 出现在几个网页里
    occurrences: int  # 总共出现几次
    short: bool  # 太短(≤ 3 个字符):不预勾

    @property
    def preselected(self) -> bool:
        return self.pages_with > 0 and not self.short


_PROMPT = """利用联网检索,列出品牌「{brand}」(官网 {website})在网上最常见的写法:
中文名、英文名、简称、连写、大小写变化、常见错写等。最多 {k} 种,每种用一句话说明它是哪种写法。
只要真实见到过的写法,不得编造;不要列别的品牌。

只返回合法 JSON,不要 markdown,不要解释,形如:
{"spellings":[{"text":"...","reason":"..."}]}"""

_PROMPT_EN = """Using web search, list the most common ways the brand "{brand}" (website {website}) is written online:
localized names, abbreviations, run-together forms, casing variants, common misspellings.
At most {k}, each with a one-sentence note saying what kind of variant it is.
Only forms actually seen online; do not invent; do not list other brands.

Return ONLY valid JSON, no markdown, no comments:
{"spellings":[{"text":"...","reason":"..."}]}"""


def suggest_spellings(
    adapter: SurfaceAdapter,
    *,
    brand: BrandSpec,
    website: str,
    locale: str,
    ledger: Ledger,
    pages: PageOpener,
) -> list[SpellingCandidate]:
    """让 AI 列写法 → 去掉已登记的与重复的、封顶 → 开网页数次数 → 按次数排序。"""
    template = _PROMPT_EN if locale.lower().startswith("en") else _PROMPT
    prompt = template.replace("{brand}", brand.name).replace("{website}", website).replace("{k}", str(MAX_SPELLINGS))
    result = run_task(adapter, prompt, _parse, label="常见写法", prompt_id="__spellings__", locale=locale,
                      ledger=ledger)
    known = {brand.name.strip().lower(), *(alias.strip().lower() for alias in brand.aliases)}
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text, reason in result.value:
        key = text.lower()
        if not text or key in known or key in seen:
            continue
        seen.add(key)
        picked.append((text, reason))
        if len(picked) >= MAX_SPELLINGS:
            break
    texts = _open_pages(pages, _page_urls(result.runs, website), website)
    candidates = [
        SpellingCandidate(
            text=text, reason=reason, pages_seen=len(texts),
            pages_with=sum(count_alias(page, text) > 0 for page in texts.values()),
            occurrences=sum(count_alias(page, text) for page in texts.values()),
            short=len(text) < MIN_CHARS_TO_PRESELECT,
        )
        for text, reason in picked
    ]
    candidates.sort(key=lambda c: (-c.occurrences, -c.pages_with))  # 同分保持 AI 给的顺序(稳定排序)
    log.info("spellings_suggested", brand=brand.name, listed=len(result.value), kept=len(candidates), pages=len(texts))
    return candidates


def _parse(text: str) -> list[tuple[str, str]]:
    data = extract_json(text)
    items = data.get("spellings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("spellings 须为数组")
    out: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("spellings 的每项须为对象")
        out.append((str(item.get("text", "")).strip(), str(item.get("reason", "")).strip()))
    return out


def _page_urls(runs: list[SurfaceRun], website: str) -> list[str]:
    """数次数的网页:官网首页 + 这次联网调用引用的与搜到的网页(引用的在前),去重,封顶。"""
    urls = [website]
    for run in runs:
        if run.answer is not None:
            urls += [c.url for c in run.answer.citations]
        sources = run.search_trace.retrieved_sources
        if isinstance(sources, list):
            urls += [s.url for s in sources]
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= MAX_PAGES:
            break
    return out


def _open_pages(pages: PageOpener, urls: list[str], website: str) -> dict[str, str]:
    """开网页取正文:官网不看 robots(D13),第三方尊重 robots;打不开、抽不出正文的不算分母。"""
    own = normalize_url(website)
    index = pages.check([
        PageRequest(url=url, respect_robots=normalize_url(url) != own, allow_archive=False) for url in urls
    ])
    texts: dict[str, str] = {}
    for url in urls:
        check = index.lookup(url)
        if check is not None and check.status is PageFetchStatus.OK and check.text:
            texts[url] = check.text
    return texts
