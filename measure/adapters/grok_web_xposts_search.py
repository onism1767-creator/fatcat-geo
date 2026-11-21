"""Grok 生成+引用通道,开着 X 帖子搜索的那一台(2026-11-20,用户定「让用户可以自选,多一个 grok 选项里可以打钩的选项」)。

和只搜网页的那台(adapters/grok_web_search.py)是同一个接口、同一份请求与翻译,只多带一个工具 {"type": "x_search"}。
它是一台独立的仪器(新 surface = 一个适配器文件 + config/surfaces.yaml 一行,L2 / L3 零改动,硬边界 10):
回答分开存、分开报、分开估价、不和只搜网页的回答互相复用——开不开 X 是口径的一部分。
确认页把它收在 Grok 下面当一个可勾的选项(meta 的 variant_of / variant_label),勾上 = 测这一台、不测只搜网页的那台。

开着 X 时回包多出来的东西、计费、看得到与看不到的,都写在 adapters/grok_web_search.py 的模块说明里(来自 2026-11-18
两个工具都开着的 6 份真实回包:口碑题 4 / 4 次搜了 X,推荐题 2 / 2 次没搜——搜不搜它自己定)。
这一台的可观测面比那台多 / 少的:
- 检索词:多了 X 搜索的搜索词(和网页搜索的词按出现顺序放在一起);
- 候选来源:**只有网页的**。搜到了哪些帖子平台不逐条列,retrieved_sources 里没有帖子——不拿别处的清单代偿(铁律 7);
- 引用:回答里标了出处的帖子(x.com 的网址)照常进引用池;
- 闭卷腿:不带任何工具,和那台的闭卷腿是同一种请求。
"""

from __future__ import annotations

from adapters.base import MethodNote, SurfaceMeta
from adapters.grok_web_search import (
    _CAPABILITIES,
    _CONSUMER_DEFAULT_MODEL,
    _DEFAULT_MODEL,
    _MAIN_MODEL,
    _MODELS_CHECKED,
    _NO_MARKS_NOTE,
    _PRICE_TABLE,
    _TOKEN_PRICES_PER_MTOK,
    MAX_OUTPUT_TOKENS,
    SURFACE,
    X_POSTS_KEY,
    GrokWebSearchAdapter,
)

X_SURFACE = "grok_web_xposts_search"
# 每抓回一条帖子 $0.005:四份真实回包按这个算与平台自报费用一个 tick 不差(来历见那台的模块说明)
_X_POST_USD = 5.0 / 1000.0
# 被测型号与只搜网页的那台相同;辅助型号(裁判)不在这台上
_X_PRICE_TABLE: dict[str, dict[str, float]] = {
    model: {**_PRICE_TABLE[model], X_POSTS_KEY: _X_POST_USD} for model in _TOKEN_PRICES_PER_MTOK
}
# 联网问一次的实测均值(样本很少:2026-11-18 两个工具都开着各 3 次,含两道口碑题一道推荐题):
# grok-4.5 $0.21 / $0.27 / $0.23,均 $0.23;grok-4.7 $0.23 / $0.34 / $0.57(最后一次过了 20 万 token),均 $0.38
_X_MEASURED_USD_PER_CALL = {"grok-4.5": 0.23, "grok-4.7": 0.38}

# 确认页 Grok 下面那个勾选框旁边的话(句子是 Claude 拟的)
_X_LABEL = MethodNote(zh="也搜 X 上的帖子(搜不搜由 Grok 自己定;搜了会更贵)",
                      en="Also search posts on X (Grok decides whether to; it costs more when it does)")
# 报告「这次是怎么测的」里的一句(句子是 Claude 拟的):这一单开了 X,以及看得到什么
_X_ON_NOTE = MethodNote(
    zh="本次 Grok 开了 X 帖子搜索:搜不搜 X 由它自己定;它搜到了哪些帖子看不到,只看得到回答里标了出处的帖子。",
    en="X post search was turned on for Grok in this run: Grok decides whether to search X. Which posts it found "
       "cannot be observed; only the posts it marks as sources in the answer can.",
)


class GrokWebXPostsSearchAdapter(GrokWebSearchAdapter):
    surface = X_SURFACE
    x_search = True
    price_table = _X_PRICE_TABLE

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="xai",
            surface=X_SURFACE,
            display_name="Grok + X",
            api_name="Grok API + X",
            models=list(_TOKEN_PRICES_PER_MTOK),
            default_model=_DEFAULT_MODEL,
            consumer_default_model=_CONSUMER_DEFAULT_MODEL,
            main_model=_MAIN_MODEL,
            models_checked=(_MODELS_CHECKED + ";2026-11-20 用户定 X 帖子搜索让用户自己勾:"
                            "开着 X 的是这台仪器,型号与标记同只搜网页的那台"),
            capabilities=_CAPABILITIES,
            price_table=_X_PRICE_TABLE,
            method_notes=[_X_ON_NOTE, _NO_MARKS_NOTE],
            est_cost_per_call_usd=dict(_X_MEASURED_USD_PER_CALL),
            # 闭卷腿不带工具,和只搜网页的那台同一种请求、同一个推导上界
            est_cost_per_call_search_disabled_usd={
                model: round((500 * price_in + MAX_OUTPUT_TOKENS * price_out) / 1e6, 3)
                for model, (price_in, _, price_out) in _TOKEN_PRICES_PER_MTOK.items()
            },
            variant_of=SURFACE,
            variant_label=_X_LABEL,
        )
