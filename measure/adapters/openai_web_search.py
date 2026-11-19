"""OpenAI 生成+引用通道(Responses API + web_search 托管工具)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

版本与口径钉死(2026-09-08 按官方文档与 SDK 类型定义核对):
- 工具用 GA 版 {"type": "web_search"}(legacy web_search_preview 不用)。
  OpenAI 工具串无日期版本号,防漂移第一道防线是 golden payload 契约测试;
- search_context_size 显式钉 "medium"(口径明示,不吃平台默认值漂移);
- include=["web_search_call.action.sources"] 常开:候选来源全量观测;
- store=false:测量调用不在平台侧留存;
- 不设 reasoning/温度参数:测平台默认状态 = 真实用户遇到的那台机器。

本 surface 的可观测面(暴露面核对,比 claude_web_search 再多一扇窗):
- 检索词:web_search_call.action.search 的 queries/query(两种字段都处理);
- 候选来源:action.sources 有序 URL 列表(无标题无排名,位次即顺序);
- 打开的网页:action.open_page 的 url——opened_sources 首个可观测的 surface;
  find_in_page 是页内查找,不计新打开。未见任何检索动作 = search_triggered=False
  的真实负观测;
- 引用:message 文本的 url_citation 锚注(url/title/字符区间),同一 URL 可
  多处引用 → 按首次出现去重成引用池,锚定细节留 raw_payload;
- FORCED_SEARCH:tool_choice="required"(单工具下即强制检索)——全链首个
  支持强制检索的 surface;
- search_disabled = 不带工具(citations_available=False,结构性无引用);
  该模式下 user_location 无处附着,region 组合显式报错;
- 显式拒答仅认机器可读信号:message 内容含 {"type": "refusal"} 部件 → REFUSED;
- 顶层 status="incomplete"(如 max_output_tokens 截断)仍是 SUCCESS(截断的
  回答也是回答,细节在 raw_payload);status="failed" 按瞬时错误上抛;
- 检索动作本身失败(web_search_call.status == "failed",官方枚举 in_progress / searching /
  completed / failed)= 本次采样被检索基础设施污染,同样按瞬时错误上抛,与 Claude 工具错误块、
  Gemini is_error 同口径(第三轮审核 A10;原来记成「搜了、没候选、回答有效」还计检索费);
- 平台不报美元成本:cost_usd = usage token × 官方价目 + 检索动作数 × $0.01
  ($10/千次,检索内容 token 计入 input_tokens 按模型价,确定性折算);输入 token 分三档:
  没命中缓存的全价、命中缓存的按各型号的缓存价(输入价的 0.05–0.1 倍,逐个照官方价目页)、写入缓存的 1.25 倍
  (三者都含在平台报的 input_tokens 里;2026-11-03 对 astra 一天的账单逐行核过,一分不差;
  2026-11-11 起分档记,之前一律全价、记高,Q8;2026-11-17 缓存价改成按型号写);
- 检索次数上限:**没接**,成本保险丝是 max_output_tokens 与调度器预算闸门。平台现在有 `max_tool_calls` 参数
  (2026-09-08 落地时没有;2026-11-18 真实试过:给 1,只完成 1 次检索、回显 1、回答正常,模型想发的下一次
  留成一个 status="searching" 的 web_search_call——那一项算不算钱没查明)。没接的原因:库里 184 次回答最多搜 5 回、
  九成在 3 回以内,没有失控的尾巴,装了也碰不到;要装得先查清被截下的那一项怎么记,并对每个型号重做真实对照;
- external_web_access(仅缓存模式)存在但 v1 无消费方,未接入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from adapters.base import AdapterTransientError, BillingNote, CapabilityRegistry, SurfaceMeta, known_model, priced
from adapters.http_support import STRUCTURE_ERRORS, HttpAdapter, domain_of, full_body, malformed, require_list, spent_on
from engine.models import (
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

BASE_URL = "https://api.openai.com"
TOOL_TYPE = "web_search"  # GA 托管工具;改动前必须重做 golden 对照
SEARCH_CONTEXT_SIZE = "medium"  # 显式钉死的检索上下文档位,属本 surface 固定口径
MAX_OUTPUT_TOKENS = 4096  # 回答长度保险丝(成本上界的一部分)
INCLUDE = ["web_search_call.action.sources"]  # 候选来源观测窗常开

# 官方价目(2026-11-17 核对 developers.openai.com 价目页与型号页,标准价,与模型清单同步维护):
# 模型 → (输入, 输出, 缓存命中的输入) USD / MTok。缓存命中的折扣各型号不同(6.1 / 6.2 代是输入价的 0.05 倍,其余 0.1 倍),
# 所以逐个照价目页写(2026-11-17 改;原来一律按 0.1 倍算,6.1 两款记高,错误日志 11-17)。
# 清单 = 现役一代(型号页 11-17:6.2 Astra / 6.2 Sol / 6.1 Terra / 6 Luna)
# + 消费端默认 gpt-5.6-luna(ChatGPT 免费版默认,低档要测)。
# 用户 2026-11-17 定「按清单改」(工程原则 9):6.2 两款真实冒烟通过后入清单
# (6.2-sol:检索词 6、候选 39、引用 4、正文 985 字、$0.054、12 秒;
# 6.2-astra:检索词 3、候选 19、引用 3、正文 933 字、$0.111、15 秒;输出各六百来个 token,没截断),
# 上一代 gpt-6-astra、gpt-6.1-sol、gpt-6-sol 删掉(旧回答留库;gpt-6-sol 是付费版聊天默认,
# 11-15 起付费版默认不再是标记)。更早:gpt-5.6-terra / gpt-5.6-sol 2026-10-14 移出;
# gpt-6.1-terra 2026-11-13 冒烟后入(搜 7 次、候选 53、引用 3、正文 935 字、$0.042、18 秒)。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
    "gpt-6-luna": (0.10, 0.50, 0.01),
    "gpt-6.1-terra": (1.00, 5.00, 0.05),
    "gpt-6.2-sol": (2.00, 10.00, 0.10),
    "gpt-6.2-astra": (8.00, 40.00, 0.40),
}
_SEARCH_USD_PER_CALL = 10.0 / 1000.0  # web search $10 / 千次(仅 search 动作计费;2026-11-17 价目页没变)
# 写入缓存的输入按输入价的 1.25 倍(30 分钟档,各型号一样;回包 cache_write_tokens_details.ttl_30m_tokens 对得上;
# 2026-11-03 对 astra 一天的账单逐行核过)。命中缓存的与写入缓存的都含在平台报的 input_tokens 里,算全价那份时要扣掉
_CACHE_WRITE_FACTOR = 1.25
# 牌价(用量 × 牌价 = 费用;计费项与 _usage 读出的键一致):token 按每个 token 折,检索按每次
_PRICE_TABLE: dict[str, dict[str, float]] = {
    model: {"input_tokens": price_in / 1e6, "cached_input_tokens": price_cached / 1e6,
            "cache_write_tokens": price_in * _CACHE_WRITE_FACTOR / 1e6, "output_tokens": price_out / 1e6,
            "searches": _SEARCH_USD_PER_CALL}
    for model, (price_in, price_out, price_cached) in _TOKEN_PRICES_PER_MTOK.items()
}
# 2026-11-11 之前建的单按全价记了缓存命中的输入(用户 2026-11-03 定 Q8:已记的不改,旧单的报告注明)
_CACHE_BILLING_NOTE = BillingNote(
    since=datetime(2026, 11, 11, tzinfo=UTC),
    zh="OpenAI 的费用在 2026-11-11 前把命中缓存的输入 token 也按全价记,实际账单要低一些"
       "(对过一天:最贵的型号低约 19%,默认型号约 3%);已记的数照旧不改。",
    en="Before 2026-11-11, OpenAI's cached input tokens were charged here at full price; the real bill is lower "
       "(one day reconciled: about 19% lower for the priciest model, about 3% for the default). "
       "Recorded figures are kept as they were.",
)
_DEFAULT_MODEL = "gpt-5.6-luna"
# 型号标记(配置资产,用户定 2026-10-14「消费端默认按免费版」;2026-11-15 定「主力 = 消费端默认在最新一代里的同款」):
# 2026-11-17 核对官方帮助中心「GPT-6 and other models in ChatGPT」(当天更新):免费版 / Go 默认 GPT-5.6 Luna;
# 付费版普通聊天默认 GPT-6,由 GPT-6 Sol 驱动;6.2 Astra / 6.2 Sol 当天起只在 Work 与 Codex 推出,不进普通聊天。
# 消费端默认 gpt-5.6-luna 是 5.6 代;它在最新一代里的同款是 gpt-6-luna
# (6.1、6.2 代都没有 Luna,型号页现役阵容里的 Luna 就是它)。
# 帮助中心没提 gpt-6-luna 在 ChatGPT 里,所以中档测的是「免费用户今天看到的」加「现役一代的同款」。
# 11-03 到 11-15 主力是付费版默认 gpt-6-sol;11-17 它随上一代一起移出清单。
_CONSUMER_DEFAULT_MODEL = "gpt-5.6-luna"
_MAIN_MODEL = "gpt-6-luna"
_MODELS_CHECKED = ("2026-11-17 官方型号页 + 价目页:现役阵容 6.2 Astra / 6.2 Sol / 6.1 Terra / 6 Luna,"
                   "价目逐项核对(含各型号的缓存命中价);"
                   "官方帮助中心「GPT-6 and other models in ChatGPT」(11-17 更新):免费版默认 GPT-5.6 Luna、"
                   "付费版聊天默认 GPT-6(Sol),6.2 两款只在 Work 与 Codex;"
                   "用户 11-15 定主力 = 消费端默认在最新一代里的同款(gpt-6-luna);"
                   "用户 11-17 定按清单改:6.2 两款冒烟通过入清单,删 gpt-6-astra / gpt-6.1-sol / gpt-6-sol")

_CAPABILITIES = CapabilityRegistry(
    supports_query_trace=True,
    supports_citations=True,
    supports_region=True,
    supports_forced_search=True,
    supports_search_disabled=True,
)



def _search_calls(output: list[Any]) -> int:
    """计费的检索动作数:仅 web_search_call 里 action.type == search 的算(官方口径;open_page 等不收费)。"""
    return sum(
        1 for item in output
        if isinstance(item, dict) and item.get("type") == "web_search_call"
        and isinstance(item.get("action"), dict) and item["action"].get("type") == "search"
    )


def _usage(data: dict[str, Any]) -> dict[str, float]:
    """这次调用的计费用量:输入 token 分三档(没命中缓存的 / 命中缓存的 / 写入缓存的,三者之和 = 平台报的 input_tokens,
    检索回来的内容已计入)、输出 token(平台已把推理 token 计入)、计费的检索动作数。键与 _PRICE_TABLE 一致。"""
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    total_input = float(usage.get("input_tokens") or 0)
    cached = float(details.get("cached_tokens") or 0)
    written = float(details.get("cache_write_tokens") or 0)
    return {
        "input_tokens": max(0.0, total_input - cached - written),
        "cached_input_tokens": cached,
        "cache_write_tokens": written,
        "output_tokens": float(usage.get("output_tokens") or 0),
        "searches": float(_search_calls(data.get("output") or [])),
    }


def _cost_usd(data: dict[str, Any], model: str) -> float:
    """官方价目确定性折算:用量 × 牌价。"""
    return priced(_PRICE_TABLE[model], _usage(data))


def _payload(request: AskRequest, model: str, search_disabled: bool) -> dict[str, Any]:
    """AskRequest → 平台请求体(口径钉死)。"""
    payload: dict[str, Any] = {
        "model": model,
        "input": request.prompt_text,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
    }
    if not search_disabled:
        tool: dict[str, Any] = {"type": TOOL_TYPE, "search_context_size": SEARCH_CONTEXT_SIZE}
        if request.region is not None:
            tool["user_location"] = {"type": "approximate", "country": request.region}
        payload["tools"] = [tool]
        payload["include"] = INCLUDE
        if request.search_mode is SearchMode.FORCED_SEARCH:
            payload["tool_choice"] = "required"  # 单工具下即强制检索(官方口径)
    return payload


def _translate(
    request: AskRequest,
    model: str,
    search_disabled: bool,
    data: Any,
    latency_ms: int,
    started_at: datetime,
) -> SurfaceRun:
    """平台返回 → SurfaceRun。只翻译,不判定;结构不对按 Permanent 上抛(平台改格式须显式暴露)。"""
    output = require_list(data, "output")
    if data.get("status") == "failed":  # 平台回了话、钱照计,却报失败:花费随失败行落(第二轮审核 26)
        raise AdapterTransientError(f"响应 status=failed: {data.get('error')}", response_body=full_body(data),
                                    cost_usd=spent_on(lambda: _cost_usd(data, model)))

    # ── 翻译 output 项 ──────────────────────────────────────────────
    answer_parts: list[str] = []
    queries: list[str] = []
    sources: list[RetrievedSource] = []
    opened: list[str] = []
    citations: list[Citation] = []
    cited_urls: set[str] = set()
    search_observed = False
    refusal_text: str | None = None
    try:
        for item in output:
            item_type = item.get("type")
            if item_type == "web_search_call":
                search_observed = True
                if item.get("status") == "failed":  # 检索动作本身失败:本次采样被检索基础设施污染,交调度器重采
                    raise AdapterTransientError(f"web_search_call 检索动作失败(status=failed,id={item.get('id')!r})",
                                                response_body=full_body(data),
                                                cost_usd=spent_on(lambda: _cost_usd(data, model)))
                action = item.get("action") or {}
                action_type = action.get("type")
                if action_type == "search":
                    queries += action.get("queries") or (
                        [action["query"]] if action.get("query") else []
                    )
                    for entry in action.get("sources") or []:
                        sources.append(
                            RetrievedSource(
                                url=entry["url"],
                                domain=domain_of(entry["url"]),
                                title=None,  # 平台不给标题,不臆造
                                rank=len(sources) + 1,  # 有序列表位次
                            )
                        )
                elif action_type == "open_page" and action.get("url"):
                    opened.append(action["url"])
            elif item_type == "message":
                for part in item.get("content") or []:
                    part_type = part.get("type")
                    if part_type == "output_text":
                        answer_parts.append(part["text"])
                        for note in part.get("annotations") or []:
                            if note.get("type") != "url_citation":
                                continue
                            if note["url"] in cited_urls:
                                continue  # 引用池按首次出现去重;重复锚点在 raw_payload
                            cited_urls.add(note["url"])
                            citations.append(
                                Citation(
                                    url=note["url"],
                                    domain=domain_of(note["url"]),
                                    title=note.get("title"),
                                    position=len(citations) + 1,
                                )
                            )
                    elif part_type == "refusal":
                        refusal_text = part.get("refusal") or "refusal"
    except STRUCTURE_ERRORS as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise malformed(exc, data, lambda: _cost_usd(data, model)) from exc

    answer_text = "".join(answer_parts)
    run_fields: dict[str, Any] = dict(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
        provider="openai",
        surface="openai_web_search",
        model=model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        search_trace=SearchTrace(
            # 检索是显式工具调用:触发/检索词/候选/翻页全部真实可观测
            search_triggered=False if search_disabled else search_observed,
            generated_queries=queries,
            retrieved_sources=sources,
            opened_sources=opened,  # 本 surface 独有:open_page 动作暴露翻页行为
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
        raw_payload=data,
        timestamp=started_at,
        latency_ms=latency_ms,
        cost_usd=_cost_usd(data, model),
    )
    if refusal_text is not None:
        return SurfaceRun(
            outcome=Outcome.REFUSED,
            error=RunError(
                error_type=RunErrorType.REFUSAL,
                message=f"refusal 内容部件: {refusal_text[:200]}",
            ),
            **run_fields,
        )
    return SurfaceRun(outcome=Outcome.SUCCESS, **run_fields)


class OpenAIWebSearchAdapter(HttpAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(headers={"Authorization": f"Bearer {api_key}"}, client=client)

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="openai",
            surface="openai_web_search",
            display_name="OpenAI",
            api_name="OpenAI Responses API",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
            # 口径:推导上界,按型号各算(无检索次数上限参数,按 3 次检索、每次约 1.2 万
            # 检索内容 token 的 Potato 校准值估),有足量实测后确认页改读实测均值:
            # 默认型号 gpt-5.6-luna (2000 + 3×12000)/1e6×$0.2 + 2000/1e6×$1.2 + 3×$0.01 ≈ $0.04
            est_cost_per_call_usd={
                model: round((2000 + 3 * 12000) / 1e6 * price_in + 2000 / 1e6 * price_out
                             + 3 * _SEARCH_USD_PER_CALL, 3)
                for model, (price_in, price_out, _) in _TOKEN_PRICES_PER_MTOK.items()
            },
            # 闭卷腿口径:同源推导上界,无检索项:默认型号 (2000×$0.2 + 2000×$1.2)/1e6 ≈ $0.003
            est_cost_per_call_search_disabled_usd={
                model: round((2000 * price_in + 2000 * price_out) / 1e6, 3)
                for model, (price_in, price_out, _) in _TOKEN_PRICES_PER_MTOK.items()
            },
            billing_notes=[_CACHE_BILLING_NOTE],
        )

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        return _usage(raw_payload) if isinstance(raw_payload, dict) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload),平台返回 → SurfaceRun(_translate)。"""
        _CAPABILITIES.require_request(request)
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        model = known_model(request.model, _DEFAULT_MODEL, _TOKEN_PRICES_PER_MTOK)
        data, latency_ms, started_at = self._post(
            f"{BASE_URL}/v1/responses", payload=_payload(request, model, search_disabled), timeout_s=request.timeout_s,
        )
        return _translate(request, model, search_disabled, data, latency_ms, started_at)

    def healthcheck(self) -> bool:
        return self._probe(f"{BASE_URL}/v1/responses", payload={
            "model": _DEFAULT_MODEL,
            "input": "ping",
            "max_output_tokens": 16,  # 平台下限;不带检索工具,成本近零
            "store": False,
        })
