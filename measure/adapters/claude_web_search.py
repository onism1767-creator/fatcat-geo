"""Claude 生成+引用通道(Anthropic Messages API + web_search 服务端工具)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

版本钉死(架构书附录 v0.4.2 条目 2,风险表 #8 的具体案例):
- 接口版本:anthropic-version: 2023-06-01;
- 检索工具必须用经典版 web_search_20250305——它把来源以内联
  web_search_result_location 引用挂在答案文本上(cited_text/title/url),
  是本 surface「被引用」测量的依据。新版 web_search_20260209 的「动态过滤」
  (模型写代码筛结果)实测内联引用恒为 0(前作 Potato 真实 API 对照:12 vs 0),
  会让核心引用指标静默失效。改动版本号前必须重做引用对照实验。

本 surface 的可观测面(与 perplexity_agent 的关键差异——每家 AI 的检索习惯
不同,暴露的链路环节也不同,翻译只忠实记录本平台暴露的部分):
- 检索是显式工具调用:server_tool_use 块暴露模型自拟的检索词
  (generated_queries),web_search_tool_result 块暴露喂给模型的有序候选
  (retrieved_sources,rank = 全轮次出现顺序)。「模型没搜」是
  search_triggered=False 的真实负观测,不是 UNKNOWN。
- 引用是文内锚定,同一 URL 可被多处引用:翻译为按首次出现去重的引用池
  (position = 池内位次);锚定细节(cited_text/重复锚点)留在 raw_payload
  供 L2 判定器重解析。
- search_disabled 模式 = 不带工具调用(测参数化知识)。此时不存在引用机制,
  citations_available=False——空引用是结构性缺席,不是「没引用你」的负观测。
  该模式下 user_location 无处附着,故 region 与 search_disabled 组合显式报错。
- 服务端工具循环超长时平台返回 stop_reason=pause_turn:按协议把累计 content
  原样回传续跑。这是完成单次采样的协议步骤,不是重试;续传有界
  (_MAX_CONTINUATIONS),超限按瞬时错误上抛交引擎处置。
- 显式拒答仅认机器可读信号 stop_reason=refusal → outcome=REFUSED;
  从回答文本猜测「算不算拒答」属 L2 判定器,不做。
- 检索工具错误以 HTTP 200 + error 块返回:max_uses_exceeded 是配额内的正常
  有界检索(SUCCESS,已完成的检索与回答有效);其余错误码
  (too_many_requests / unavailable / ...)意味着本次采样被检索基础设施污染,
  按瞬时错误上抛,由引擎决定重采。
- 平台不在响应中报告美元成本:cost_usd 由 usage 按官方价目表确定性折算
  (token 单价 + 检索次数单价),是换算不是估算。
- raw_payload 统一存 {"responses": [逐次完整响应]}(含 pause_turn 各段),
  单响应也用此形,L2 重解析只需处理一种形状。
"""

from __future__ import annotations

from typing import Any

import httpx

from adapters.base import (
    AdapterError,
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
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
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"  # 钉死的接口版本
TOOL_TYPE = "web_search_20250305"  # 钉死的工具版本,升级前必须重做引用对照实验(见模块注释)
MAX_USES = 3  # 单次采样检索次数上限:成本上界的一部分,属本 surface 的固定口径
MAX_TOKENS = 4096  # 回答长度上限(平台必填):成本上界的一部分,属本 surface 的固定口径

_MAX_CONTINUATIONS = 3  # pause_turn 续传上限:有界完成单次采样,超限按瞬时错误上抛

# 官方价目(2026-06 核对,与模型清单同步维护):模型 → (输入, 输出) USD / MTok
# 只列官方当前一代;过时型号不入选(定义见架构书附录 v0.4.20,2026-09-29 移除上一代两个)
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    # ↓ 2026-09-21 补入(官方当前阵容;经 scripts/smoke_models.py 真实冒烟:请求形状被接受、观测窗在、回答未被截断)
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
_SEARCH_USD_PER_USE = 10.0 / 1000.0  # web search $10 / 千次
_DEFAULT_MODEL = "claude-sonnet-5"

_CAPABILITIES = CapabilityRegistry(
    supports_query_trace=True,
    supports_citations=True,
    supports_region=True,
    supports_search_disabled=True,
)



def _blocks(data: Any) -> list[Any]:
    """取响应的 content 块列表;形状不对按 Permanent 上抛(平台改格式须显式暴露)。"""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise AdapterPermanentError(f"响应结构不符合预期: 顶层为 {type(data).__name__},缺 content 列表")
    return data["content"]


def _search_requests(data: dict[str, Any]) -> int:
    usage = data.get("usage") or {}
    server_tool_use = usage.get("server_tool_use") or {}
    return server_tool_use.get("web_search_requests") or 0


def _cost_usd(responses: list[dict[str, Any]], model: str) -> float:
    """按官方价目从 usage 确定性折算(本 surface 不发 cache_control,无缓存计费项)。"""
    in_price, out_price = _TOKEN_PRICES_PER_MTOK[model]
    input_tokens = output_tokens = searches = 0
    for data in responses:
        usage = data.get("usage") or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0
        searches += _search_requests(data)
    token_cost = input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
    return token_cost + searches * _SEARCH_USD_PER_USE


class ClaudeWebSearchAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="anthropic",
            surface="claude_web_search",
            display_name="Claude",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            default_model=_DEFAULT_MODEL,
            capabilities=_CAPABILITIES,
            unit_prices={  # 默认模型口径;实际计费按所选模型价目折算
                "input_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][0],
                "output_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][1],
                "search_usd_per_use": _SEARCH_USD_PER_USE,
            },
            # 口径:推导上界,按型号各算(满额 MAX_USES 次检索;每次检索约 1.2 万输入
            # token 为 Potato 实测校准值),有足量实测后确认页改读实测均值:
            # 默认型号 (2000 + 3×12000)/1e6×$2 + 2000/1e6×$10 + 3×$0.01 ≈ $0.13
            est_cost_per_call_usd={
                model: round((2000 + MAX_USES * 12000) / 1e6 * price_in + 2000 / 1e6 * price_out
                             + MAX_USES * _SEARCH_USD_PER_USE, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
            # 闭卷腿口径:同源推导上界,无检索项:默认型号 (2000×$2 + 2000×$10)/1e6 ≈ $0.02
            est_cost_per_call_search_disabled_usd={
                model: round((2000 * price_in + 2000 * price_out) / 1e6, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        _CAPABILITIES.require_search_mode(request.search_mode)
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        if request.region is not None and search_disabled:
            raise CapabilityNotSupportedError(
                "search_disabled 模式无法钉地区:user_location 挂在检索工具上,该模式不带工具"
            )
        model = request.model or _DEFAULT_MODEL
        if model not in _TOKEN_PRICES_PER_MTOK:
            raise AdapterPermanentError(
                f"未知模型 {model!r},本 surface 支持: {list(_TOKEN_PRICES_PER_MTOK)}"
            )

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": request.prompt_text}],
        }
        if not search_disabled:
            tool: dict[str, Any] = {"type": TOOL_TYPE, "name": "web_search", "max_uses": MAX_USES}
            if request.region is not None:
                tool["user_location"] = {"type": "approximate", "country": request.region}
            payload["tools"] = [tool]

        data, latency_ms, started_at = contract_post(
            self._client,
            f"{BASE_URL}/v1/messages",
            headers=self._headers,
            payload=payload,
            timeout_s=request.timeout_s,
        )
        responses: list[dict[str, Any]] = [data]
        _blocks(data)  # 结构防线:每段响应必须有 content 列表
        total_latency = latency_ms

        continuations = 0
        while data.get("stop_reason") == "pause_turn" and continuations < _MAX_CONTINUATIONS:
            # 协议续传:原始提问 + 迄今全部 content 块作为同一 assistant 轮回传
            continuation = dict(
                payload,
                messages=[
                    {"role": "user", "content": request.prompt_text},
                    {"role": "assistant", "content": [b for r in responses for b in _blocks(r)]},
                ],
            )
            data, latency_ms, _ = contract_post(
                self._client,
                f"{BASE_URL}/v1/messages",
                headers=self._headers,
                payload=continuation,
                timeout_s=request.timeout_s,
            )
            responses.append(data)
            _blocks(data)
            total_latency += latency_ms
            continuations += 1
        if data.get("stop_reason") == "pause_turn":
            raise AdapterTransientError(f"pause_turn 续传 {_MAX_CONTINUATIONS} 次后仍未完成")

        # ── 翻译 content 块(跨全部续传段)──────────────────────────────
        answer_parts: list[str] = []
        queries: list[str] = []
        sources: list[RetrievedSource] = []
        citations: list[Citation] = []
        cited_urls: set[str] = set()
        tool_error: str | None = None
        search_observed = False
        try:
            for response in responses:
                for block in _blocks(response):
                    block_type = block.get("type")
                    if block_type == "text":
                        answer_parts.append(block["text"])
                        for c in block.get("citations") or []:
                            if c.get("type") != "web_search_result_location":
                                continue
                            if c["url"] in cited_urls:
                                continue  # 引用池按首次出现去重;重复锚点在 raw_payload
                            cited_urls.add(c["url"])
                            citations.append(
                                Citation(
                                    url=c["url"],
                                    domain=domain_of(c["url"]),
                                    title=c.get("title"),
                                    position=len(citations) + 1,
                                )
                            )
                    elif block_type == "server_tool_use":
                        search_observed = True
                        query = (block.get("input") or {}).get("query")
                        if query:
                            queries.append(query)
                    elif block_type == "web_search_tool_result":
                        search_observed = True
                        content = block.get("content")
                        if isinstance(content, list):
                            for item in content:
                                sources.append(
                                    RetrievedSource(
                                        url=item["url"],
                                        domain=domain_of(item["url"]),
                                        title=item.get("title"),
                                        rank=len(sources) + 1,  # 全轮次出现顺序即位次
                                    )
                                )
                        else:  # HTTP 200 + 错误块(平台的工具级错误通道)
                            tool_error = (content or {}).get("error_code") or "unknown"
        except (KeyError, TypeError) as exc:
            raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}") from exc

        if tool_error is not None and tool_error != "max_uses_exceeded":
            raise AdapterTransientError(f"web_search 工具错误块: {tool_error}")

        answer_text = "".join(answer_parts)
        run_fields: dict[str, Any] = dict(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="anthropic",
            surface="claude_web_search",
            model=model,
            search_mode=request.search_mode,
            locale=request.locale,
            region=request.region,
            search_trace=SearchTrace(
                # 检索是显式工具调用:触发与否、检索词、候选集全部真实可观测
                search_triggered=False if search_disabled else search_observed,
                generated_queries=queries,
                retrieved_sources=sources,
                opened_sources=NOT_OBSERVABLE,  # 候选整体喂给模型,无「打开哪条」概念
            ),
            answer=(
                Answer(
                    answer_text=answer_text,
                    citations_available=not search_disabled,
                    citations=citations,
                )
                if answer_text
                else None
            ),
            raw_payload={"responses": responses},
            timestamp=started_at,
            latency_ms=total_latency,
            cost_usd=_cost_usd(responses, model),
        )
        if data.get("stop_reason") == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            message = "stop_reason=refusal" + (f", category={category}" if category else "")
            return SurfaceRun(
                outcome=Outcome.REFUSED,
                error=RunError(error_type=RunErrorType.REFUSAL, message=message),
                **run_fields,
            )
        return SurfaceRun(outcome=Outcome.SUCCESS, **run_fields)

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client,
                f"{BASE_URL}/v1/messages",
                headers=self._headers,
                payload={
                    "model": _DEFAULT_MODEL,
                    "max_tokens": 1,  # 不带检索工具的最小请求:验 key 与配额,成本近零
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout_s=15.0,
            )
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
