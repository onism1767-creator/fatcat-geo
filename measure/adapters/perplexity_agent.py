"""Perplexity 生成+引用+检索通道(Agent API `POST /v1/agent`,S9.0 迁移;
旧 Sonar chat/completions 已于 2026-09-27 停止支持)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

- 请求:`model` 用平台的「厂商/型号」写法(本仓库只测 Perplexity 自家的 perplexity/sonar),`input` 原样放题目,
  带一个 `web_search` 工具;回答长度上限 MAX_OUTPUT_TOKENS 是本 surface 的固定口径。
- 返回是一串 `output` 项:`search_results`(检索词 queries + 结果 results,每条有 id)、`message`(正文)。
  Answer.citations = 正文里 `[n]` / `[web:n]` 标记指到的结果(按首次出现顺序给 position)
  ——这是平台自己的引用标记,机械解析,不判定;
  SearchTrace.retrieved_sources = 全部检索结果(按 id 顺序,rank = 顺序)、generated_queries = 检索词:
  两者 Agent API 都暴露,
  所以本 surface 自 S9.0 起 supports_query_trace=True(旧接口时期标「看不到」是因为平台不给)。
- 没有机器可读的拒答信号(旧接口的 finish_reason=content_filter 在 Agent API 里没有对应):
  不从正文猜「算不算拒答」(属 L2)。
  status=failed → AdapterPermanentError;status=incomplete(截断)→ 回答仍有效,SUCCESS。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from adapters.base import (
    AdapterError,
    AdapterPermanentError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from adapters.http_support import contract_post, domain_of
from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    AskRequest,
    Citation,
    Outcome,
    RetrievedSource,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.perplexity.ai"
AGENT_URL = f"{BASE_URL}/v1/agent"
MAX_OUTPUT_TOKENS = 4096  # 回答长度上限(成本上界的一部分),属本 surface 的固定口径
_SUCCESS_STATUSES = ("completed", "incomplete")  # incomplete = 截断,回答仍有效

# 官方价目(2026-10-14 核对,docs.perplexity.ai/docs/agent-api/models):模型 → (输入, 输出) USD / MTok
# 只列 Perplexity 自家当前一代型号;Agent API 里挂的 OpenAI / Anthropic / Google 型号由各自适配器直接测,这里不重复
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "perplexity/sonar": (0.25, 2.50),
}
_WEB_SEARCH_USD_PER_CALL = 0.0025  # 工具费:web_search 每次调用(同页)
_DEFAULT_MODEL = "perplexity/sonar"
# 型号标记(配置资产,用户定 2026-10-14):官方帮助中心只列套餐可选型号、没写默认,
# 按「默认搜索模式用 Sonar」(官方 2025-02 博客)填,待核对
_CONSUMER_DEFAULT_MODEL = "perplexity/sonar"
_STRONGEST_MODEL = "perplexity/sonar"  # 自家型号只有 sonar 一个;Pro 套餐里的第三方型号归各家自己的适配器
_MODELS_CHECKED = ("2026-10-14 官方 Agent API 型号与价目页;消费端默认按 2025-02 官方博客「默认搜索模式用 Sonar」填,"
                   "待核对;最强 = 自家仅 sonar 一个")

_CAPABILITIES = CapabilityRegistry(supports_query_trace=True, supports_citations=True)

_MARKER = re.compile(r"\[(?:web:)?(\d+)\]")  # 正文里的引用标记:[3] 或 [web:3]


def _output_items(data: Any) -> list[dict[str, Any]]:
    """取 output 项列表;形状不对按 Permanent 上抛(平台改格式须显式暴露)。"""
    if not isinstance(data, dict) or not isinstance(data.get("output"), list):
        raise AdapterPermanentError(f"响应结构不符合预期: 顶层为 {type(data).__name__},缺 output 列表")
    return [item for item in data["output"] if isinstance(item, dict)]


def _answer_text(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


def _search_results(items: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]] | None:
    """全部 search_results 项合并:(检索词, 结果);一个都没有 → None(这次没搜)。"""
    found = [item for item in items if item.get("type") == "search_results"]
    if not found:
        return None
    queries = [q for item in found for q in (item.get("queries") or []) if isinstance(q, str)]
    results = [r for item in found for r in (item.get("results") or []) if isinstance(r, dict) and r.get("url")]
    return queries, results


def _citations(text: str, results: list[dict[str, Any]]) -> list[Citation]:
    """正文标记指到的结果,按首次出现顺序;标记指向不存在的 id 忽略(不猜)。"""
    by_id = {str(r.get("id")): r for r in results}
    citations: list[Citation] = []
    seen: set[str] = set()
    for marker in _MARKER.findall(text):
        if marker in seen or marker not in by_id:
            continue
        seen.add(marker)
        r = by_id[marker]
        citations.append(Citation(url=r["url"], domain=domain_of(r["url"]), title=r.get("title"),
                                  position=len(citations) + 1))
    return citations


def _cost_usd(data: dict[str, Any]) -> float:
    cost = (data.get("usage") or {}).get("cost")
    if isinstance(cost, dict) and "total_cost" in cost:
        return float(cost["total_cost"])
    return 0.0  # 平台未报成本时不臆造


class PerplexityAgentAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="perplexity",
            surface="perplexity_agent",
            display_name="Perplexity",
            api_name="Perplexity Agent API",
            models=list(_TOKEN_PRICES_PER_MTOK),
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            strongest_model=_STRONGEST_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            unit_prices={
                "input_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][0],
                "output_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][1],
                "web_search_usd_per_call": _WEB_SEARCH_USD_PER_CALL,
            },
            # 口径:推导上界——官方样例一次搜索约 4,700 输入 token;按 6,000 输入 + 800 输出 + 1 次搜索:
            # (6000×$0.25 + 800×$2.5)/1e6 + $0.0025 ≈ $0.006;有足量实测后确认页改读实测均值
            est_cost_per_call_usd={
                model: round((6000 * price_in + 800 * price_out) / 1e6 + _WEB_SEARCH_USD_PER_CALL, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        _CAPABILITIES.require_search_mode(request.search_mode)
        if request.region is not None:
            _CAPABILITIES.require("supports_region")
        model = request.model or _DEFAULT_MODEL
        if model not in _TOKEN_PRICES_PER_MTOK:
            raise AdapterPermanentError(f"型号 {model!r} 不在本适配器清单里(清单与价目同步维护)")
        payload = {
            "model": model,
            "input": request.prompt_text,
            "tools": [{"type": "web_search"}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        data, latency_ms, started_at = contract_post(
            self._client, AGENT_URL, headers=self._headers, payload=payload, timeout_s=request.timeout_s,
        )
        items = _output_items(data)
        status = data.get("status")
        if status not in _SUCCESS_STATUSES:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise AdapterPermanentError(f"平台返回 status={status!r}: {error.get('message') or '无说明'}")
        text = _answer_text(items)
        searched = _search_results(items)
        if searched is None:
            trace = SearchTrace(search_triggered=False, generated_queries=[], retrieved_sources=[],
                                opened_sources=NOT_OBSERVABLE)
            citations: list[Citation] = []
        else:
            queries, results = searched
            trace = SearchTrace(
                search_triggered=True,
                generated_queries=queries,
                retrieved_sources=[RetrievedSource(url=r["url"], domain=domain_of(r["url"]), rank=i + 1)
                                   for i, r in enumerate(results)],
                opened_sources=NOT_OBSERVABLE,
            )
            citations = _citations(text, results)
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
            answer=Answer(answer_text=text, citations_available=True, citations=citations) if text else None,
            raw_payload=data,
            timestamp=started_at,
            latency_ms=latency_ms,
            cost_usd=_cost_usd(data),
        )

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client, AGENT_URL, headers=self._headers,
                payload={"model": _DEFAULT_MODEL, "input": "ping", "max_output_tokens": 16},
                timeout_s=15.0,
            )
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
