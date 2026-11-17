"""Perplexity 生成+引用+检索通道(Agent API `POST /v1/agent`;S9.0 从旧 Sonar chat/completions 迁来)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

- 测什么(用户 2026-11-15 定「换成 low 预设」):Perplexity 自己维护的 **low 预设**(`effort`),固定版本 PRESET。
  预设 = Perplexity 配好的一整套:型号、搜索设置、步数、系统提示词、工具(web_search、fetch_url)。
  官方定位「日常研究问题」,和我们的题型对得上;它是「Perplexity 替你挑」的接口版,不是应用里的「Best」本身
  (「Best」挑了哪个型号,接口里没有对应,记看不到)。
  原来测的 perplexity/sonar 11-15 删掉:应用里已手选不到,Perplexity 自家的预设也不用它;旧的 sonar 回答留在库里。
- 请求:`effort` 放固定版本(不放 model、不放 tools,都用预设自带的),`input` 原样放题目,
  `max_output_tokens` = MAX_OUTPUT_TOKENS(回答长度保险丝是口径,用户定不放宽;预设本身不设,这里覆盖)。
  版本固定后 Perplexity 改预设也不影响我们;回包的 `effort` 字段写明实际跑的版本,
  `model` 字段写明背后的型号(都在 raw_payload)。
- 返回是一串 `output` 项:`search_results`(检索词 queries + 结果 results,每条有 id;搜几轮就几项,id 全程接着编,
  11-15 真实回包核过)、`fetch_url_results`(打开的网页:url / 标题 / 摘录)、`message`(正文)。
  Answer.citations 走两条机械通道合并:正文段落的 `annotations`(url_citation)与正文里 `[n]` / `[web:n]` 标记指到的结果;
  按在正文里出现的位置排序、按 url 去重——都是平台自己给的对应,不判定。
  预设给正文标记(官方写「带正文内引用」,冒烟 4/4 有),这条通道是通的,所以 citations_available 一律 True:
  搜了没标记就是真的没引,记 0 条。
  (原来 sonar 从不给对应,「搜了却没对上」记看不到;方法版本 0.4.34 改。)
  SearchTrace:retrieved_sources = 全部检索结果(按平台返回顺序,rank = 顺序),generated_queries = 检索词,
  opened_sources = fetch_url 打开的网页(工具调用可见,没打开就是空列表)。
- 没有机器可读的拒答信号:不从正文猜「算不算拒答」(属 L2)。
  status=failed(平台回了话却没办成)→ AdapterTransientError 交调度器重试,与另两家同口径(第三轮审核 A9);
  status=incomplete(截断)→ 回答仍有效,SUCCESS。
- 费用记平台自报的 total_cost(分项齐全:输入三档、输出、搜索、打开网页);用量与牌价只给确认页估价。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from adapters.base import AdapterTransientError, CapabilityRegistry, SurfaceMeta, known_model
from adapters.http_support import STRUCTURE_ERRORS, HttpAdapter, domain_of, full_body, malformed, require_list, spent_on
from engine.models import (
    Answer,
    AskRequest,
    Citation,
    JsonValue,
    Outcome,
    RetrievedSource,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.perplexity.ai"
AGENT_URL = f"{BASE_URL}/v1/agent"
MAX_OUTPUT_TOKENS = 4096  # 回答长度上限(成本上界的一部分),属本 surface 的固定口径
_SUCCESS_STATUSES = ("completed", "incomplete")  # incomplete = 截断,回答仍有效

# 被测对象 = 一个固定版本的预设(型号名就是平台的版本名,报告里写「Perplexity Agent API · low@2026-11-11.0」)。
# 2026-11-15 核对 docs.perplexity.ai/docs/agent-api/effort:low 预设背后是 openai/gpt-6-luna,最多 5 步,
# web_search(快搜、15 条、每页 4,000 token)+ fetch_url(一次 1 个网页、共 1 万 token),推理力度 minimal;
# 同页 fast / medium 也是 gpt-6-luna,high 是 gpt-6.1-terra。
PRESET = "low@2026-11-11.0"
# 牌价(计费项与 _usage 读出的键一致;实际费用记平台自报的 total_cost,这张表只给确认页估价):
# 2026-11-15 核对 Agent API 型号页 gpt-6-luna $0.10 / $0.50 每百万,并拿冒烟回包的分项验算
# (缓存写入 1.25 倍、读取 0.1 倍、搜索每次 $0.001,四条与 total_cost 一分不差);
# 打开网页的费用平台按它自己的抽取用量算、直接给美元,照搬(fetch_usd,单价 1)。
_PRICE_TABLE: dict[str, dict[str, float]] = {
    PRESET: {
        "input_tokens": 0.10 / 1e6,
        "cache_write_tokens": 0.125 / 1e6,
        "cached_input_tokens": 0.01 / 1e6,
        "output_tokens": 0.50 / 1e6,
        "searches": 0.001,
        "fetch_usd": 1.0,
    },
}
# 型号标记(配置资产,用户定 2026-10-14;2026-11-15 定「主力 = 消费端默认在最新一代里的同款」、同日定「换成 low 预设」):
# 应用默认是「Best」自动挑(免费版只能用它,官方没说挑哪个);接口里没有「Best」,
# 最接近的是 Perplexity 自己维护、替你挑型号的预设。
# 清单只有这一个,所以默认、消费端默认、主力都是它。
_CONSUMER_DEFAULT_MODEL = PRESET
_MAIN_MODEL = PRESET
_MODELS_CHECKED = ("2026-11-15 官方帮助中心:Sonar 不在 Pro 手选清单(11-11 快照),应用默认「Best」自动挑、没说挑哪个;"
                   "Agent API 预设页:fast / low / medium 背后是 openai/gpt-6-luna;"
                   "用户定改测 low 预设、固定版本 low@2026-11-11.0,删掉 perplexity/sonar;"
                   "冒烟 4 道题:引用 4/4 看得到,每次 $0.0017–0.0045")

_CAPABILITIES = CapabilityRegistry(supports_query_trace=True, supports_citations=True)

_MARKER = re.compile(r"\[(?:web:)?(\d+)\]")  # 正文里的引用标记:[3] 或 [web:3]


def _output_items(data: Any) -> list[dict[str, Any]]:
    """取 output 项列表;形状不对按 Permanent 上抛(平台改格式须显式暴露)。"""
    return [item for item in require_list(data, "output") if isinstance(item, dict)]


def _text_parts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """message 项里的正文段落(output_text),按顺序;正文 = 各段 text 拼接。"""
    return [
        part
        for item in items if item.get("type") == "message"
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
    ]


def _search_results(items: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]] | None:
    """全部 search_results 项合并:(检索词, 结果);一个都没有 → None(这次没搜)。"""
    found = [item for item in items if item.get("type") == "search_results"]
    if not found:
        return None
    queries = [q for item in found for q in (item.get("queries") or []) if isinstance(q, str)]
    results = [r for item in found for r in (item.get("results") or []) if isinstance(r, dict) and r.get("url")]
    return queries, results


def _opened(items: list[dict[str, Any]]) -> list[str]:
    """fetch_url 打开的网页网址,按出现顺序去重。"""
    urls = [c["url"] for item in items if item.get("type") == "fetch_url_results"
            for c in item.get("contents") or [] if isinstance(c, dict) and c.get("url")]
    return list(dict.fromkeys(urls))


def _citations(parts: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[Citation]:
    """两条机械通道合并:段落 annotations(url_citation)与正文 [n] / [web:n] 标记指到的结果;
    按在正文里出现的位置排序、按 url 去重;标记指向不存在的 id 忽略(不猜)。"""
    by_id = {str(r.get("id")): r for r in results}
    found: list[tuple[int, str, str | None]] = []  # (正文位置, url, 标题)
    offset = 0
    for part in parts:
        text: str = part["text"]
        for note in part.get("annotations") or []:
            if isinstance(note, dict) and note.get("type") == "url_citation" and isinstance(note.get("url"), str):
                found.append((offset + int(note.get("start_index") or 0), note["url"], note.get("title")))
        for match in _MARKER.finditer(text):
            r = by_id.get(match.group(1))
            if r is not None:
                found.append((offset + match.start(), r["url"], r.get("title")))
        offset += len(text)
    citations: list[Citation] = []
    seen: set[str] = set()
    for _, url, title in sorted(found, key=lambda f: f[0]):
        if url in seen:
            continue
        seen.add(url)
        citations.append(Citation(url=url, domain=domain_of(url), title=title, position=len(citations) + 1))
    return citations


def _usage(data: dict[str, Any]) -> dict[str, float]:
    """这次调用的计费用量,键与 _PRICE_TABLE 一致:
    输入三档(没命中缓存的 / 写入缓存的 / 命中缓存的,之和 = 平台报的 input_tokens)、
    输出 token、搜索次数、打开网页的费用(平台直接给美元)。费用本身按平台自报,见 _cost_usd。"""
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    tools = usage.get("tool_calls_details") or {}
    total_input = float(usage.get("input_tokens") or 0)
    written = float(details.get("cache_creation_input_tokens") or 0)
    cached = float(details.get("cache_read_input_tokens") or 0)
    return {
        "input_tokens": max(0.0, total_input - written - cached),
        "cache_write_tokens": written,
        "cached_input_tokens": cached,
        "output_tokens": float(usage.get("output_tokens") or 0),
        "searches": float((tools.get("search_web") or {}).get("invocation") or 0),
        "fetch_usd": float((tools.get("fetch_url") or {}).get("cost_usd") or 0),
    }


def _cost_usd(data: dict[str, Any]) -> float:
    cost = (data.get("usage") or {}).get("cost")
    if isinstance(cost, dict) and "total_cost" in cost:
        return float(cost["total_cost"])
    return 0.0  # 平台未报成本时不臆造


def _translate(
    request: AskRequest,
    model: str,
    items: list[dict[str, Any]],
    data: dict[str, Any],
    latency_ms: int,
    started_at: datetime,
) -> SurfaceRun:
    """平台返回 → SurfaceRun。只翻译,不判定;字段类型不对由调用方按结构不符上抛。"""
    parts = _text_parts(items)
    text = "".join(part["text"] for part in parts)
    searched = _search_results(items)
    queries, results = searched if searched is not None else ([], [])
    trace = SearchTrace(
        search_triggered=searched is not None,
        generated_queries=queries,
        retrieved_sources=[RetrievedSource(url=r["url"], domain=domain_of(r["url"]), rank=i + 1)
                           for i, r in enumerate(results)],
        opened_sources=_opened(items),
    )
    return SurfaceRun(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
        provider="perplexity",
        surface="perplexity_agent",
        model=model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        outcome=Outcome.SUCCESS,
        search_trace=trace,
        # 预设给正文标记,对应这条通道是通的:没标记就是真的没引(0 条),不是看不到
        answer=(Answer(answer_text=text, citations_available=True, citations=_citations(parts, results))
                if text else None),
        raw_payload=data,
        timestamp=started_at,
        latency_ms=latency_ms,
        cost_usd=_cost_usd(data),
    )


class PerplexityAgentAdapter(HttpAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(headers={"Authorization": f"Bearer {api_key}"}, client=client)

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="perplexity",
            surface="perplexity_agent",
            display_name="Perplexity",
            api_name="Perplexity Agent API",
            models=[PRESET],
            default_model=PRESET,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
            # 口径:推导上界(冒烟实测 $0.0017–0.0045 / 次):30,000 输入全按缓存写入价 + 1,500 输出 + 5 次搜索(最多 5 步)
            # + 打开网页 $0.005(冒烟一次 $0.0021)≈ $0.0145;有足量实测后确认页改读实测
            est_cost_per_call_usd={PRESET: 0.015},
        )

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        return _usage(raw_payload) if isinstance(raw_payload, dict) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        _CAPABILITIES.require_request(request)
        preset = known_model(request.model, PRESET, _PRICE_TABLE)
        payload = {"effort": preset, "input": request.prompt_text, "max_output_tokens": MAX_OUTPUT_TOKENS}
        data, latency_ms, started_at = self._post(AGENT_URL, payload=payload, timeout_s=request.timeout_s)
        items = _output_items(data)
        status = data.get("status")
        if status not in _SUCCESS_STATUSES:  # 平台回了话、钱照计,却报失败:花费随失败行落(第二轮审核 26)
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise AdapterTransientError(f"平台返回 status={status!r}: {error.get('message') or '无说明'}",
                                        response_body=full_body(data), cost_usd=spent_on(lambda: _cost_usd(data)))
        try:
            return _translate(request, preset, items, data, latency_ms, started_at)
        except STRUCTURE_ERRORS as exc:  # 字段类型怪(标题不是字符串、位置不是数字、成本不是数……):也算结构不符
            raise malformed(exc, data, lambda: _cost_usd(data)) from exc

    def healthcheck(self) -> bool:
        """按同一个固定版本发最小请求:顺带确认这个版本还在(版本没了平台回 400,预检报 PRECHECK_REJECTED);
        限 1 步、16 个输出 token,一次约一两厘钱。"""
        return self._probe(AGENT_URL, payload={"effort": PRESET, "input": "ping", "max_output_tokens": 16,
                                               "max_steps": 1})
