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

from datetime import datetime
from typing import Any

import httpx

from adapters.base import AdapterError, AdapterTransientError, CapabilityRegistry, SurfaceMeta, known_model, priced
from adapters.http_support import STRUCTURE_ERRORS, HttpAdapter, domain_of, full_body, malformed, require_list, spent_on
from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    AskRequest,
    Citation,
    JsonValue,
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

# 官方价目(2026-10-14 核对 platform.claude.com 型号页与价目,与模型清单同步维护):模型 → (输入, 输出) USD / MTok
# 只列官方当前一代(Fable 5.1 / Opus 5.5 / Sonnet 5.5 / Haiku 5.5);上一代(sonnet-5、opus-5、haiku-4-5)2026-10-14 移出。
# 新型号入清单前须经 scripts/smoke_models.py 真实冒烟(本批 S9.0 换代后待冒烟)。
# 2026-11-12 核对 platform.claude.com/docs/en/about-claude/pricing:Fable 5.2 $8 / $40(缓存命中 $0.20),其余四个不变;
# web search 仍 $10 / 千次,「搜索出错的那次不计费」。列型号接口同日确认 claude-fable-5-2 已可调用。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),  # 10 万输入 token 以内的档;本 surface 单次远低于此
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    # claude-fable-5-2($8 / $40,2026-11-09 正式可用)2026-11-12 冒烟两次都没过:平台默认态下搜 2 次、拿到 18–19 个候选后,
    # 4,096 的输出额度被思考与检索过程吃光,stop_reason=max_tokens、正文 0 字、每次 $0.36;
    # 5.1 在 ① 的 16 次里最多用到 2,388。保险丝是口径、用户定不放宽,所以不入清单、5.1 留着;以后再试要先冒烟。
}
_SEARCH_USD_PER_USE = 10.0 / 1000.0  # web search $10 / 千次
# 牌价(用量 × 牌价 = 费用;计费项与 _usage 读出的键一致):token 按每个 token 折,检索按每次
_PRICE_TABLE: dict[str, dict[str, float]] = {
    model: {"input_tokens": price_in / 1e6, "output_tokens": price_out / 1e6, "searches": _SEARCH_USD_PER_USE}
    for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
}
_DEFAULT_MODEL = "claude-sonnet-5-5"
# 型号标记(配置资产,用户定 2026-10-14;2026-11-15 定「主力 = 消费端默认在最新一代里的同款」):
# 消费端默认:应用默认是 Sonnet 系(第三方资料:2026-06-30 起 Sonnet 5 为默认;5.5 上线后是否切换官方未写)→ 待核对。
# Sonnet 5.5 已是最新一代(5.5 代),所以主力就是它自己;中档按档位规则再补清单里紧挨着的 Opus 5.5。
# 沿革:11-03 到 11-15 主力是付费版默认;11-11 查官方帮助中心「What is the Pro plan?」「Claude Fable models on your plan」
# 「Covered Models」都没写付费版默认,11-12 用户付费账号截图默认停在 Opus 5.5,当时定为主力。
# 另:官方「Covered Models」页写 Claude Fable 5.2 已于 2026-11-09 正式可用;用户 2026-11-12 定冒烟过了替掉 5.1,
# 结果冒烟两次都撞 4,096 零正文(见价目表旁的注释),先不换。
_CONSUMER_DEFAULT_MODEL = "claude-sonnet-5-5"
_MAIN_MODEL = "claude-sonnet-5-5"
_MODELS_CHECKED = ("2026-10-14 官方型号页 + 价目页;消费端默认按应用默认 Sonnet 系填 5.5,待核对;"
                   "2026-11-12 用户付费账号截图:付费版默认停在 Opus 5.5(当时的主力);"
                   "Fable 5.2 同日冒烟两次都撞 4,096 零正文,不入清单、5.1 留着;"
                   "2026-11-15 用户定主力 = 消费端默认在最新一代里的同款,Sonnet 5.5 已是最新一代,主力就是它;"
                   "2026-11-15 按工程原则 10 复核:列型号接口没有新一代(Fable 5.2 仍在、冒烟没过);"
                   "帮助中心与发布说明 10-07 Haiku 5.5 之后没有新型号,仍没写免费版默认,标记不变")

_CAPABILITIES = CapabilityRegistry(
    supports_query_trace=True,
    supports_citations=True,
    supports_region=True,
    supports_search_disabled=True,
)



def _blocks(data: Any) -> list[Any]:
    """取响应的 content 块列表;形状不对按 Permanent 上抛(平台改格式须显式暴露)。"""
    return require_list(data, "content")


def _search_requests(data: dict[str, Any]) -> int:
    usage = data.get("usage") or {}
    server_tool_use = usage.get("server_tool_use") or {}
    return server_tool_use.get("web_search_requests") or 0


def _usage(responses: list[dict[str, Any]]) -> dict[str, float]:
    """这次调用的计费用量,pause_turn 各段加总:输入 token(检索回来的内容已计入)、输出 token(含思考)、检索次数。
    本 surface 不发 cache_control,无缓存计费项。"""
    input_tokens = output_tokens = searches = 0
    for data in responses:
        usage = data.get("usage") or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0
        searches += _search_requests(data)
    return {"input_tokens": float(input_tokens), "output_tokens": float(output_tokens), "searches": float(searches)}


def _cost_usd(responses: list[dict[str, Any]], model: str) -> float:
    """按官方价目从 usage 确定性折算:用量 × 牌价。"""
    return priced(_PRICE_TABLE[model], _usage(responses))


def _payload(request: AskRequest, model: str, search_disabled: bool) -> dict[str, Any]:
    """AskRequest → 平台请求体(口径钉死:工具版本、检索上限、回答长度上限)。"""
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
    return payload


def _translate(
    request: AskRequest,
    model: str,
    search_disabled: bool,
    responses: list[dict[str, Any]],
    latency_ms: int,
    started_at: datetime,
) -> SurfaceRun:
    """平台返回(跨全部续传段的 content 块)→ SurfaceRun。只翻译,不判定。"""
    answer_parts: list[str] = []
    queries: list[str] = []
    sources: list[RetrievedSource] = []
    citations: list[Citation] = []
    cited_urls: set[str] = set()
    tool_errors: list[str] = []  # 每个工具错误块的错误码(第三轮审核 A11:原来只留最后一个,前面的被盖掉)
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
                        tool_errors.append((content or {}).get("error_code") or "unknown")
    except STRUCTURE_ERRORS as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise malformed(exc, {"responses": responses}, lambda: _cost_usd(responses, model)) from exc

    polluted = [code for code in tool_errors if code != "max_uses_exceeded"]  # max_uses 是配额内的正常有界检索
    if polluted:  # 平台回了话、钱照计:花费随失败行落(第二轮 26)
        raise AdapterTransientError(f"web_search 工具错误块: {', '.join(polluted)}",
                                    response_body=full_body({"responses": responses}),
                                    cost_usd=_cost_usd(responses, model))

    answer_text = "".join(answer_parts)
    run_fields: dict[str, Any] = dict(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
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
        latency_ms=latency_ms,
        cost_usd=_cost_usd(responses, model),
    )
    last = responses[-1]
    if last.get("stop_reason") == "refusal":
        category = (last.get("stop_details") or {}).get("category")
        message = "stop_reason=refusal" + (f", category={category}" if category else "")
        return SurfaceRun(
            outcome=Outcome.REFUSED,
            error=RunError(error_type=RunErrorType.REFUSAL, message=message),
            **run_fields,
        )
    return SurfaceRun(outcome=Outcome.SUCCESS, **run_fields)


class ClaudeWebSearchAdapter(HttpAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(headers={"x-api-key": api_key, "anthropic-version": API_VERSION}, client=client)

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="anthropic",
            surface="claude_web_search",
            display_name="Claude",
            api_name="Anthropic Messages API",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
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

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        responses = raw_payload.get("responses") if isinstance(raw_payload, dict) else None
        return _usage([r for r in responses if isinstance(r, dict)]) if isinstance(responses, list) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload / _converse),
        平台返回 → SurfaceRun(_translate)。"""
        _CAPABILITIES.require_request(request)
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        model = known_model(request.model, _DEFAULT_MODEL, _TOKEN_PRICES_PER_MTOK)
        responses, latency_ms, started_at = self._converse(request, _payload(request, model, search_disabled))
        return _translate(request, model, search_disabled, responses, latency_ms, started_at)

    def _converse(
        self, request: AskRequest, payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int, datetime]:
        """发请求;遇 pause_turn 按协议续传(有界),返回全部响应段、总耗时、开始时刻。
        续传途中失败(续传请求报错、或续传回包结构不对):前几段已经计费,花费带在异常上随失败行落(第二轮审核 26)。"""
        model = payload["model"]
        data, latency_ms, started_at = self._post(f"{BASE_URL}/v1/messages", payload=payload,
                                                  timeout_s=request.timeout_s)
        _blocks(data)  # 结构防线:每段响应必须有 content 列表
        responses: list[dict[str, Any]] = [data]
        total_latency = latency_ms

        continuations = 0
        try:
            while data.get("stop_reason") == "pause_turn" and continuations < _MAX_CONTINUATIONS:
                # 协议续传:原始提问 + 迄今全部 content 块作为同一 assistant 轮回传
                continuation = dict(
                    payload,
                    messages=[
                        {"role": "user", "content": request.prompt_text},
                        {"role": "assistant", "content": [b for r in responses for b in _blocks(r)]},
                    ],
                )
                data, latency_ms, _ = self._post(f"{BASE_URL}/v1/messages", payload=continuation,
                                                 timeout_s=request.timeout_s)
                _blocks(data)
                responses.append(data)
                total_latency += latency_ms
                continuations += 1
        except AdapterError as exc:
            exc.cost_usd += spent_on(lambda: _cost_usd(responses, model))
            raise
        if data.get("stop_reason") == "pause_turn":
            raise AdapterTransientError(f"pause_turn 续传 {_MAX_CONTINUATIONS} 次后仍未完成",
                                        response_body=full_body({"responses": responses}),
                                        cost_usd=spent_on(lambda: _cost_usd(responses, model)))
        return responses, total_latency, started_at

    def healthcheck(self) -> bool:
        return self._probe(f"{BASE_URL}/v1/messages", payload={
            "model": _DEFAULT_MODEL,
            "max_tokens": 1,  # 不带检索工具的最小请求:验 key 与配额,成本近零
            "messages": [{"role": "user", "content": "ping"}],
        })
