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
- 平台不报美元成本:cost_usd = usage token × 官方价目 + 检索动作数 × $0.01
  ($10/千次,检索内容 token 计入 input_tokens 按模型价,确定性折算);
- 平台无检索次数上限参数(无 claude 的 max_uses 类比):成本保险丝是
  max_output_tokens 与调度器预算闸门;
- external_web_access(仅缓存模式)存在但 v1 无消费方,未接入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from adapters.base import (
    AdapterAccountError,
    AdapterError,
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
    priced,
)
from adapters.http_support import contract_post, domain_of, full_body, spent_on
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

# 官方价目(2026-10-14 核对 developers.openai.com 价目页与型号页,与模型清单同步维护):模型 → (输入, 输出) USD / MTok
# 当前一代 = GPT-6 系(astra / 6.1-sol / 6-sol / 6-luna);gpt-5.6-luna 保留,
# 因为它仍是 ChatGPT 免费版的默认型号(帮助中心 2026-10)。
# 上一代 gpt-5.6-terra / gpt-5.6-sol 2026-10-14 移出。新型号入清单前须经 scripts/smoke_models.py 真实冒烟(本批待冒烟)。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5.6-luna": (0.2, 1.2),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-astra": (10.0, 50.0),
}
_SEARCH_USD_PER_CALL = 10.0 / 1000.0  # web search $10 / 千次(仅 search 动作计费)
# 牌价(用量 × 牌价 = 费用;计费项与 _usage 读出的键一致):token 按每个 token 折,检索按每次
_PRICE_TABLE: dict[str, dict[str, float]] = {
    model: {"input_tokens": price_in / 1e6, "output_tokens": price_out / 1e6, "searches": _SEARCH_USD_PER_CALL}
    for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
}
_DEFAULT_MODEL = "gpt-5.6-luna"
# 型号标记(配置资产,用户定 2026-10-14「消费端默认按免费版」):ChatGPT 免费版 / Go 默认 GPT-5.6 Luna(官方帮助中心);
# 付费版默认是 GPT-6(Sol 跑 Instant / Medium / High),不取。最强 = GPT-6 Astra(官方「最强」)。
_CONSUMER_DEFAULT_MODEL = "gpt-5.6-luna"
_STRONGEST_MODEL = "gpt-6-astra"
_MODELS_CHECKED = ("2026-10-14 官方型号页 + 价目页 + 帮助中心「GPT-6 and other models in ChatGPT」;"
                   "消费端默认按免费版(用户定)")

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
    """这次调用的计费用量:输入 token(检索回来的内容已计入)、输出 token(平台已把推理 token 计入)、计费的检索动作数。
    缓存命中的输入 token 仍按全价计(官方按 0.1 倍,要对一次账单再改:任务列表第十三节第 10 条)。"""
    usage = data.get("usage") or {}
    return {
        "input_tokens": float(usage.get("input_tokens") or 0),
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
    if not isinstance(data, dict) or not isinstance(data.get("output"), list):
        raise AdapterPermanentError(
            f"响应结构不符合预期: 顶层为 {type(data).__name__},缺 output 列表", response_body=full_body(data),
        )
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
        for item in data["output"]:
            item_type = item.get("type")
            if item_type == "web_search_call":
                search_observed = True
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
    except (KeyError, TypeError, AttributeError, ValueError) as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}", response_body=full_body(data),
                                    cost_usd=spent_on(lambda: _cost_usd(data, model))) from exc

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


class OpenAIWebSearchAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="openai",
            surface="openai_web_search",
            display_name="OpenAI",
            api_name="OpenAI Responses API",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            strongest_model=_STRONGEST_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
            # 口径:推导上界,按型号各算(无检索次数上限参数,按 3 次检索、每次约 1.2 万
            # 检索内容 token 的 Potato 校准值估),有足量实测后确认页改读实测均值:
            # 默认型号 (2000 + 3×12000)/1e6×$2 + 2000/1e6×$12 + 3×$0.01 ≈ $0.13
            est_cost_per_call_usd={
                model: round((2000 + 3 * 12000) / 1e6 * price_in + 2000 / 1e6 * price_out
                             + 3 * _SEARCH_USD_PER_CALL, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
            # 闭卷腿口径:同源推导上界,无检索项:默认型号 (2000×$2 + 2000×$12)/1e6 ≈ $0.03
            est_cost_per_call_search_disabled_usd={
                model: round((2000 * price_in + 2000 * price_out) / 1e6, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        return _usage(raw_payload) if isinstance(raw_payload, dict) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload),平台返回 → SurfaceRun(_translate)。"""
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
        data, latency_ms, started_at = contract_post(
            self._client,
            f"{BASE_URL}/v1/responses",
            headers=self._headers,
            payload=_payload(request, model, search_disabled),
            timeout_s=request.timeout_s,
        )
        return _translate(request, model, search_disabled, data, latency_ms, started_at)

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client,
                f"{BASE_URL}/v1/responses",
                headers=self._headers,
                payload={
                    "model": _DEFAULT_MODEL,
                    "input": "ping",
                    "max_output_tokens": 16,  # 平台下限;不带检索工具,成本近零
                    "store": False,
                },
                timeout_s=15.0,
            )
        except AdapterAccountError:
            raise  # 账户问题要说清是换 key 还是充值:开跑前预检据此停单(S11.1)
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
