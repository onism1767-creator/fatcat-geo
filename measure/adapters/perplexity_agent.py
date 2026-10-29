"""Perplexity 生成+引用+检索通道(Agent API `POST /v1/agent`,S9.0 迁移;
旧 Sonar chat/completions 已于 2026-09-27 停止支持)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

- 请求:`model` 用平台的「厂商/型号」写法(本仓库只测 Perplexity 自家的 perplexity/sonar),`input` 原样放题目,
  带一个 `web_search` 工具;回答长度上限 MAX_OUTPUT_TOKENS 是本 surface 的固定口径。
- 返回是一串 `output` 项:`search_results`(检索词 queries + 结果 results,每条有 id)、`message`(正文)。
  Answer.citations 走两条机械通道合并:正文段落的 `annotations`(官方 schema 的 url_citation:start_index / url / title)
  与正文里 `[n]` / `[web:n]` 标记指到的结果;按在正文里出现的位置排序、按 url 去重——都是平台自己给的对应,不判定。
  搜了、但两条通道都是空的 → citations_available=False:平台没把回答和来源对上,「看不到」;
  不得记成「引用 0 条」(2026-10-14 实测 perplexity/sonar 三次都是 annotations=[] 且正文无标记,见错误日志)。
  没搜 → 没有来源可引,citations=[] 且 citations_available=True(真实的「没引」)。
  SearchTrace.retrieved_sources = 全部检索结果(按 id 顺序,rank = 顺序)、generated_queries = 检索词:
  两者 Agent API 都暴露,所以本 surface 自 S9.0 起 supports_query_trace=True(旧接口时期标「看不到」是因为平台不给)。
- 没有机器可读的拒答信号(旧接口的 finish_reason=content_filter 在 Agent API 里没有对应):
  不从正文猜「算不算拒答」(属 L2)。
  status=failed → AdapterPermanentError;status=incomplete(截断)→ 回答仍有效,SUCCESS。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from adapters.base import (
    AdapterAccountError,
    AdapterError,
    AdapterPermanentError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from adapters.http_support import contract_post, domain_of, full_body, spent_on
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
        raise AdapterPermanentError(f"响应结构不符合预期: 顶层为 {type(data).__name__},缺 output 列表",
                                    response_body=full_body(data))
    return [item for item in data["output"] if isinstance(item, dict)]


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
    if searched is None:
        trace = SearchTrace(search_triggered=False, generated_queries=[], retrieved_sources=[],
                            opened_sources=NOT_OBSERVABLE)
        citations: list[Citation] = []
        citations_available = True  # 没搜就没有来源可引:「没引」是真实观测
    else:
        queries, results = searched
        trace = SearchTrace(
            search_triggered=True,
            generated_queries=queries,
            retrieved_sources=[RetrievedSource(url=r["url"], domain=domain_of(r["url"]), rank=i + 1)
                               for i, r in enumerate(results)],
            opened_sources=NOT_OBSERVABLE,
        )
        citations = _citations(parts, results)
        citations_available = bool(citations)  # 搜了却一条都没对上:平台没给回答与来源的对应,看不到(不记 0 条)
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
        answer=(Answer(answer_text=text, citations_available=citations_available, citations=citations)
                if text else None),
        raw_payload=data,
        timestamp=started_at,
        latency_ms=latency_ms,
        cost_usd=_cost_usd(data),
    )


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
        if status not in _SUCCESS_STATUSES:  # 平台回了话、钱照计,却报失败:花费随失败行落(第二轮审核 26)
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise AdapterPermanentError(f"平台返回 status={status!r}: {error.get('message') or '无说明'}",
                                        response_body=full_body(data), cost_usd=spent_on(lambda: _cost_usd(data)))
        try:
            return _translate(request, model, items, data, latency_ms, started_at)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            # 结构防线(第二轮审核 26:原来这家没有):字段类型怪(标题不是字符串、位置不是数字、成本不是数……)也算
            # 结构不符,带整份响应按 Permanent 上抛——不让原始异常冒出去成了「契约之外的异常」、连响应都没存
            raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}", response_body=full_body(data),
                                        cost_usd=spent_on(lambda: _cost_usd(data))) from exc

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client, AGENT_URL, headers=self._headers,
                payload={"model": _DEFAULT_MODEL, "input": "ping", "max_output_tokens": 16},
                timeout_s=15.0,
            )
        except AdapterAccountError:
            raise  # 账户问题要说清是换 key 还是充值:开跑前预检据此停单(S11.1)
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
