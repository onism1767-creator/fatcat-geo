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
- 计费(价目与到期日见下面价目表的注释,以那里为准):思考 token 按输出价计费;
  检索按「执行的唯一非空查询数」计费 $14/千条(每月 5000 条免费额度全家共享,预估不指望免费额度)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from adapters.base import AdapterTransientError, CapabilityRegistry, SurfaceMeta, known_model, priced
from adapters.http_support import STRUCTURE_ERRORS, HttpAdapter, domain_of, full_body, malformed, require_list, spent_on
from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    AskRequest,
    Citation,
    JsonValue,
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
# 2026-11-12 核对 aistudio.google.com/docs/pricing(ai.google.dev 的价目页已跳转到这里)与 /docs/models,
# 列型号接口同日确认都能调:4 Carbon(官方「最强推理」)与 4 Argon 同价 $2 / $10,12-01 起 $4 / $20;
# 4 Hydrogen(「日常编码与代理工作流的快模型」)$1.5 / $7.5;3.8 Flash $0.75 / $3.75 至 12-31;
# 3.5 Flash-Lite $0.30 / $2.50(型号页写它是现行 Flash-Lite,「新项目用 4 Hydrogen 或 3.5 Flash-Lite」);
# 3.1 Pro preview $2 / $12(≤200k);接地检索 $14 / 千次、每月 5,000 次免费全家共享、每日上限 1,500 次。
# 3.5 Flash-Lite 进被测清单是给「消费端默认」用的(用户 11-12 定冒烟后换;官方帮助中心只写免费版走「Flash-Lite」没写版本,
# 接口里现行的 Flash-Lite 是 3.5,9to5Google 2026-10-09 也写免费版是 3.5 Flash-Lite——版本号是这样定下来的,不是官方原话)。
# 2026-11-13 按工程原则 9(用户定「最高档跟着各家最新一代走,旧的删掉」):第 3 代的 3.8 Flash、3.1 Pro preview 移出,
# 第 4 代的 Hydrogen 入清单(11-12 冒烟通:搜 4 次、引用 17 条、$0.089、32 秒);3.5 Flash-Lite 是消费端默认,低档要测,留着。
# 2026-11-20 4 Neon 入清单(用户定「加进清单」;11-19 按工程原则 10 核对时列型号接口里新出现,官方 11-18 发布):
# 型号页写「Frontier-level intelligence with fast end-to-end task completion」,排在 4 Carbon 与 4 Hydrogen 之间;
# 价目页 $3 / $15,没有分档、没有优惠期、没有免费档。11-20 冒烟通:搜 2 次、引用 13 条、正文 2,048 字、$0.0854、45 秒,
# 没截断,平台自报型号就是它;那一次输出加思考 3,782 个 token,比下面声明价假设的 3,000 多。闭卷腿没单独试。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-4-hydrogen": (1.5, 7.5),
    # 2026-11-20 用户定「每家只留两个」(消费端默认 + 主力):4 Neon($3 / $15,当天上午刚入)、4 Argon、4 Carbon($2 / $10,
    # 12-01 起 $4 / $20)删掉,旗舰不再测
}
# 只当裁判的便宜型号(BR6;价目 2026-10-30 核对官方价目页与型号页):不进上面的被测清单,否则高档「全部型号」会多测一个
_HELPER_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
}
_ALL_PRICES_PER_MTOK = {**_TOKEN_PRICES_PER_MTOK, **_HELPER_PRICES_PER_MTOK}
_MEASURED_USD_PER_CALL = {"gemini-4-hydrogen": 0.20}  # 联网问一次的实测均值(来历见 meta() 里的口径说明)
_SEARCH_USD_PER_QUERY = 14.0 / 1000.0  # 接地检索 $14 / 千次查询(每月 5,000 次免费额度不计入预估)
# 牌价(用量 × 牌价 = 费用;计费项与 _usage 读出的键一致):思考 token 按输出价;裁判型号也列,裁判费用同一公式
_PRICE_TABLE: dict[str, dict[str, float]] = {
    model: {"input_tokens": price_in / 1e6, "output_tokens": price_out / 1e6, "thought_tokens": price_out / 1e6,
            "search_queries": _SEARCH_USD_PER_QUERY}
    for model, (price_in, price_out) in _ALL_PRICES_PER_MTOK.items()
}
_DEFAULT_MODEL = "gemini-3.5-flash-lite"  # 2026-11-13 跟着 3.8 Flash 移出改成消费端默认(加载器要求默认型号在清单里)
# 型号标记(配置资产,用户定 2026-10-14;2026-11-15 定「主力 = 消费端默认在最新一代里的同款」):
# 2026-11-11 核对官方帮助中心「About Gemini models」:没有付费方案的用户默认「自动选型」(多数提问走 Flash-Lite,
# 要推理的用 Flash;关掉自动就全是 Flash-Lite);Google AI Pro 可选 Flash-Lite / Flash / Pro / 4 Argon,同样默认自动选型;
# Ultra 另有 4 Carbon(官方「最强」)。用户 2026-11-12 定消费端默认换 Flash-Lite,同日冒烟通过,填 3.5 Flash-Lite(第 3 代)。
# 它在最新一代里的同款是 4 Hydrogen:第 4 代没有 Flash-Lite,型号页写「新项目用 4 Hydrogen 或 3.5 Flash-Lite」,
# Hydrogen 是第 4 代的快型号。11-13 时 Gemini 应用里还没有;11-15 再核帮助中心:应用里原来写「Flash」的地方
# 都换成了 4 Hydrogen(免费版自动选型「要推理的走 Hydrogen」,Plus / Pro / Ultra 都可选),免费版默认仍是 Flash-Lite。
# 11-12 到 11-15 主力是 4 Argon(付费默认自动选型范围里的最高档);11-20「每家只留两个」起它不在清单里了。
_CONSUMER_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_MAIN_MODEL = "gemini-4-hydrogen"
_MODELS_CHECKED = ("2026-10-14 官方型号页 + 价目页 + 应用更新日志;2026-11-11 官方帮助中心「About Gemini models」:"
                   "免费与付费都默认自动选型(多数走 Flash-Lite);"
                   "用户 2026-11-12 定消费端默认换 Flash-Lite,同日冒烟后填 3.5 Flash-Lite(接口里现行的 Flash-Lite;"
                   "官方帮助中心只写「Flash-Lite」没写版本);4 Carbon 同日冒烟通入高档;"
                   "2026-11-13 按工程原则 9 移出第 3 代的 3.8 Flash、3.1 Pro,4 Hydrogen 入清单;"
                   "2026-11-15 用户定主力 = 消费端默认在最新一代里的同款,填 4 Hydrogen(原主力 4 Argon);"
                   "2026-11-15 按工程原则 10 复核:列型号接口没有新的第 4 代型号;"
                   "帮助中心里 Flash 换成了 4 Hydrogen(主力已在应用里),标记不变;"
                   "2026-11-19 按工程原则 10 核对:列型号接口新出现 gemini-4-neon(官方 11-18 发布);"
                   "2026-11-20 官方型号页 + 价目页($3 / $15)核对、冒烟通过,用户定入清单(只有高档测到);"
                   "应用里它给 Pro / Ultra 订阅用户选(官方发布文与 9to5Google 的摘要,没逐字读原文),"
                   "免费版默认与主力不受影响,标记不变;"
                   "2026-11-20 用户定「每家只留两个」:消费端默认 + 主力,4 Neon / 4 Argon / 4 Carbon 删掉;"
                   "2026-11-23 按工程原则 10 复核:列型号接口没有新的第 4 代型号;帮助中心「About Gemini models」"
                   "改写了(浏览器逐字读过):没有付费方案的聊天只有 Auto,在 4 Hydrogen 和一款没点名的"
                   "「fast, lightweight model」之间自动分配,整页不再出现 Flash-Lite 这个名字,"
                   "Pro 档的 Argon 换成了 Neon;"
                   "消费端默认填 3.5 Flash-Lite 的依据(「多数走 Flash-Lite」)今天在这页上对不出原话,"
                   "标记没动,已报用户、等用户定")

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


def _usage(data: dict[str, Any]) -> dict[str, float]:
    """这次调用的计费用量:输入 / 输出 / 思考 token、唯一非空检索词数(接地回来的内容平台不按 token 计)。"""
    usage = data.get("usage") or {}
    return {
        "input_tokens": float(usage.get("total_input_tokens") or 0),
        "output_tokens": float(usage.get("total_output_tokens") or 0),
        "thought_tokens": float(usage.get("total_thought_tokens") or 0),
        "search_queries": float(len(set(_queries(data.get("steps") or [])))),
    }


def _cost_usd(data: dict[str, Any], model: str) -> float:
    """官方价目确定性折算:用量 × 牌价。"""
    return priced(_PRICE_TABLE[model], _usage(data))


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
    steps = require_list(data, "steps")
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
        queries = _queries(steps)
        for step in steps:
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
    except STRUCTURE_ERRORS as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise malformed(exc, data, lambda: _cost_usd(data, model)) from exc

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


class GeminiGroundingAdapter(HttpAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(headers={"x-goog-api-key": api_key}, client=client)

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="gemini",
            surface="gemini_grounding",
            display_name="Gemini",
            api_name="Gemini API",
            models=list(_TOKEN_PRICES_PER_MTOK),  # 以官方模型页为准,与价目表同步核对
            helper_models=list(_HELPER_PRICES_PER_MTOK),
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
            # 口径:推导上界,按型号各算(3 条检索词 + 探针实测 token 量),有足量实测后
            # 确认页改读实测均值:默认型号(3.5 Flash-Lite)3×$0.014 + (200×$0.30 + 3000×$2.50)/1e6 ≈ $0.050
            # 4 Hydrogen 不按这个公式:2026-11-20 试价它一次搜 10–17 个词(公式假设 3 个),三次 $0.19 / $0.31 / $0.26,
            # 是公式算出来 $0.065 的四倍,撞了试价的上限只问到 3 道。同日用户同意把剩下 7 道问完:十次 $0.12–0.31,
            # 均 $0.20(每次搜 5–17 个词)。它的声明价用这十次的均值;中文单库里已有这十次,确认页读库里的
            est_cost_per_call_usd={
                **{model: round(3 * _SEARCH_USD_PER_QUERY + (200 * price_in + 3000 * price_out) / 1e6, 3)
                   for model, (price_in, price_out) in _TOKEN_PRICES_PER_MTOK.items()},
                **_MEASURED_USD_PER_CALL,
            },
            # 闭卷腿口径:同源推导,无检索项:默认型号 (2300+950)×$2.50/1e6 ≈ $0.008
            est_cost_per_call_search_disabled_usd={
                model: round((2300 + 950) * price_out / 1e6, 3)
                for model, (_, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        return _usage(raw_payload) if isinstance(raw_payload, dict) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload),平台返回 → SurfaceRun(_translate)。"""
        _CAPABILITIES.require_request(request)
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        model = known_model(request.model, _DEFAULT_MODEL, _ALL_PRICES_PER_MTOK)
        data, latency_ms, started_at = self._post(
            f"{BASE_URL}/v1beta/interactions", payload=_payload(request, model, search_disabled),
            timeout_s=request.timeout_s,
        )
        return _translate(request, model, search_disabled, data, latency_ms, started_at)

    def healthcheck(self) -> bool:
        return self._probe(f"{BASE_URL}/v1beta/interactions",
                           payload={"model": _DEFAULT_MODEL, "input": "ping", "store": False}, timeout_s=30.0)
