"""Grok 生成+引用通道(xAI 的 Responses API + web_search 托管工具;2026-11-17 接入,用户定)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

口径(2026-11-17 按官方文档 docs.x.ai 与四发真实回包核对;官方文档没给原始返回的样子,下面的形状都来自真实回包):
- 工具只开 {"type": "web_search"},只放官方文档写了的参数。回包会把工具回显成带 search_context_size="medium",
  那是平台默认值;文档没写这个参数,所以不发(发了也不知道它认不认)。
- **不开 X 帖子搜索(x_search)**:用户 2026-11-17 定「试一轮,搜不到就砍掉」。同一道题把网页与 X 帖子两个工具都开着
  问了一次(grok-4.5,推荐类的题):搜了 6 次网页、X 帖子 0 次(平台自报 x_search_calls=0、x_posts_fetched=0)→ 砍掉。
  11-18 用户定补一轮(含口碑题),两个工具都开着,4.5 / 4.7 各 3 道:**口碑题 4 / 4 次都去搜了 X**(每次 1–5 回、
  拿回 5–32 条帖子;4.5 有一次把 4 条帖子挂在正文上当出处),**推荐题 2 / 2 次没搜**(加上 11-17 那次是 3 / 3)。
  也就是它搜不搜 X 看题型,不是一律不搜。这台仪器仍然不开(和另外四家同口径,都只搜网页);
  报告「这次是怎么测的」里写一句(_NO_X_NOTE)。
  **2026-11-20 用户定「让用户可以自选」**:开着 X 搜索的是另一台仪器 grok_web_xposts_search
  (`adapters/grok_web_xposts_search.py`,本模块的请求与翻译它都复用,只多一个开关),
  确认页把它收在 Grok 下面当一个可勾的选项。开着 X 时回包多出来的东西
  (都来自 11-18 那 6 份真实回包):
  · X 搜索是 custom_tool_call 项,name = x_keyword_search / x_semantic_search,input 是一段 JSON 字符串、里面有 query;
    这一项只有搜索词,**不列搜到了哪些帖子**(网页搜索的 action.sources 那样的清单它没有);
  · 回答里标了出处的帖子照常是落在正文上的 url_citation 锚注(网址 x.com/…/status/…),进引用池;
  · 计费:每抓回一条帖子 $0.005(usage.server_side_tool_usage_details.x_posts_fetched;x_search_calls 本身不另收)——
    四份搜了 X 的回包按这个算与平台自报费用一个 tick 不差(5 / 10 / 29 / 32 条帖子)。
- store=false:测量调用不在平台侧留存;不设推理力度与温度:测平台默认状态
  (回包写明默认推理力度:grok-4.5 / grok-4.7 是 high,grok-4.3 是 low)。
- max_output_tokens = MAX_OUTPUT_TOKENS:回答长度保险丝(成本上界的一部分),不放宽;推理 token 也占它。

本 surface 的可观测面:
- 检索词:web_search_call.action(type=search)的 query,一次动作一个词;
- 候选来源:同一个 action 的 sources(只有网址,没有标题和排名,位次即顺序;不用另外开开关,默认就给);
- 打开的网页:web_search_call.action(type=open_page)的 url,一次动作一个网址,照记进 opened_sources。
  2026-11-20 才接上:11-17 / 18 手头的 70 个检索动作全是 search,当时写成了「没暴露、记 NOT_OBSERVABLE」;
  11-19 试价的 20 份回答里有 10 个 open_page(grok-4.5 八个、grok-4.7 两个),11-18 两个工具都开着的一份里也有两个——
  是我拿「没看见」当了「没有」(错误日志 2026-11-20)。那 20 份已入库的回答 opened_sources 仍是当时记的「看不到」
  (原始层不改),原始回包里有,要用可以重新解析;
- 引用:message 正文段落的 url_citation 锚注(平台默认在正文里插 [[n]](网址) 并给锚注),按首次出现去重成引用池;
  锚注的 title 是引用编号(「1」「2」),不是网页标题 → Citation.title 记 None,不拿编号冒充标题;
  **只认落在正文一段字上的锚注**(end_index > start_index)。2026-11-17 冒烟:grok-4.7 一次回答正文里没有任何
  [[n]] 标记,却把搜到的 65 个网页全部挂成锚注(start_index = end_index = 0、title 就是网址,和 sources 一个不差)——
  那是「搜到的」又报了一遍,不是「引用的」;搜到与引用本来就分两格记(用户 2026-11-18 确认),它们只进 retrieved_sources。
  11-18 又问了 grok-4.7 两道题(一英一中),三次全是这样:正文零标记、锚注全是零宽度、与去重后的 sources 一个不差;
  同样的题 grok-4.5 三次都给落在正文上的锚注。所以按回包本身机械地分三种(与当年另一家「搜了却没对上」同口径,铁律 7):
  有落在正文上的锚注 → 照记;只有零宽度清单、一条落在正文上的都没有 → 哪些被引用看不到,citations_available=False
  (不记「引用 0 条」);什么锚注都没有 → 真的没引,0 条。
  11-18 把手头全部真实回包重数了一遍:grok-4.7 搜过的 7 份,0 份在正文里标出处;grok-4.5 的 8 份里 7 份标了、1 份只给清单。
  想明确要求它标也不行:请求里放 include=["inline_citations"](官方文档里另一套接口的写法)→ HTTP 400
  「Argument not supported」(用户同意后试的,没花钱);这个接口上正文标出处是默认开着的,只有关掉的开关;
- 正文:一次回答可能有不止一个 message 项(真实回包见过:搜索前先说一句、搜完再给正式回答),
  按出现顺序全收,项与项之间空一行;正文里的 [[n]](网址) 原样保留;
- 没见到任何 web_search_call = search_triggered=False 的真实负观测;search_disabled = 不带工具;
- 检索动作失败(web_search_call.status == "failed")= 本次采样被检索基础设施污染,按瞬时错误上抛,与另外几家同口径;
- 顶层 status:completed / incomplete(截断,回答仍有效)算成功;其余(failed 等)按瞬时错误上抛、花费随失败行落;
- 没查到机器可读的拒答信号:不从正文猜「算不算拒答」(属 L2);
- 地区、强制检索:官方文档没写对应参数,不声明(请求了就报错,不静默降级)。
- **搜索次数封不了顶**(2026-11-18 用户定「只当保险丝」后真实试了四次,都没限住,所以不发任何封顶参数):
  它按「轮」搜,每轮同时搜 3–5 回,一般 2 轮,多的 3–4 轮
  (15 份真实回包,含下面 4 份试封顶的:2 轮 8 次、3 轮 5 次、4 轮 2 次);
  2 轮输入约 6–9 万 token,3 轮约 12–15 万,4 轮过 20 万(整单双倍价)。
  `max_tool_calls`(回包顶层有这个字段)给 2:照样搜了 10 回,回显仍是空;
  `max_turns`(官方 X 搜索页的原始请求示例里写在顶层)给 2:搜了 2 轮 7 回(2 轮本来就常见,说明不了);
  给 1:grok-4.5 搜了 2 轮 8 回、grok-4.7 搜了 4 轮 11 回并撞上双倍价——没限住。
  花费只能靠调度器的费用上限把守。

费用:平台自报(usage.cost_in_usd_ticks,1 美元 = 10^10 个 tick),照搬;没报时按牌价折算(预算闸门不能失明)。
牌价与自报费用拿三发真实回包验算过,一个 tick 不差:
没命中缓存的输入 + 命中缓存的输入 + 输出(含推理)+ 网页搜索每次 $0.005。
单次提示达到 20 万 token 时,这一次的全部 token(输入、命中缓存的输入、输出)按高一档的价计,正好是两倍;
搜索费不变(官方价目页每个型号列两行:< 200k / ≥ 200k prompt tokens,2026-11-19 复核)。
这一档是另一组计费项(键带 `_long`),牌价表里各有各的价:用量 × 牌价在两档都等于平台自报的数,
确认页按「平均用量 × 牌价」估出来的才不会偏低(2026-11-19 试价发现:原来牌价表只写 20 万以内的价,
grok-4.7 十次里两次过线,估出来 $0.24、实际均价 $0.30,错误日志同日)。
真实撞到过四次,都是 grok-4.7,按上面的算法验算一个 tick 不差:11-18 两次($0.54 / $0.57),
11-19 两次(输入 200,266 与 314,150 token,$0.70 / $0.69);同一批里输入 197,937 的那次没过线,按平常价。
搜索来的内容都算输入,搜得多就过线。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from adapters.base import (
    AdapterTransientError,
    CapabilityNotSupportedError,
    CapabilityRegistry,
    MethodNote,
    SurfaceMeta,
    known_model,
    priced,
)
from adapters.http_support import STRUCTURE_ERRORS, HttpAdapter, domain_of, full_body, malformed, require_list, spent_on
from engine.models import (
    Answer,
    AskRequest,
    Citation,
    JsonValue,
    Outcome,
    RetrievedSource,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.x.ai"
RESPONSES_URL = f"{BASE_URL}/v1/responses"
SURFACE = "grok_web_search"
TOOL_TYPE = "web_search"  # 改动前必须重做真实对照并更新 golden
X_TOOL_TYPE = "x_search"  # 只有开着 X 帖子搜索的那台仪器(grok_web_xposts_search)带这个工具
X_CALL_PREFIX = "x_"  # X 搜索动作的名字:x_keyword_search / x_semantic_search(真实回包)
X_POSTS_KEY = "x_posts"  # 计费项:抓回的 X 帖子条数(牌价表里有这一项的就是开着 X 搜索的仪器)
MAX_OUTPUT_TOKENS = 4096  # 回答长度保险丝(成本上界的一部分)
_SUCCESS_STATUSES = ("completed", "incomplete")  # incomplete = 截断,回答仍有效
_TICKS_PER_USD = 1e10  # usage.cost_in_usd_ticks 的单位(三发真实回包与牌价验算一致)

# 官方价目(2026-11-17 核对 docs.x.ai 价目页,并与接口 /v1/language-models 自报的价目逐项对上):
# 型号 → (输入, 命中缓存的输入, 输出) USD / MTok,20 万 token 以内的档;
# 到了 20 万的那一档三项都是两倍(见模块说明「费用」)。
# 被测清单(用户 2026-11-17 定「4.5 + 4.7」,工程原则 9):
#   grok-4.5:Grok 应用里用的那款——官方 2026-07-22 公告「Grok 4.5 now powers Grok wherever you use it」
#             (grok.com / X / iOS / Android);之后 4.6、4.7 的公告只说进了 Cursor、Grok Build 与接口,没提进应用;
#   grok-4.7:最新一代(2026-09-21 发布,官方型号页:聊天与写代码都用它)。
# 上一款 grok-4.6 与更早的 grok-4.3 / grok-4.20 系列不入被测清单。
_TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "grok-4.5": (2.00, 0.30, 6.00),
    "grok-4.7": (2.00, 0.50, 6.00),
}
# 辅助型号:只做裁判这类不联网小调用,不被测(不进档位、不上确认页)。挑便宜的一款;
# 2026-11-17 真实回包:不带工具时没有任何检索动作,费用与牌价对得上。
HELPER_MODEL = "grok-4.3"
_HELPER_PRICES_PER_MTOK = (1.25, 0.20, 2.50)
_SEARCH_USD_PER_CALL = 5.0 / 1000.0  # 网页搜索 $5 / 千次(官方价目页「Tools pricing」)
# 联网问一次的实测均值(确认页在库里攒够实测前用它估价;来历见 meta() 里的口径说明)
_MEASURED_USD_PER_CALL = {"grok-4.5": 0.16, "grok-4.7": 0.31}
_LONG_PROMPT_TOKENS = 200_000  # 单次提示(平台报的 input_tokens,含命中缓存的)到这个数,全部 token 按高一档计
_LONG_PRICE_FACTOR = 2.0  # 官方价目页「≥ 200k prompt tokens」一行的三项价都是「< 200k」一行的两倍


def _prices(price_in: float, price_cached: float, price_out: float) -> dict[str, float]:
    """牌价(用量 × 牌价 = 费用;计费项与 _usage 读出的键一致):token 按每个 token 折,搜索按每次。
    提示到了 20 万 token 的那一档是另一组计费项(`_long`),价是两倍;搜索费两档一样。"""
    usual = {"input_tokens": price_in / 1e6, "cached_input_tokens": price_cached / 1e6,
             "output_tokens": price_out / 1e6}
    return {**usual, **{f"{key}_long": price * _LONG_PRICE_FACTOR for key, price in usual.items()},
            "searches": _SEARCH_USD_PER_CALL}


_PRICE_TABLE: dict[str, dict[str, float]] = {
    **{model: _prices(*prices) for model, prices in _TOKEN_PRICES_PER_MTOK.items()},
    HELPER_MODEL: _prices(*_HELPER_PRICES_PER_MTOK),
}
_DEFAULT_MODEL = "grok-4.5"
# 型号标记(配置资产;规矩见 CLAUDE.md 工程原则 9 / 10):
# 消费端默认 = grok-4.5(用户 2026-11-18 确认「的确默认是4.5」;官方依据是上面那条 07-22 公告,
# 官方没写免费版与付费版默认是否不同);
# 主力 = 消费端默认那款在最新一代里的同款 = grok-4.7(Grok 只有一条产品线,最新一代就是它)。
_CONSUMER_DEFAULT_MODEL = "grok-4.5"
_MAIN_MODEL = "grok-4.7"
_MODELS_CHECKED = ("2026-11-17 接口列型号(免费):grok-4.7 / 4.6 / 4.5 / 4.3 / 4.20 系列 / grok-build-0.1;"
                   "官方型号页(09-21 更新):旗舰 Grok 4.7,聊天与写代码都用它;价目页逐项核对,与接口自报价目一致;"
                   "官方公告 07-22:Grok 4.5 进 grok.com / X / iOS / Android,"
                   "之后 4.6(08-12)、4.7(09-21)的公告没提进应用;"
                   "应用默认是 4.5:用户 11-18 确认(官方没写免费版与付费版是否不同);"
                   "用户 11-17 定清单 = 4.5 + 4.7、只搜网页(X 帖子试一轮没搜,砍掉);"
                   "冒烟 11-17 / 18:4.5 一次 $0.12、4.7 三次 $0.12–0.22,都没截断;"
                   "4.7 三次都不在正文里标出处(引用记看不到),4.5 三次都标")

# 报告「这次是怎么测的」里的一句(用户 2026-11-18 定的原话):接口这边没开 X 帖子搜索,应用里会搜
_NO_X_NOTE = MethodNote(zh="本次 Grok 的结果不包含 X 内容。",
                        en="Grok's results in this run do not include content from X.")
# 只在这一单测了 grok-4.7 时写(用户 2026-11-18:「加一行提示解释一下这个单独型号的状态」;句子是 Claude 拟的):
# 它 17 份真实回答(11-19 试价后)都只附搜到的清单、不在正文里标出处,那样的回答引用记「看不到」(见模块说明「引用」一条)
_NO_MARKS_NOTE = MethodNote(
    models=["grok-4.7"],
    zh="Grok 4.7 的回答通常不标出处,它引用了哪些网页看不到(不是 0);它搜到了哪些网页照常统计。",
    en="Grok 4.7 usually does not mark its sources in the answer, so which pages it cited cannot be observed "
       "(this is not zero); the pages it found are still counted.",
)

_CAPABILITIES = CapabilityRegistry(
    supports_query_trace=True,
    supports_citations=True,
    supports_search_disabled=True,
)


def _usage(data: dict[str, Any], *, x_search: bool = False) -> dict[str, float]:
    """这次调用的计费用量,键与牌价表一致:输入分两档(没命中缓存的 / 命中缓存的,之和 = 平台报的 input_tokens,
    检索回来的内容已计入)、输出 token(平台已把推理 token 计入)、计费的网页搜索次数(平台自报)。
    提示到了 20 万 token 的那一次,三项 token 都记在 `_long` 的计费项下(那一档另有价),平常的三项记 0。
    开着 X 搜索的那台仪器多一项:抓回的帖子条数(按条计费)。"""
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    tools = usage.get("server_side_tool_usage_details") or {}
    total_input = float(usage.get("input_tokens") or 0)
    cached = float(details.get("cached_tokens") or 0)
    tokens = {
        "input_tokens": max(0.0, total_input - cached),
        "cached_input_tokens": cached,
        "output_tokens": float(usage.get("output_tokens") or 0),
    }
    long_prompt = total_input >= _LONG_PROMPT_TOKENS
    found = {
        **{key: 0.0 if long_prompt else quantity for key, quantity in tokens.items()},
        **{f"{key}_long": quantity if long_prompt else 0.0 for key, quantity in tokens.items()},
        "searches": float(tools.get("web_search_calls") or 0),
    }
    if x_search:
        found[X_POSTS_KEY] = float(tools.get("x_posts_fetched") or 0)
    return found


def _cost_usd(data: dict[str, Any], prices: dict[str, float]) -> float:
    """平台自报的费用照搬;没报就按牌价折算(确定性折算,不是臆造——预算闸门不能因为少一个字段就失明)。
    prices = 这台仪器这个型号的牌价(带不带 X 帖子那一项,跟仪器走)。"""
    ticks = (data.get("usage") or {}).get("cost_in_usd_ticks")
    if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
        return ticks / _TICKS_PER_USD
    return priced(prices, _usage(data, x_search=X_POSTS_KEY in prices))


def _anchored(note: dict[str, Any]) -> bool:
    """这条锚注有没有落在正文的一段字上(end_index > start_index)。平台有时把搜到的网页整份再挂一遍、位置全是 0
    (见模块说明「引用」一条):那是「搜到的」,已经记在 retrieved_sources,不进引用池。"""
    return int(note.get("end_index") or 0) > int(note.get("start_index") or 0)


def _payload(request: AskRequest, model: str, search_disabled: bool, *, x_search: bool = False) -> dict[str, Any]:
    """AskRequest → 平台请求体(口径钉死)。x_search = 这台仪器开着 X 帖子搜索(多带一个工具,别的都一样)。"""
    payload: dict[str, Any] = {
        "model": model,
        "input": request.prompt_text,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
    }
    if not search_disabled:
        payload["tools"] = [{"type": TOOL_TYPE}] + ([{"type": X_TOOL_TYPE}] if x_search else [])
    return payload


def _x_query(item: dict[str, Any]) -> str | None:
    """X 搜索动作的搜索词:custom_tool_call 的 input 是一段 JSON 字符串,里面的 query。读不出来就没有,不猜。"""
    try:
        query = json.loads(item.get("input") or "{}").get("query")
    except (ValueError, AttributeError):
        return None
    return query if isinstance(query, str) and query else None


def _translate(
    request: AskRequest,
    model: str,
    search_disabled: bool,
    data: Any,
    latency_ms: int,
    started_at: datetime,
    *,
    surface: str = SURFACE,
    prices: dict[str, float] | None = None,
) -> SurfaceRun:
    """平台返回 → SurfaceRun。只翻译,不判定;结构不对按 Permanent 上抛(平台改格式须显式暴露)。
    surface / prices 跟仪器走(开着 X 搜索的那台用它自己的名字和牌价);不给 = 只搜网页的这台。"""
    prices = prices if prices is not None else _PRICE_TABLE[model]
    output = require_list(data, "output")
    status = data.get("status")
    if status not in _SUCCESS_STATUSES:  # 平台回了话、钱照计,却报失败:花费随失败行落
        raise AdapterTransientError(f"响应 status={status!r}: {data.get('error')}", response_body=full_body(data),
                                    cost_usd=spent_on(lambda: _cost_usd(data, prices)))

    messages: list[str] = []
    queries: list[str] = []
    sources: list[RetrievedSource] = []
    opened: list[str] = []
    citations: list[Citation] = []
    cited_urls: set[str] = set()
    search_observed = False
    unanchored_seen = False  # 见过没落在正文上的锚注(平台把搜到的清单又挂了一遍)
    try:
        for item in output:
            item_type = item.get("type")
            if item_type == "web_search_call":
                search_observed = True
                if item.get("status") == "failed":  # 检索动作本身失败:本次采样被检索基础设施污染,交调度器重采
                    raise AdapterTransientError(f"web_search_call 检索动作失败(status=failed,id={item.get('id')!r})",
                                                response_body=full_body(data),
                                                cost_usd=spent_on(lambda: _cost_usd(data, prices)))
                action = item.get("action") or {}
                if action.get("type") == "open_page":
                    if action.get("url"):
                        opened.append(action["url"])  # 打开了这一页(平台自己报的浏览动作)
                    continue
                if action.get("type") != "search":
                    continue  # 没见过的动作类型不猜,原样留在 raw_payload
                if action.get("query"):
                    queries.append(action["query"])
                for entry in action.get("sources") or []:
                    sources.append(RetrievedSource(
                        url=entry["url"],
                        domain=domain_of(entry["url"]),
                        title=None,  # 平台只给网址,不臆造
                        rank=len(sources) + 1,  # 有序列表位次
                    ))
            elif item_type == "custom_tool_call" and str(item.get("name") or "").startswith(X_CALL_PREFIX):
                # X 帖子搜索(只在开着它的那台仪器的回包里出现):搜索词可见;搜到了哪些帖子这一项不列
                search_observed = True
                if item.get("status") == "failed":  # 和网页搜索同口径:检索动作失败 = 本次采样被污染,交调度器重采
                    raise AdapterTransientError(f"{item.get('name')} 检索动作失败(status=failed,id={item.get('id')!r})",
                                                response_body=full_body(data),
                                                cost_usd=spent_on(lambda: _cost_usd(data, prices)))
                query = _x_query(item)
                if query:
                    queries.append(query)
            elif item_type == "message":
                parts: list[str] = []
                for part in item.get("content") or []:
                    if part.get("type") != "output_text":
                        continue
                    parts.append(part["text"])
                    for note in part.get("annotations") or []:
                        if note.get("type") != "url_citation":
                            continue
                        if not _anchored(note):
                            unanchored_seen = True
                            continue
                        if note["url"] in cited_urls:
                            continue  # 引用池按首次出现去重;重复锚点在 raw_payload
                        cited_urls.add(note["url"])
                        citations.append(Citation(
                            url=note["url"],
                            domain=domain_of(note["url"]),
                            title=None,  # 锚注的 title 是引用编号,不是网页标题
                            position=len(citations) + 1,
                        ))
                if parts:
                    messages.append("".join(parts))
    except STRUCTURE_ERRORS as exc:  # 列表项不是 dict、字段类型怪:也算结构不符
        raise malformed(exc, data, lambda: _cost_usd(data, prices)) from exc

    answer_text = "\n\n".join(messages)  # 不止一个 message 项时(搜索前先说一句)项与项之间空一行
    # 平台只给了没落在正文上的清单、一条落在正文上的都没有:哪些被引用了看不到(不是「引用 0 条」)
    citations_hidden = unanchored_seen and not citations
    return SurfaceRun(
        run_id=request.run_id,
        prompt_id=request.prompt_id,
        run_index=request.run_index,
        provider="xai",
        surface=surface,
        model=model,
        search_mode=request.search_mode,
        locale=request.locale,
        region=request.region,
        outcome=Outcome.SUCCESS,
        search_trace=SearchTrace(
            # 检索是显式工具调用:触发 / 检索词 / 候选 / 打开的网页都真实可观测
            search_triggered=False if search_disabled else search_observed,
            generated_queries=queries,
            retrieved_sources=sources,
            opened_sources=opened,
        ),
        answer=(
            Answer(answer_text=answer_text, citations_available=not (search_disabled or citations_hidden),
                   citations=citations)
            if answer_text
            else None
        ),
        raw_payload=data,
        timestamp=started_at,
        latency_ms=latency_ms,
        cost_usd=_cost_usd(data, prices),
    )


class GrokWebSearchAdapter(HttpAdapter):
    # 开着 X 帖子搜索的那台仪器(adapters/grok_web_xposts_search.py)是本类的子类,只换下面三处和 meta();请求与翻译共用
    surface = SURFACE
    x_search = False  # 请求里带不带 X 帖子搜索这个工具
    price_table = _PRICE_TABLE  # 型号 → 牌价(开着 X 的那台多一项「抓回的帖子」)

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(headers={"Authorization": f"Bearer {api_key}"}, client=client)

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="xai",
            surface=SURFACE,
            display_name="Grok",
            api_name="Grok API",
            models=list(_TOKEN_PRICES_PER_MTOK),
            helper_models=[HELPER_MODEL],
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=_MODELS_CHECKED,
            capabilities=_CAPABILITIES,
            price_table=_PRICE_TABLE,
            method_notes=[_NO_X_NOTE, _NO_MARKS_NOTE],
            # 口径:实测均值(只搜网页的真实回答,11-17 / 18 冒烟与试验 + 11-19 试价 10 道真单题):
            # grok-4.5 十六次均 $0.16(试价十次 $0.01–0.24,均 $0.144);
            # grok-4.7 十五次均 $0.31(试价十次 $0.13–0.70,均 $0.304),十五次里四次过了 20 万 token 那条线、按高一档计。
            # 搜索次数封不了顶(见模块说明),所以写不出靠得住的上界(原来写的「推导上界 $0.225」被打破,错误日志 11-18)。
            # 中文单库里已有十次实测,确认页读库里的「平均用量 × 牌价」;这个数只在没有实测时用。真实花费由预算闸门把守。
            est_cost_per_call_usd=dict(_MEASURED_USD_PER_CALL),
            # 闭卷腿口径:同源推导上界,无检索项:(500×$2 + 4096×$6)/1e6 ≈ $0.026
            est_cost_per_call_search_disabled_usd={
                model: round((500 * price_in + MAX_OUTPUT_TOKENS * price_out) / 1e6, 3)
                for model, (price_in, _, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
        )

    def usage_of(self, raw_payload: JsonValue) -> dict[str, float]:
        return _usage(raw_payload, x_search=self.x_search) if isinstance(raw_payload, dict) else {}

    def ask(self, request: AskRequest) -> SurfaceRun:
        """适配器只做两件事(CLAUDE.md 硬边界 3):AskRequest → 平台调用(_payload),平台返回 → SurfaceRun(_translate)。"""
        _CAPABILITIES.require_request(request)
        search_disabled = request.search_mode is SearchMode.SEARCH_DISABLED
        model = known_model(request.model, _DEFAULT_MODEL, self.price_table)
        if model == HELPER_MODEL and not search_disabled:
            raise CapabilityNotSupportedError(f"{HELPER_MODEL} 只做不联网的小调用(search_disabled),不被测")
        data, latency_ms, started_at = self._post(
            RESPONSES_URL, payload=_payload(request, model, search_disabled, x_search=self.x_search),
            timeout_s=request.timeout_s,
        )
        return _translate(request, model, search_disabled, data, latency_ms, started_at,
                          surface=self.surface, prices=self.price_table[model])

    def healthcheck(self) -> bool:
        return self._probe(RESPONSES_URL, payload={
            "model": _DEFAULT_MODEL,
            "input": "ping",
            "max_output_tokens": 16,  # 不带检索工具,成本近零
            "store": False,
        })
