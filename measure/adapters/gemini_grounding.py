"""Gemini 生成+引用通道(Interactions API + google_search 接地工具)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

口径钉死(2026-09-08 按官方文档 + 真实探针核对;golden 即真身裁剪):
- 走 GA 版 Interactions API(POST /v1beta/interactions,官方推荐;
  legacy generateContent+groundingMetadata 不用);工具 {"type": "google_search"};
- store=false:测量调用不在平台侧留存;
- 不带 generation_config:测平台默认态(无输出上限保险丝,成本由预算闸门
  封顶——实测单发 token 费不足一分钱,检索费才是大头)。

本 surface 的可观测面(暴露面核对,介于 perplexity 与 claude 之间):
- 检索词可观测:google_search_call 步骤 arguments.queries(一步多词);
  未见任何检索步骤 = search_triggered=False 的真实负观测(探针实测:
  同一问题模型可自主选择不搜);
- 候选集不暴露:google_search_result 仅含搜索建议组件(界面用 HTML),
  retrieved_sources 如实记 NOT_OBSERVABLE(铁律 7,禁止用引用反推);
- 引用为文内锚注 url_citation,但 url 是平台跳转链
  (vertexaisearch.cloud.google.com/grounding-api-redirect/…)。能力声明
  citation_urls_redirect=True:Citation.domain 就是链接本身的主机名(跳转服务),
  不从 title 推真实域名——引擎采样后跟随跳转还原终点(engine/resolve.py,S4),
  判定用终点链接,还原失败记看不到。实测同一来源的跳转链稳定复用,
  按 url 去重成立(75 锚注 → 16 唯一 URL);
- search_disabled = 不带工具(citations_available=False,结构性无引用);
- 拒答:Interactions API 未见文档化的机器可读拒答信号,v1 不做 REFUSED
  翻译(宁缺毋滥,待真实样本再补);
- 终态:status=completed/incomplete → SUCCESS(截断的回答也是回答);
  failed/cancelled 及其余状态按瞬时错误上抛;google_search_result 的
  is_error=true 视为检索基础设施污染本次采样,同样上抛;
- 计费(价目 2026-09-08:促销价至 2026-12-31,2027 起翻倍,届时同步):
  思考 token 按输出价计费;检索按「执行的唯一非空查询数」计费
  $14/千条(每月 5000 条免费额度全家共享,预估不指望免费额度)。
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
    SearchMode,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://generativelanguage.googleapis.com"
TOOL_TYPE = "google_search"  # GA 版接地工具;改动前必须重做真实对照
_SUCCESS_STATUSES = ("completed", "incomplete")  # incomplete=截断,回答仍有效

# 官方价目(2026-10-14 核对 ai.google.dev 型号页与价目页;促销价有到期日,到期须同步):
# 模型 → (输入, 输出含思考) USD / MTok
# 当前一代 = 4 Argon(优惠价至 2026-11-30,之后 $4 / $20)、3.1 Pro(preview)、
# 3.8 Flash(优惠价至 2026-12-31,之后 $1.5 / $7.5);3.6-flash 2026-10-14 移出
# (官方已把 3.5 / 3.7 流量转到 3.6 / 3.8,3.8 才是现行 Flash)。新型号入清单前须真实冒烟(本批待冒烟)。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "gemini-4-argon": (2.0, 10.0),
}
_SEARCH_USD_PER_QUERY = 14.0 / 1000.0  # 接地检索 $14 / 千次查询(每月 5,000 次免费额度不计入预估)
_DEFAULT_MODEL = "gemini-3.8-flash"
# 型号标记(配置资产,用户定 2026-10-14):Gemini 应用默认官方未写(2025-12 起 3 Flash 为默认;
# 3.8 Flash 是现行 Flash)→ 填 3.8,待核对;
# 最强 = Gemini 4 Argon(官方「旗舰推理模型」,2026-10-13 起向 Pro / Ultra 用户推)
_CONSUMER_DEFAULT_MODEL = "gemini-3.8-flash"
_STRONGEST_MODEL = "gemini-4-argon"
_MODELS_CHECKED = "2026-10-14 官方型号页 + 价目页 + 应用更新日志;消费端默认按现行 Flash 填 3.8,待核对"

_CAPABILITIES = CapabilityRegistry(
    supports_query_trace=True,
    supports_citations=True,
    supports_search_disabled=True,
    citation_urls_redirect=True,  # 引用是平台跳转链,引擎采样后还原终点(S4)
)


def _queries(steps: list[Any]) -> list[str]:
    """全部 google_search_call 步里的非空检索词,按出现顺序(计费与检索轨迹共用)。"""
    return [
        q
        for step in steps if step.get("type") == "google_search_call"
        for q in (step.get("arguments") or {}).get("queries") or [] if q
    ]


def _cost_usd(data: dict[str, Any], model: str) -> float:
    """官方价目确定性折算:token 费(思考按输出价)+ 唯一非空查询数 × 检索单价。"""
    in_price, out_price = _TOKEN_PRICES_PER_MTOK[model]
    usage = data.get("usage") or {}
    token_cost = (
        (usage.get("total_input_tokens") or 0) / 1e6 * in_price
        + ((usage.get("total_output_tokens") or 0) + (usage.get("total_thought_tokens") or 0))
        / 1e6 * out_price
    )
    return token_cost + len(set(_queries(data["steps"]))) * _SEARCH_USD_PER_QUERY


def _payload(request: AskRequest, model: str, search_disabled: bool) -> dict[str, Any]:
    """AskRequest → 平台请求体(口径钉死)。"""
    payload: dict[str, Any] = {
        "model": model,
        "input": request.prompt_text,
        "store": False,
    }
    if not search_disabled:
        payload["tools"] = [{"type": TOOL_TYPE}]
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
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise AdapterPermanentError(
            f"响应结构不符合预期: 顶层为 {type(data).__name__},缺 steps 列表", response_body=full_body(data),
        )
    status = data.get("status")
    if status not in _SUCCESS_STATUSES:  # 平台回了话、钱照计,却报失败:花费随失败行落(第二轮审核 26)
        raise AdapterTransientError(f"互动终态异常 status={status!r}", response_body=full_body(data),
                                    cost_usd=spent_on(lambda: _cost_usd(data, model)))

    # ── 翻译 steps ─────────────────────────────────────────────────
    answer_parts: list[str] = []
    citations: list[Citation] = []
    cited_urls: set[str] = set()
    search_observed = False
    try:
        queries = _queries(data["steps"])
        for step in data["steps"]:
            step_type = step.get("type")
            if step_type == "google_search_call":
                search_observed = True
            elif step_type == "google_search_result":
                search_observed = True
                if step.get("is_error"):
                    raise AdapterTransientError("google_search_result 标记 is_error", response_body=full_body(data),
                                                cost_usd=spent_on(lambda: _cost_usd(data, model)))
            elif step_type == "model_output":
                for block in step.get("content") or []:
                    if block.get("type") != "text":
                        continue
                    answer_parts.append(block["text"])
                    for note in block.get("annotations") or []:
                        if note.get("type") != "url_citation":
                            continue
                        if note["url"] in cited_urls:
                            continue  # 跳转链按来源稳定复用,按 url 去重(实测核对)
                        cited_urls.add(note["url"])
                        citations.append(
                            Citation(
                                url=note["url"],  # 平台跳转链原样保留,引擎采样后还原终点(S4)
                                domain=domain_of(note["url"]),  # 链接本身的主机名(跳转服务),不从标题推
                                title=note.get("title"),
                                position=len(citations) + 1,
                            )
                        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}", response_body=full_body(data),
                                    cost_usd=spent_on(lambda: _cost_usd(data, model))) from exc

    answer_text = "".join(answer_parts)
    run = SurfaceRun(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
        provider="gemini",
        surface="gemini_grounding",
        model=model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        outcome=Outcome.SUCCESS,
        search_trace=SearchTrace(
            # 检索步骤可观测:触发与否、检索词真实可观测;候选集平台不暴露
            search_triggered=False if search_disabled else search_observed,
            generated_queries=queries,
            retrieved_sources=NOT_OBSERVABLE,
            opened_sources=NOT_OBSERVABLE,
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
    return run


class GeminiGroundingAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"x-goog-api-key": api_key}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="gemini",
            surface="gemini_grounding",
            display_name="Gemini",
            api_name="Gemini API",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            strongest_model=_STRONGEST_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            unit_prices={  # 默认模型口径;实际计费按所选模型价目折算
                "input_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][0],
                "output_usd_per_mtok": _TOKEN_PRICES_PER_MTOK[_DEFAULT_MODEL][1],
                "search_usd_per_query": _SEARCH_USD_PER_QUERY,
            },
            # 口径:推导上界,按型号各算(3 条检索词 + 探针实测 token 量),有足量实测后
            # 确认页改读实测均值:默认型号 3×$0.014 + (200×$0.75 + 3000×$3.75)/1e6 ≈ $0.054
            est_cost_per_call_usd={
                model: round(3 * _SEARCH_USD_PER_QUERY + (200 * price_in + 3000 * price_out) / 1e6, 3)
                for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
            # 闭卷腿口径:同源推导,无检索项:默认型号 (2300+950)×$3.75/1e6 ≈ $0.012
            est_cost_per_call_search_disabled_usd={
                model: round((2300 + 950) * price_out / 1e6, 3)
                for model, (_, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload),平台返回 → SurfaceRun(_translate)。"""
        _CAPABILITIES.require_search_mode(request.search_mode)
        if request.region is not None:
            _CAPABILITIES.require("supports_region")
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        model = request.model or _DEFAULT_MODEL
        if model not in _TOKEN_PRICES_PER_MTOK:
            raise AdapterPermanentError(
                f"未知模型 {model!r},本 surface 支持: {list(_TOKEN_PRICES_PER_MTOK)}"
            )
        data, latency_ms, started_at = contract_post(
            self._client,
            f"{BASE_URL}/v1beta/interactions",
            headers=self._headers,
            payload=_payload(request, model, search_disabled),
            timeout_s=request.timeout_s,
        )
        return _translate(request, model, search_disabled, data, latency_ms, started_at)

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client,
                f"{BASE_URL}/v1beta/interactions",
                headers=self._headers,
                payload={"model": _DEFAULT_MODEL, "input": "ping", "store": False},
                timeout_s=30.0,
            )
        except AdapterAccountError:
            raise  # 账户问题要说清是换 key 还是充值:开跑前预检据此停单(S11.1)
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
