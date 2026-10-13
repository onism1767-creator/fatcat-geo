"""L3 结论码目录与渲染器(16.5 v1.1)。

L3 逻辑只产出 Msg(code+params),本模块负责按语言组句。
zh 模板与 v1.0 的成品语句逐字一致(保持报告连续性);en 为对应译文。
参数值若为 Msg(或 Msg 列表)则递归渲染后再代入——用于嵌套从句。
未登记的 code 渲染时直接 KeyError:宁可炸,不可静默吞结论。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class Msg(BaseModel):
    """一条结构化结论:code 定语义,params 带事实(可嵌套 Msg)。
    定义在本模块(零 engine 依赖),供含 access_audit 在内的全部 L3 使用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    params: dict[str, Any] = {}

    @field_validator("params", mode="after")
    @classmethod
    def _rehydrate(cls, params: dict[str, Any]) -> dict[str, Any]:
        """报告快照落库后从 JSON 回水(v0.4.16):嵌套的 {code, params} 字典复活为 Msg,
        否则渲染器只能把它当普通值打印成字典。"""
        return {key: _rehydrate_value(value) for key, value in params.items()}


def _rehydrate_value(value: Any) -> Any:
    if (
        isinstance(value, dict)
        and set(value) == {"code", "params"}
        and isinstance(value["code"], str)
        and isinstance(value["params"], dict)
    ):
        return Msg(code=value["code"], params=value["params"])
    if isinstance(value, list):
        return [_rehydrate_value(item) for item in value]
    return value


def msg(code: str, **params: Any) -> Msg:
    return Msg(code=code, params=params)


# 比例一律「k/n = 比例」,不出区间(产品文档 15.2;v0.4.26 S2)
_RATE = "{k}/{n} = {share:.0%}"
_RATE_EN = "{k}/{n} = {share:.0%}"

CATALOG: dict[str, dict[str, str]] = {
    # ── 公共 ──────────────────────────────────────────────────────────
    "common.low_confidence_downgrade": {
        "zh": "无效样本占比超阈值(LOW_CONFIDENCE),确定性结论降权为 WEAK(7.3)",
        "en": "Invalid-sample share exceeds threshold (LOW_CONFIDENCE); definitive verdicts are downgraded to WEAK (§7.3)",
    },
    "common.caliber": {
        "zh": "口径(冻结题目集上的观测率,不外推到需求总体):surface={surface} model={model} "
              "题数={n_prompts} N_valid={n_valid}/{n_total} stop_reasons=[{stop_reasons}] "
              "无效:拒答 {n_refused}、空回答 {n_empty}、接口错误 {n_failed}",
        "en": "Caliber (observed rate on the frozen prompt set; not extrapolated to the need population): "
              "surface={surface} model={model} prompts={n_prompts} N_valid={n_valid}/{n_total} "
              "stop_reasons=[{stop_reasons}] invalid: refused {n_refused}, empty {n_empty}, provider errors {n_failed}",
    },
    "common.per_prompt": {
        "zh": "各题并列(同一道题的几次回答是一簇,不是独立样本):{items}",
        "en": "Per prompt (the repeated runs of one prompt form a cluster, not independent samples): {items}",
    },
    "common.prompt_item": {
        "zh": "题{idx} {k}/{n}",
        "en": "prompt {idx} {k}/{n}",
    },
    "common.prompt_item_none": {
        "zh": "题{idx} 无有效样本",
        "en": "prompt {idx} no valid samples",
    },
    "common.prompt_coverage": {
        "zh": "题目覆盖率:{n} 道题里 {k} 道至少命中一次({share:.0%})——防止少数几道题撑起总数;没拿到有效回答的题按没命中算",
        "en": "Prompt coverage: {k} of {n} prompts hit at least once ({share:.0%}) — guards against a few prompts carrying the total; prompts without a valid answer count as not hit",
    },
    # ── RETRIEVAL(仪器不暴露检索链路 → 不可观测;公共证据行供生成端链路复用)──────
    "retrieval.headline.not_observable": {
        "zh": "该仪器不公开检索结果,检索阶段不可观测",
        "en": "This surface does not expose its retrieval results — the retrieval stage is not observable",
    },
    "retrieval.not_observable_note": {
        "zh": "禁止用引用集反推检索(铁律 7),也不从别的接口借数据,故此阶段如实记不可观测",
        "en": "Inferring retrieval from citations is forbidden (iron rule 7) and no other interface's data is borrowed, so this stage is honestly recorded as not observable",
    },
    "retrieval.rank_summary": {
        "zh": "在场样本中的最优位次:中位数第 {median:.0f} 位(最好第 {best} 位,共 {count} 次在场)",
        "en": "Best rank when present: median #{median:.0f} (best #{best}, present {count} times)",
    },
    "retrieval.rank_never": {
        "zh": "品牌域名从未进入检索候选集(k=0)",
        "en": "The brand domain never entered the retrieval candidate set (k=0)",
    },
    "retrieval.thresholds": {
        "zh": "操作阈值(v1 初始值,未经校准):比例 < {fail:.0%} 为 FAIL,≥ {pass_:.0%} 为 PASS",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if the rate is below {fail:.0%}, PASS if at or above {pass_:.0%}",
    },
    # ── RETRIEVAL 生成端检索链路 ──────────────────────────────────────
    "genret.headline.fail": {
        "zh": "在 AI 自己的搜索习惯下,品牌基本不被递到模型手上",
        "en": "Under the AI's own search behavior, the brand rarely reaches the model's hands",
    },
    "genret.headline.weak": {
        "zh": "在 AI 自己的搜索习惯下,品牌不稳定地进入检索候选",
        "en": "Under the AI's own search behavior, the brand enters retrieval candidates inconsistently",
    },
    "genret.headline.pass": {
        "zh": "在 AI 自己的搜索习惯下,品牌稳定进入检索候选",
        "en": "Under the AI's own search behavior, the brand reliably enters retrieval candidates",
    },
    "genret.headline.not_observable": {
        "zh": "生成端检索通道无有效样本,不可观测",
        "en": "The generative-side retrieval channel has no valid samples — not observable",
    },
    "genret.presence": {
        "zh": f"候选在场率(模型自拟检索词换来的候选集中出现品牌域名):{_RATE}",
        "en": f"Candidate presence rate (brand domain among results fetched for model-authored queries): {_RATE_EN}",
    },
    "genret.query_entry": {
        "zh": f"检索词品牌入场率(模型自拟检索词中出现品牌名/别名):{_RATE}",
        "en": f"Query entry rate (brand name/alias appears in model-authored queries): {_RATE_EN}",
    },
    "genret.funnel.pair": {
        "zh": "同仪器漏斗:候选在场 {presence_k}/{presence_n} = {presence_share:.0%} vs 被引 {cited_k}/{cited_n} = {cited_share:.0%}",
        "en": "Same-instrument funnel: candidate presence {presence_k}/{presence_n} = {presence_share:.0%} vs cited {cited_k}/{cited_n} = {cited_share:.0%}",
    },
    "genret.funnel.gap": {
        "zh": "被引比例比在场比例低 {gap:.0%} 个百分点以上——资料递到了模型手上却常落选,缺席主因偏向内容竞争力而非检索可见度(同一批运行内部对比,零跨仪器混淆)",
        "en": "The cited rate is at least {gap:.0%} points below the presence rate — material reaches the model but often loses out; the absence leans toward content competitiveness rather than retrieval visibility (same-run comparison, no cross-instrument confound)",
    },
    "genret.funnel.aligned": {
        "zh": "在场与被引比例相差不足 {gap:.0%}——递上来的资料被采用的程度与在场程度无法区分",
        "en": "Presence and cited rates differ by less than {gap:.0%} — adoption of delivered material is indistinguishable from its presence",
    },
    "genret.triggered": {
        "zh": f"检索触发率:{_RATE};未触发检索的运行计入各分母——不检索本身就是自然行为的一部分",
        "en": f"Search trigger rate: {_RATE_EN}; runs without search count in denominators — not searching is itself part of natural behavior",
    },
    # ── CITATION ─────────────────────────────────────────────────────
    "citation.headline.fail": {
        "zh": "品牌域名基本不进引用集",
        "en": "The brand domain is largely absent from citation sets",
    },
    "citation.headline.weak": {
        "zh": "品牌域名进入引用集但不稳定",
        "en": "The brand domain enters citation sets inconsistently",
    },
    "citation.headline.pass": {
        "zh": "品牌域名稳定进入引用集",
        "en": "The brand domain reliably enters citation sets",
    },
    "citation.headline.not_observable": {
        "zh": "无引用观测通道或无有效样本,引用阶段不可观测",
        "en": "No citation observation channel or no valid samples — the citation stage is not observable",
    },
    "citation.rate": {
        "zh": f"被引比例:{_RATE}",
        "en": f"Citation rate: {_RATE_EN}",
    },
    "citation.primary_rate": {
        "zh": f"主站引用率:{_RATE}(主站注册域名;上一行是任一官网域名)",
        "en": f"Primary-site citation rate: {_RATE_EN} (registered domain of the primary site; the line above counts any brand domain)",
    },
    "citation.by_domain": {
        "zh": "按域名拆分(各官网注册域名各算一次,一条回答引用同一域名多次只算一次):{items}",
        "en": "By domain (each brand registered domain counted once per answer): {items}",
    },
    "citation.domain_item": {
        "zh": "{domain} {k}/{n}",
        "en": "{domain} {k}/{n}",
    },
    "citation.channel_rate": {
        "zh": f"官方渠道引用:{_RATE}(主机名 + 路径前缀精确匹配,名单来自主站自己的声明;单列,不并入官网引用率)",
        "en": f"Official-channel citation rate: {_RATE_EN} (exact host + path-prefix match against channels the primary site itself declares; listed apart, not merged into the brand-site rate)",
    },
    "citation.unresolved": {
        "zh": "{n} 条引用是平台跳转链且还原不出终点,记 NOT_OBSERVABLE,不进分子分母(铁律 7:不拿标题顶替)",
        "en": "{n} citation(s) are platform redirect links whose destination could not be resolved; recorded as NOT_OBSERVABLE and excluded from numerator and denominator (iron rule 7: titles are not used as a stand-in)",
    },
    "citation.thresholds": {
        "zh": "操作阈值(v1 初始值,未经校准):比例 < {fail:.0%} 为 FAIL,≥ {pass_:.0%} 为 PASS",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if the rate is below {fail:.0%}, PASS if at or above {pass_:.0%}",
    },
    "citation.tier_note": {
        "zh": "三档证据口径:提及 → 被引 → 已核验。「已核验」= 本工具自行重抓被引 URL,"
              "确认 HTML 可达且可见正文出现品牌清晰名——不轻信平台引用。抓不到"
              "(被拦截 / 未解析 / 不可达)只是核验器缺数据,不算引用无效;唯一的负面证据是"
              "页面可达但正文未见品牌(软 404/错标指纹)",
        "en": "Three-tier evidence: mentioned → cited → verified. \"Verified\" = this tool "
              "independently re-fetches each cited URL and confirms the HTML is reachable AND the "
              "visible text contains the brand's clear name — platform citations are not trusted "
              "blindly. A failed fetch (blocked / unparsed / unreachable) is missing verifier data, "
              "not an invalid citation; the only negative evidence is a reachable page whose text "
              "lacks the brand (soft-404 / mislabel fingerprint)",
    },
    "citation.verify_tally": {
        "zh": "核验四态(URL 级,{attempted} 条唯一被引 URL):正文见品牌 {match};"
              "正文未见品牌 {mismatch};被拦截(401/403/429/503){blocked};非 HTML 未解析 {unparsed};"
              "其余不可达 {unreachable}",
        "en": "Verification outcomes (per URL, {attempted} unique cited URL(s)): brand found {match}; "
              "brand absent {mismatch}; fetch blocked (401/403/429/503) {blocked}; non-HTML unparsed "
              "{unparsed}; otherwise unreachable {unreachable}",
    },
    "citation.verify_coverage": {
        "zh": "核验覆盖率:{decidable}/{attempted} = {coverage:.0%}(可判定 ÷ 尝试核验);"
              "覆盖率低说明本工具核验器视角受限,不说明平台引用有问题",
        "en": "Verification coverage: {decidable}/{attempted} = {coverage:.0%} (decidable ÷ attempted); "
              "low coverage reflects this tool's verifier vantage, not a platform citation problem",
    },
    "citation.compare.behind": {
        "zh": f"落后竞品 {{name}} {{gap:.0%}} 个百分点以上(其被引 {_RATE})",
        "en": f"At least {{gap:.0%}} points behind competitor {{name}} (their citation rate: {_RATE_EN})",
    },
    "citation.compare.ahead": {
        "zh": f"领先竞品 {{name}} {{gap:.0%}} 个百分点以上(其被引 {_RATE})",
        "en": f"At least {{gap:.0%}} points ahead of competitor {{name}} (their citation rate: {_RATE_EN})",
    },
    "citation.compare.close": {
        "zh": f"与竞品 {{name}} 相差不足 {{gap:.0%}},分不出高低(其被引 {_RATE})",
        "en": f"Within {{gap:.0%}} of competitor {{name}} — not distinguishable (their citation rate: {_RATE_EN})",
    },
    # ── 第三方线(S6.1,v0.4.24 D2 / D3):搜到 → 引用写到你的第三方网页 ────────
    "thirdparty.retrieved.headline.fail": {
        "zh": "第三方线·搜到:正文写到品牌的第三方网页基本不进搜索结果(FAIL)",
        "en": "Third-party line, retrieval: third-party pages that mention the brand rarely enter the search results (FAIL)",
    },
    "thirdparty.retrieved.headline.weak": {
        "zh": "第三方线·搜到:正文写到品牌的第三方网页不稳定地进入搜索结果(WEAK)",
        "en": "Third-party line, retrieval: third-party pages that mention the brand enter the search results inconsistently (WEAK)",
    },
    "thirdparty.retrieved.headline.pass": {
        "zh": "第三方线·搜到:正文写到品牌的第三方网页稳定进入搜索结果(PASS)",
        "en": "Third-party line, retrieval: third-party pages that mention the brand reliably enter the search results (PASS)",
    },
    "thirdparty.retrieved.headline.not_observable": {
        "zh": "第三方线·搜到:该仪器不公开搜索结果或无可核实网页,不可观测",
        "en": "Third-party line, retrieval: this surface exposes no search results or no checkable pages — not observable",
    },
    "thirdparty.cited.headline.fail": {
        "zh": "第三方线·引用:正文写到品牌的第三方网页基本不被当作来源(FAIL)",
        "en": "Third-party line, citation: third-party pages that mention the brand are rarely cited (FAIL)",
    },
    "thirdparty.cited.headline.weak": {
        "zh": "第三方线·引用:正文写到品牌的第三方网页不稳定地被当作来源(WEAK)",
        "en": "Third-party line, citation: third-party pages that mention the brand are cited inconsistently (WEAK)",
    },
    "thirdparty.cited.headline.pass": {
        "zh": "第三方线·引用:正文写到品牌的第三方网页稳定被当作来源(PASS)",
        "en": "Third-party line, citation: third-party pages that mention the brand are reliably cited (PASS)",
    },
    "thirdparty.cited.headline.not_observable": {
        "zh": "第三方线·引用:该仪器不公开引用集或无可核实网页,不可观测",
        "en": "Third-party line, citation: this surface exposes no citations or no checkable pages — not observable",
    },
    "thirdparty.retrieved.rate": {
        "zh": f"第三方线·搜到比例:{_RATE}(有效回答里搜索结果含 ≥ 1 个正文写到品牌的第三方网页;全部看不到的回答不进分母)",
        "en": f"Third-party line, retrieval rate: {_RATE_EN} (valid answers whose search results include ≥ 1 third-party page mentioning the brand; answers where every page is unobservable are excluded)",
    },
    "thirdparty.cited.rate": {
        "zh": f"第三方引用率:{_RATE}(与官网引用率分开计、不相加;全部看不到的回答不进分母)",
        "en": f"Third-party citation rate: {_RATE_EN} (counted apart from the brand-site rate, never added; answers where every page is unobservable are excluded)",
    },
    "thirdparty.lead": {
        "zh": f"其中标题或首段就出现品牌:{_RATE}(位置口径:标题 → 首段 → 正文,只在有「写到你」的回答里算)",
        "en": f"Of those, the brand appears in the title or lead paragraph: {_RATE_EN} (position order title → lead → body; counted only among answers with a mentioning page)",
    },
    "thirdparty.unobservable": {
        "zh": "看不到的网页 {n} 条(不进分母,按原因):{items}",
        "en": "{n} page(s) not observable (excluded from the denominator), by reason: {items}",
    },
    "thirdparty.reason_item": {
        "zh": "{reason} {n}",
        "en": "{reason} {n}",
    },
    "pagereason.ROBOTS_DISALLOWED": {"zh": "站点不允许抓取", "en": "site disallows fetching"},
    "pagereason.BLOCKED": {"zh": "被拦截", "en": "blocked"},
    "pagereason.LOGIN_REQUIRED": {"zh": "需要登录", "en": "login required"},
    "pagereason.EMPTY_TEXT": {"zh": "抽不出正文", "en": "no extractable text"},
    "pagereason.NOT_HTML": {"zh": "不是网页", "en": "not an HTML page"},
    "pagereason.FETCH_FAILED": {"zh": "打不开", "en": "could not be opened"},
    "pagereason.NOT_ATTEMPTED": {"zh": "没来得及开", "en": "not attempted within the time budget"},
    "pagereason.UNRESOLVED": {"zh": "跳转去向查不到", "en": "redirect destination unresolved"},
    "pagereason.UNKNOWN": {"zh": "原因未知", "en": "unknown reason"},
    "caution.thirdparty.divergence": {
        "zh": "第三方线通道分歧:搜索结果里没见写到品牌的第三方网页,引用集却有——接口暴露的来源列表 ≠ 模型实际检索,检索步不作定位依据",
        "en": "Third-party line channel divergence: no mentioning third-party page in the visible search results, yet one was cited — the exposed source list ≠ the model's actual retrieval; the retrieval step is not used for localization",
    },
    # ── REPRESENTATION ───────────────────────────────────────────────
    "repr.headline.not_observable": {
        "zh": "无生成回答观测,表述阶段不可观测",
        "en": "No generated-answer observations — the representation stage is not observable",
    },
    "repr.headline.probe_none": {
        "zh": "探针需求下无可分析的提及句(异常,建议检查采样)",
        "en": "No analyzable mention sentences under a brand-probe question (unexpected — check sampling)",
    },
    "repr.headline.probe_fail": {
        "zh": "点名询问下 AI 对品牌的描述以负面为主",
        "en": "When asked by name, the AI describes the brand predominantly negatively",
    },
    "repr.headline.probe_weak": {
        "zh": "点名询问下 AI 对品牌的描述含明显负面成分",
        "en": "When asked by name, the AI's description contains notable negative sentiment",
    },
    "repr.headline.probe_pass": {
        "zh": "点名询问下 AI 对品牌的描述以中性/正面为主",
        "en": "When asked by name, the AI describes the brand neutrally or positively",
    },
    "repr.headline.neutral_fail": {
        "zh": "品牌未稳定进入生成回答,暂无可分析的品牌描述",
        "en": "The brand does not reliably appear in generated answers; no brand description to analyze yet",
    },
    "repr.headline.neutral_weak": {
        "zh": "品牌在回答中不稳定地被提及",
        "en": "The brand is mentioned inconsistently in answers",
    },
    "repr.headline.neutral_pass": {
        "zh": "品牌稳定进入回答且描述以中性/正面为主",
        "en": "The brand reliably appears in answers with neutral/positive descriptions",
    },
    "repr.mention_rate": {
        "zh": f"提及比例:{_RATE}",
        "en": f"Mention rate: {_RATE_EN}",
    },
    "repr.sentiment_dist": {
        "zh": "提及句情感分布:正面 {positive}、中性 {neutral}、负面 {negative}、混合 {mixed}(负面占比 {share:.0%})",
        "en": "Sentiment of mention sentences: positive {positive}, neutral {neutral}, negative {negative}, mixed {mixed} (negative share {share:.0%})",
    },
    "repr.sentiment_insufficient": {
        "zh": "情感分析:提及样本不足,暂不适用",
        "en": "Sentiment analysis: not applicable — insufficient mention samples",
    },
    "repr.caliber": {
        "zh": "口径(冻结题目集上的观测率,不外推到需求总体):surface={surface} model={model} "
              "题数={n_prompts} N_valid={n_valid}/{n_total} stop_reasons=[{stop_reasons}] "
              "无效:拒答 {n_refused}、空回答 {n_empty}、接口错误 {n_failed} 题型={intent}",
        "en": "Caliber (observed rate on the frozen prompt set; not extrapolated to the need population): "
              "surface={surface} model={model} prompts={n_prompts} N_valid={n_valid}/{n_total} "
              "stop_reasons=[{stop_reasons}] invalid: refused {n_refused}, empty {n_empty}, provider errors {n_failed} "
              "intent={intent}",
    },
    "repr.thresholds.neutral": {
        "zh": "操作阈值(v1 初始值,未经校准):比例 < {fail:.0%} 为 FAIL,≥ {pass_:.0%} 为 PASS,负面占比 >{neg:.0%} 封顶 WEAK",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if the rate is below {fail:.0%}, PASS if at or above {pass_:.0%}; negative share >{neg:.0%} caps at WEAK",
    },
    "repr.thresholds.probe": {
        "zh": "操作阈值(v1 初始值,未经校准):负面占比 >{weak:.0%} 为 WEAK,≥{fail:.0%} 为 FAIL(点名品牌的题提及率不参与判定)",
        "en": "Operating thresholds (v1 initial values, uncalibrated): negative share >{weak:.0%} → WEAK, ≥{fail:.0%} → FAIL (mention rate is not judged for brand-fact prompts)",
    },
    "repr.negative_cap": {
        "zh": "提及充分但负面占比 {share:.0%} 超阈值,状态封顶 WEAK",
        "en": "Mentions are sufficient but negative share {share:.0%} exceeds the threshold; status capped at WEAK",
    },
    "repr.claims_note": {
        "zh": "描述正确性判定需品牌事实清单作参照,v1 未启用(路线图 v1.x)",
        "en": "Claim-correctness checking requires a brand fact sheet as reference; not enabled in v1 (roadmap v1.x)",
    },
    # ── ACCESS(诊断结论)──────────────────────────────────────────────
    "access.headline.pass": {
        "zh": "站点侧证据未见准入阻断(本工具探针视角,非官方爬虫身份;真实爬虫是否放行须站长侧核验)",
        "en": "Site-side evidence shows no access block (this tool's probe vantage, not an official crawler "
              "identity; whether real crawlers are admitted needs site-owner verification)",
    },
    "access.headline.fail": {
        "zh": "站点侧证据存在准入阻断(robots 显式禁止或探针硬失败)",
        "en": "Site-side evidence shows an access block (explicit robots disallow or a hard probe failure)",
    },
    "access.headline.unknown": {
        "zh": "站点侧证据有无法判定项,资格门不可观测",
        "en": "Site-side evidence has undeterminable items; the access gate is not observable",
    },
    "access.headline.missing": {
        "zh": "未执行 ACCESS 审计",
        "en": "The ACCESS audit was not run",
    },
    "access.unknown_note": {
        "zh": "存在无法判定的检查项(常见为机器人防护拦截本工具探针),资格门记为不可观测,"
              "需站长在 CDN 后台或服务器日志侧核验官方爬虫是否放行",
        "en": "Some checks are undeterminable (typically bot protection blocking this tool's probe); the gate is "
              "recorded as not observable — verify in the CDN console or server logs whether official crawlers are admitted",
    },
    # ── ACCESS(证据层,v0.4.13 条目 6)──────────────────────────────────
    "access.layer.POLICY": {
        "zh": "POLICY — 站点公开政策(robots.txt / sitemap,站点单方声明)",
        "en": "POLICY — published site policy (robots.txt / sitemap; unilateral declaration)",
    },
    "access.layer.GENERIC_FETCH": {
        "zh": "GENERIC_FETCH — 本工具探针可达性(普通网络、非官方爬虫身份)",
        "en": "GENERIC_FETCH — this tool's probe reachability (regular network, not an official crawler identity)",
    },
    "access.layer.RENDERED_FETCH": {
        "zh": "RENDERED_FETCH — 真实浏览器渲染视角",
        "en": "RENDERED_FETCH — real-browser rendered view",
    },
    "access.layer.OFFICIAL_IDENTITY": {
        "zh": "OFFICIAL_IDENTITY — 官方爬虫真身访问证据(站长日志)",
        "en": "OFFICIAL_IDENTITY — verified official-crawler visits (site-owner logs)",
    },
    "access.layer.WAF_EVIDENCE": {
        "zh": "WAF_EVIDENCE — CDN/WAF 侧拦截证据(站长后台)",
        "en": "WAF_EVIDENCE — CDN/WAF-side blocking evidence (site-owner console)",
    },
    "access.rendered.not_observable": {
        "zh": "v1 不做真实浏览器渲染(Playwright 预留 v2),JS 渲染后的可索引正文不可观测",
        "en": "v1 does no real-browser rendering (Playwright reserved for v2); JS-rendered indexable text is not observable",
    },
    "access.official.not_observable": {
        "zh": "官方爬虫以来源 IP / 反向 DNS 验真身,本工具无法冒充;须站长提供服务器日志中的真实访问记录,v1 记不可观测",
        "en": "Official crawlers are verified by source IP / reverse DNS and cannot be impersonated by this tool; "
              "real visit records from server logs are required — not observable in v1",
    },
    "access.waf.not_observable": {
        "zh": "CDN/WAF 是否拦截对应爬虫须站长后台或日志核验,v1 记不可观测",
        "en": "Whether the CDN/WAF blocks the corresponding crawler must be verified in the site-owner console or logs — not observable in v1",
    },
    "access.path.summary": {
        "zh": "按路径探针(来自引用核验重抓,同一探针视角):{total} 条品牌 URL,{ok} 条 2xx,"
              "{blocked} 条疑似机器人防护(401/403/429/503),{other} 条其他失败——机器人防护常按路径拦截,首页探针不代表全站",
        "en": "Per-path probes (from citation re-fetches, same probe vantage): {total} brand URL(s), {ok} 2xx, "
              "{blocked} suspected bot protection (401/403/429/503), {other} other failures — bot protection often "
              "blocks per path, so the homepage probe does not represent the whole site",
    },
    # ── ACCESS(审计器检查详情)────────────────────────────────────────
    "access.reach.ok": {
        "zh": "基准请求(浏览器 UA)HTTP {status}",
        "en": "Baseline request (browser UA): HTTP {status}",
    },
    "access.reach.bad": {
        "zh": "基准请求(浏览器 UA)HTTP {status},站点对普通访问者亦不可达",
        "en": "Baseline request (browser UA): HTTP {status} — the site is unreachable even for regular visitors",
    },
    "access.reach.blocked": {
        "zh": "基准请求(浏览器 UA)HTTP {status},疑似 CDN/机器人防护按客户端指纹拦截非浏览器"
              "客户端——真实浏览器与官方爬虫可能正常访问,本审计视角无法判定,需站长在 CDN 后台核验 "
              "AI 爬虫是否被放行",
        "en": "Baseline request (browser UA): HTTP {status} — likely CDN/bot protection rejecting "
              "non-browser clients by fingerprint. Real browsers and official crawlers may pass; this "
              "audit vantage cannot determine it — verify in the CDN console whether AI crawlers are allowed",
    },
    "access.reach.redirect_loop": {
        "zh": "重定向次数超限(疑似循环),任何爬虫都无法到达最终页面",
        "en": "Too many redirects (likely a loop) — no crawler can reach the final page",
    },
    "access.reach.error": {
        "zh": "基准请求失败({error}),无法区分站点故障与审计侧网络问题",
        "en": "Baseline request failed ({error}); cannot distinguish a site outage from auditor-side network issues",
    },
    "access.redirects.none": {
        "zh": "无重定向",
        "en": "No redirects",
    },
    "access.redirects.chain": {
        "zh": "重定向 {count} 跳:{chain} → {final}",
        "en": "{count} redirect hop(s): {chain} → {final}",
    },
    "access.redirects.chain_cross": {
        "zh": "重定向 {count} 跳:{chain} → {final};最终落在跨主机地址 {host}",
        "en": "{count} redirect hop(s): {chain} → {final}; lands on a different host {host}",
    },
    "access.robots.note.parsed": {
        "zh": "HTTP 200,已解析",
        "en": "HTTP 200, parsed",
    },
    "access.robots.note.no_restrictions": {
        "zh": "HTTP {status},视为无限制",
        "en": "HTTP {status}, treated as unrestricted",
    },
    "access.robots.note.server_error": {
        "zh": "HTTP {status},爬虫方可能按保守策略视为全站禁抓",
        "en": "HTTP {status}; crawlers may conservatively treat this as a full disallow",
    },
    "access.robots.note.fetch_failed": {
        "zh": "robots.txt 请求失败({error})",
        "en": "robots.txt request failed ({error})",
    },
    "access.robots.unknown": {
        "zh": "robots.txt 状态无法判定:{note}",
        "en": "robots.txt state undeterminable: {note}",
    },
    "access.robots.fail": {
        "zh": "robots.txt 禁止了检索类爬虫:{denied}{info}",
        "en": "robots.txt disallows retrieval crawlers: {denied}{info}",
    },
    "access.robots.pass": {
        "zh": "robots.txt({note})对全部 {n} 个检索类爬虫令牌放行{info}",
        "en": "robots.txt ({note}) allows all {n} retrieval crawler tokens{info}",
    },
    "access.robots.info_denied": {
        "zh": ";另有非资格门爬虫被禁(不影响结论):{names}",
        "en": "; some non-gating crawlers are disallowed (does not affect the verdict): {names}",
    },
    "access.ua.no_baseline": {
        "zh": "无成功的浏览器基准响应,差异化拦截无从比对",
        "en": "No successful browser baseline response; differential blocking cannot be compared",
    },
    "access.ua.blocked": {
        "zh": "浏览器 UA 正常,但爬虫 UA 未获成功响应:{blocked}。可能是 CDN/WAF 封锁爬虫 UA(真爬虫也会被拒),"
              "也可能是仅放行官方 IP 的反伪装策略(真爬虫能进),本审计无法区分,需在 CDN 后台核验",
        "en": "Browser UA succeeds but crawler UAs do not: {blocked}. This could be CDN/WAF blocking crawler UAs "
              "(real crawlers also rejected) or an official-IP allowlist anti-spoofing policy (real crawlers pass); "
              "this audit cannot distinguish the two — verify in the CDN console",
    },
    "access.ua.pass": {
        "zh": "已探测 {n} 个检索类爬虫 UA,均与浏览器基准一致获得成功响应",
        "en": "Probed {n} retrieval crawler UAs; all succeeded consistently with the browser baseline",
    },
    "access.content.label.bot": {
        "zh": "爬虫 UA 视角:{bot}",
        "en": "crawler-UA view: {bot}",
    },
    "access.content.label.browser": {
        "zh": "浏览器 UA 视角近似",
        "en": "browser-UA approximation",
    },
    "access.content.none": {
        "zh": "未获得任何可分析的 HTML 响应",
        "en": "No analyzable HTML response obtained",
    },
    "access.content.pass": {
        "zh": "HTML 含可见文本约 {chars} 字符({label}){prerender}",
        "en": "HTML contains ~{chars} visible-text characters ({label}){prerender}",
    },
    "access.content.prerender_note": {
        "zh": ";浏览器视角为空壳而爬虫视角有内容,站点疑似对爬虫做了预渲染",
        "en": "; the browser view is a shell while crawler views have content — the site likely prerenders for crawlers",
    },
    "access.content.fail": {
        "zh": "HTML 可见文本仅约 {chars} 字符({label}),低于 {threshold} 阈值,"
              "内容疑似完全依赖 JS 渲染(JS 白板),不执行 JS 的爬虫拿不到正文{reverse}",
        "en": "HTML visible text is only ~{chars} characters ({label}), below the {threshold} threshold — "
              "content likely depends entirely on JS rendering (blank-shell); non-JS crawlers get no body{reverse}",
    },
    "access.content.reverse_note": {
        "zh": ";浏览器视角内容充足而爬虫视角为空壳,疑似对爬虫差异化输出",
        "en": "; the browser view has ample content while crawler views are shells — likely differential output to crawlers",
    },
    "access.sitemap.declared": {
        "zh": "robots.txt 声明了 sitemap:{urls}",
        "en": "robots.txt declares sitemap(s): {urls}",
    },
    "access.sitemap.fallback": {
        "zh": "robots.txt 未声明,但 {url} 可达",
        "en": "Not declared in robots.txt, but {url} is reachable",
    },
    "access.sitemap.missing": {
        "zh": "未发现 sitemap(robots.txt 未声明,/sitemap.xml HTTP {status});不阻断抓取,仅提示",
        "en": "No sitemap found (not declared in robots.txt; /sitemap.xml returned HTTP {status}); does not block crawling — informational only",
    },
    "access.sitemap.error": {
        "zh": "sitemap 检查请求失败({error})",
        "en": "Sitemap check request failed ({error})",
    },
    "access.caveat.vantage": {
        "zh": "审计自普通网络发起,未使用官方爬虫 IP 段;站点若按官方 IP 白名单放行真实爬虫,"
              "UA 差异化拦截检查将呈现 UNKNOWN 而非 FAIL,需站长在 CDN 后台或服务器日志侧核验",
        "en": "The audit runs from a regular network without official crawler IP ranges; if the site allowlists "
              "official IPs, the UA differential check shows UNKNOWN rather than FAIL — verify via CDN console or server logs",
    },
    "access.caveat.robots_voluntary": {
        "zh": "robots.txt 为站点单方声明,各爬虫是否遵守不在本审计担保范围",
        "en": "robots.txt is a unilateral site declaration; whether crawlers honor it is outside this audit's guarantee",
    },
    "access.caveat.ua_list": {
        "zh": "爬虫名单与 UA 字符串来自 config/crawlers.yaml(官方文档快照),需定期核对",
        "en": "The crawler list and UA strings come from config/crawlers.yaml (a snapshot of official docs) and need periodic review",
    },
    "access.caveat.browser_view_fallback": {
        "zh": "所有爬虫 UA 均未获得可分析的 HTML,内容检查以浏览器视角近似",
        "en": "No crawler UA returned analyzable HTML; the content check approximates using the browser view",
    },
    # ── 检索依赖(第 9 章挂靠)─────────────────────────────────────────
    "dependency.headline.high": {
        "zh": "检索依赖:高——禁用检索后提及比例不足开卷的一半,可见度主要靠实时检索支撑",
        "en": "Retrieval dependency: HIGH — with search disabled the mention rate falls below half of the search-on rate; visibility relies mainly on live retrieval",
    },
    "dependency.headline.medium": {
        "zh": "检索依赖:中——禁用检索工具后提及比例明显下降,两种模式下的观测差异明显",
        "en": "Retrieval dependency: MEDIUM — the mention rate drops clearly with the search tool disabled; the observed gap between modes is substantial",
    },
    "dependency.headline.low": {
        "zh": "检索依赖:低——开卷与闭卷提及比例接近,两种模式下未观测到差异",
        "en": "Retrieval dependency: LOW — search-on and search-off mention rates are close; no gap observed between modes",
    },
    "dependency.headline.inconclusive": {
        "zh": "检索依赖:无法判定",
        "en": "Retrieval dependency: INCONCLUSIVE",
    },
    "dependency.pair": {
        "zh": "开卷(正常检索)提及:{on_k}/{on_n} = {on_share:.0%};闭卷(禁用检索)提及:{off_k}/{off_n} = {off_share:.0%}",
        "en": "Search-on mention: {on_k}/{on_n} = {on_share:.0%}; search-off mention: {off_k}/{off_n} = {off_share:.0%}",
    },
    "dependency.note.meaning": {
        "zh": "口径说明:闭卷 = 同一批题目在不提供检索工具下重复采样;闭卷能答不等于「AI 记住了品牌」。"
              "禁用检索工具同时可能改变平台的路由与回答风格,差异是两种模式下的观测差异,"
              "不解读为实时检索的因果增量(第 9 章)。本观测不参与瓶颈定位",
        "en": "Caliber note: search-off = the same prompts re-sampled without the search tool; answering "
              "search-off does not mean the AI \"remembers\" the brand. Disabling the tool may also change "
              "the platform's routing and answer style, so the gap is an observed difference between modes, "
              "not a causal increment of live retrieval (§9). This observation does not participate in "
              "bottleneck localization",
    },
    "dependency.note.absent_online": {
        "zh": "开卷提及本身处于缺席水平(比例低于缺席线),依赖度无从谈起",
        "en": "Search-on mention is itself at absence level (rate below the absence line); dependency is moot",
    },
    "dependency.note.missing_leg": {
        "zh": "开卷或闭卷缺少有效样本,无法对比",
        "en": "The search-on or search-off leg has no valid samples; comparison is not possible",
    },
    "dependency.thresholds": {
        "zh": "判定规则(看比例):开卷与闭卷相差不足 {gap:.0%} → LOW;闭卷 < 开卷×{ratio:.0%} → HIGH;其余 → MEDIUM;"
              "开卷 < {absent:.0%} 或任一腿缺样本 → 无法判定",
        "en": "Verdict rule (rates): search-on and search-off within {gap:.0%} → LOW; search-off < search-on ×{ratio:.0%} → HIGH; "
              "otherwise MEDIUM; search-on below {absent:.0%} or a missing leg → INCONCLUSIVE",
    },
    "dependency.leg_caliber": {
        "zh": "{leg},{caliber}",
        "en": "{leg}: {caliber}",
    },
    "dependency.leg.online": {
        "zh": "开卷腿",
        "en": "search-on leg",
    },
    "dependency.leg.offline": {
        "zh": "闭卷腿",
        "en": "search-off leg",
    },
    # ── 警示语(diagnose)──────────────────────────────────────────────
    "caution.divergence": {
        "zh": "接口返回的检索来源列表里品牌缺席,但引用阶段 PASS——被引必先被检索,两者矛盾。"
              "这说明接口暴露的来源列表与模型实际执行的检索(含查询改写与多语言扩展)不等同,"
              "检索结论按观测分歧处理,不据此定位瓶颈(架构书风险 #5:观测通道不当真值)",
        "en": "The brand is absent from the retrieval sources the interface returned, yet the citation stage PASSes — "
              "being cited requires being retrieved, so the two contradict. The exposed source list "
              "is not equivalent to the model's actual retrieval (query rewriting, multilingual fan-out); "
              "the retrieval result is treated as observation divergence and not used for bottleneck localization "
              "(architecture risk #5: observation channels are not ground truth)",
    },
    "caution.unobservable": {
        "zh": "以下阶段不可观测,未参与瓶颈定位:{stages}",
        "en": "The following stages are not observable and were excluded from bottleneck localization: {stages}",
    },
    "caution.no_rewrite": {
        "zh": "最强瓶颈信号在准入/检索层:当前证据不足以表明问题出在引用或生成阶段,"
              "因此【不建议】进行全页内容改写(依据见架构书 5.3)",
        "en": "The strongest bottleneck signal is at the access/retrieval layer: current evidence does not indicate a "
              "citation- or generation-stage problem, so full-page content rewriting is NOT recommended (see architecture §5.3)",
    },
    "caution.none": {
        "zh": "未见瓶颈信号:各可观测阶段均无 FAIL/WEAK,无需药方",
        "en": "No bottleneck signal: no FAIL/WEAK among observable stages — no remedy needed",
    },
    # ── 阶段名(v0.4.14:技术名在审计视图,人话在用户视图)────────────────
    "stage.ACCESS": {"zh": "ACCESS", "en": "ACCESS"},
    "stage.RETRIEVAL": {"zh": "RETRIEVAL", "en": "RETRIEVAL"},
    "stage.CITATION": {"zh": "CITATION", "en": "CITATION"},
    "stage.REPRESENTATION": {"zh": "REPRESENTATION", "en": "REPRESENTATION"},
    # ── 每家 AI 一句话(v0.4.14,规则表经用户拍板;{ai} 由报告层代入显示名)────
    "verdict.known_recommends_cites": {
        "zh": "{ai} 知道你,会主动推荐你,也会引用你的官网",
        "en": "{ai} knows you, recommends you unprompted, and cites your site",
    },
    "verdict.known_recommends_site_rare": {
        "zh": "{ai} 知道你,会主动推荐你,但很少把你的官网当作信息来源",
        "en": "{ai} knows you and recommends you unprompted, but rarely uses your site as a source",
    },
    "verdict.known_sometimes_site_rare": {
        "zh": "{ai} 知道你,但用户没提到你时不一定会推荐你,官网也很少被用作来源",
        "en": "{ai} knows you, but does not reliably recommend you when users do not name you, and rarely uses your site as a source",
    },
    "verdict.known_sometimes_site_used": {
        "zh": "{ai} 知道你,官网也会被引用,但用户没提到你时不一定会推荐你",
        "en": "{ai} knows you and cites your site, but does not reliably recommend you when users do not name you",
    },
    "verdict.only_when_named": {
        "zh": "用户主动问起时 {ai} 能介绍你,但用户不提你时 {ai} 几乎不会想到你",
        "en": "{ai} can describe you when users ask about you directly, but almost never brings you up otherwise",
    },
    "verdict.only_when_named_site_used": {
        "zh": "用户主动问起时 {ai} 能介绍你并引用官网,但用户不提你时 {ai} 几乎不会想到你",
        "en": "{ai} can describe you and cite your site when users ask directly, but almost never brings you up otherwise",
    },
    "verdict.barely_known": {
        "zh": "连用户直接问起时,{ai} 都不能稳定地介绍你,说明它对你的了解本身就不够",
        "en": "Even when users ask about you directly, {ai} cannot describe you consistently — it barely knows you",
    },
    "verdict.known_site_used": {
        "zh": "{ai} 知道你,也会引用你的官网",
        "en": "{ai} knows you and cites your site",
    },
    "verdict.known_site_rare": {
        "zh": "{ai} 知道你,但很少把你的官网当作信息来源",
        "en": "{ai} knows you, but rarely uses your site as a source",
    },
    "verdict.unnamed_often_site_used": {
        "zh": "用户没提到你时 {ai} 经常主动推荐你,也会引用你的官网",
        "en": "When users do not name you, {ai} often recommends you and cites your site",
    },
    "verdict.unnamed_often_site_rare": {
        "zh": "用户没提到你时 {ai} 经常主动推荐你,但很少引用你的官网",
        "en": "When users do not name you, {ai} often recommends you but rarely cites your site",
    },
    "verdict.unnamed_sometimes_site_used": {
        "zh": "用户没提到你时 {ai} 有时会推荐你,官网也会被引用",
        "en": "When users do not name you, {ai} sometimes recommends you and cites your site",
    },
    "verdict.unnamed_sometimes_site_rare": {
        "zh": "用户没提到你时 {ai} 有时会推荐你,但官网很少被引用",
        "en": "When users do not name you, {ai} sometimes recommends you but rarely cites your site",
    },
    "verdict.unnamed_rare_site_used": {
        "zh": "用户没提到你时 {ai} 几乎不会想到你,虽然官网偶尔会被引用",
        "en": "When users do not name you, {ai} almost never brings you up, though it occasionally cites your site",
    },
    "verdict.unnamed_rare_site_rare": {
        "zh": "用户没提到你时 {ai} 几乎不会想到你,官网也很少被用作来源",
        "en": "When users do not name you, {ai} almost never brings you up, and rarely uses your site as a source",
    },
    "verdict.no_data": {
        "zh": "{ai} 本次没有可用的观测",
        "en": "No usable observations for {ai} this time",
    },
    "verdict.missing.probe": {
        "zh": "用户直接问起你时的表现本次没测",
        "en": "how it behaves when users ask about you directly was not tested this time",
    },
    "verdict.missing.organic": {
        "zh": "用户没提到你时的表现本次没测",
        "en": "how it behaves when users do not name you was not tested this time",
    },
    "verdict.missing.site": {
        "zh": "官网有没有被用上这一项本次看不到",
        "en": "whether your site was used could not be observed this time",
    },
    # 第三方线一句(S6.1;措辞用户 2026-10-13 拍板:口语版,v0.4.24 D10)
    "verdict.thirdparty.often": {
        "zh": "它常常拿别人提到你的文章当参考",
        "en": "It often uses other people's pages about you as references",
    },
    "verdict.thirdparty.sometimes": {
        "zh": "它偶尔拿别人提到你的文章当参考",
        "en": "It occasionally uses other people's pages about you as references",
    },
    "verdict.thirdparty.rarely": {
        "zh": "它基本不拿别人提到你的文章当参考",
        "en": "It basically never uses other people's pages about you as references",
    },
    "verdict.missing.thirdparty": {
        "zh": "别人提到你的文章有没有被它参考,这次看不出来",
        "en": "whether it used other people's pages about you could not be told this time",
    },
}


# 人话目录(v0.4.14 用户视图):同一结论码的品牌方版本。缺席即回落到技术版——
# 回落是否出现在用户视图,由 tests/unit/test_report_page.py 的禁技术词测试把守。
# 比例一律用「本次 N 次里 k 次」的原始计数表达,不出单点概率(铁律 8 用户层口径)。
PLAIN: dict[str, dict[str, str]] = {
    "stage.ACCESS": {"zh": "官网访问(爬虫能不能进你的网站)", "en": "Site access (can crawlers reach your site)"},
    "stage.RETRIEVAL": {"zh": "官网检索(AI 搜索时有没有找到你的官网)", "en": "Site retrieval (does the AI find your site when it searches)"},
    "stage.CITATION": {"zh": "官网引用(AI 拿你的官网当参考)", "en": "Site citation (does the AI use your site as a reference)"},
    "stage.REPRESENTATION": {"zh": "回答提及(AI 回答里有没有提到你、怎么评价你)", "en": "Answer mention (does the AI mention you, and how)"},
    "retrieval.headline.not_observable": {"zh": "这家 AI 不公开搜索结果,这一项本次看不到", "en": "This AI does not expose its search results; this item could not be observed"},
    "genret.headline.fail": {"zh": "AI 按自己的习惯搜索时,基本没有找到你的官网", "en": "Searching its own way, the AI basically never finds your site"},
    "genret.headline.weak": {"zh": "AI 按自己的习惯搜索时,有时能找到你的官网,不稳定", "en": "Searching its own way, the AI sometimes finds your site, not reliably"},
    "genret.headline.pass": {"zh": "AI 按自己的习惯搜索时,能稳定找到你的官网", "en": "Searching its own way, the AI reliably finds your site"},
    "genret.headline.not_observable": {"zh": "这家 AI 不公开搜索结果,这一项本次看不到", "en": "This AI does not expose its search results; this item could not be observed"},
    "citation.headline.fail": {"zh": "它基本不拿你的官网当参考", "en": "It basically never uses your site as a reference"},
    "citation.headline.weak": {"zh": "它偶尔拿你的官网当参考", "en": "It occasionally uses your site as a reference"},
    "citation.headline.pass": {"zh": "它常常拿你的官网当参考", "en": "It often uses your site as a reference"},
    "citation.headline.not_observable": {"zh": "它不公开参考来源,这一项本次看不到", "en": "It does not show its references; this item could not be observed"},
    "repr.headline.not_observable": {"zh": "没有拿到可分析的回答,这一项本次看不到", "en": "No analyzable answers; this item could not be observed"},
    # 第三方线的人话一律说「别人提到你的文章」(用户定 2026-10-13,措辞已拍板)
    "thirdparty.retrieved.headline.fail": {"zh": "它搜索时基本找不到别人提到你的文章", "en": "When it searches, it basically never finds other people's pages about you"},
    "thirdparty.retrieved.headline.weak": {"zh": "它搜索时偶尔能找到别人提到你的文章", "en": "When it searches, it occasionally finds other people's pages about you"},
    "thirdparty.retrieved.headline.pass": {"zh": "它搜索时常常能找到别人提到你的文章", "en": "When it searches, it often finds other people's pages about you"},
    "thirdparty.retrieved.headline.not_observable": {"zh": "它不公开搜索结果,或者那些文章这次打不开,看不出来", "en": "It does not show its search results, or those pages could not be opened this time; cannot tell"},
    "thirdparty.cited.headline.fail": {"zh": "它基本不拿别人提到你的文章当参考", "en": "It basically never uses other people's pages about you as references"},
    "thirdparty.cited.headline.weak": {"zh": "它偶尔拿别人提到你的文章当参考", "en": "It occasionally uses other people's pages about you as references"},
    "thirdparty.cited.headline.pass": {"zh": "它常常拿别人提到你的文章当参考", "en": "It often uses other people's pages about you as references"},
    "thirdparty.cited.headline.not_observable": {"zh": "它不公开参考来源,或者那些文章这次打不开,看不出来", "en": "It does not show its references, or those pages could not be opened this time; cannot tell"},
    "thirdparty.retrieved.rate": {"zh": "本次 {n} 次回答里,{k} 次搜到了别人提到你的文章", "en": "In {k} of {n} answers this time, its search turned up other people's pages about you"},
    "thirdparty.cited.rate": {"zh": "本次 {n} 次回答里,{k} 次拿别人提到你的文章当参考(和官网分开算)", "en": "In {k} of {n} answers this time, it used other people's pages about you as references (counted apart from your own site)"},
    "thirdparty.unobservable": {"zh": "有 {n} 篇文章这次看不到,没有参与判断:{items}", "en": "{n} page(s) could not be checked this time and were not used: {items}"},
    "caution.thirdparty.divergence": {"zh": "搜索结果里没看到别人提到你的文章,回答却参考了:AI 实际的搜索和我们看到的不完全一样,这一项不作为问题依据", "en": "No page about you by others showed up in the visible search results, yet one was used as a reference: the AI's actual search differs from what we can see, so this item is not used as evidence of a problem"},
    "repr.headline.probe_none": {"zh": "用户直接问起你时,回答里没有出现你的品牌名(不正常,建议检查测试)", "en": "Your brand name did not appear even when users asked directly (abnormal; check the test)"},
    "repr.headline.probe_fail": {"zh": "用户直接问起你时,AI 对你的评价以负面为主", "en": "When users ask about you directly, the AI's description is mostly negative"},
    "repr.headline.probe_weak": {"zh": "用户直接问起你时,AI 对你的评价里有明显负面内容", "en": "When users ask about you directly, the AI's description contains clearly negative content"},
    "repr.headline.probe_pass": {"zh": "用户直接问起你时,AI 会正常介绍你,评价以中性或正面为主", "en": "When users ask about you directly, the AI describes you normally, mostly neutral or positive"},
    "repr.headline.neutral_fail": {"zh": "用户没提到你时,AI 基本不会主动提到你", "en": "When users do not name you, the AI basically never brings you up"},
    "repr.headline.neutral_weak": {"zh": "用户没提到你时,AI 有时会提到你,不稳定", "en": "When users do not name you, the AI sometimes mentions you, not reliably"},
    "repr.headline.neutral_pass": {"zh": "用户没提到你时,AI 经常主动提到你,评价以中性或正面为主", "en": "When users do not name you, the AI often mentions you, mostly neutral or positive"},
    "access.headline.pass": {"zh": "没有发现阻碍爬虫进入官网的设置(这是我们自己的检测工具看到的;真实 AI 爬虫是否顺利,还需要网站技术团队确认)", "en": "No setting was found that blocks crawlers from your site (as seen by our own probe; whether real AI crawlers get through still needs your web team to confirm)"},
    "access.headline.fail": {"zh": "发现了阻碍爬虫进入官网的设置(robots.txt 明确禁止,或我们的检测完全无法访问)", "en": "A setting that blocks crawlers from your site was found (an explicit robots.txt disallow, or our probe could not access the site at all)"},
    "access.headline.unknown": {"zh": "目前无法确认真实 AI 爬虫能否正常访问官网:我们的自动检测有请求被网站安全系统拦截,建议由网站技术团队查看 CDN 或服务器日志", "en": "We cannot confirm whether real AI crawlers can access your site: some of our automated requests were blocked by the site's security layer — ask your web team to check the CDN or server logs"},
    "access.headline.missing": {"zh": "本次没有检测官网抓取情况", "en": "Site crawlability was not checked this time"},
    "dependency.headline.high": {"zh": "不用联网搜索时,AI 几乎不提你:目前的曝光主要靠联网搜索撑着", "en": "Without web search the AI barely mentions you: your current exposure relies mainly on live search"},
    "dependency.headline.medium": {"zh": "不用联网搜索时,AI 提到你的次数明显变少", "en": "Without web search the AI mentions you noticeably less"},
    "dependency.headline.low": {"zh": "用不用联网搜索,AI 提到你的次数差不多", "en": "With or without web search, the AI mentions you about as often"},
    "dependency.headline.inconclusive": {"zh": "联网与不联网的差别本次看不出来", "en": "The difference with and without web search could not be determined this time"},
    "caution.divergence": {"zh": "搜索结果里没看到你的官网,但回答却引用了它:说明 AI 实际执行的搜索和我们能看到的不完全一样,这一项不作为问题依据", "en": "Your site was absent from the visible search results yet cited in the answer: the AI's actual search differs from what we can see, so this item is not used as evidence of a problem"},
    "caution.unobservable": {"zh": "以下项目本次看不到,没有参与判断:{stages}", "en": "The following items could not be observed and were not used: {stages}"},
    "caution.no_rewrite": {"zh": "问题主要出在「AI 能不能找到你」这一步,现在不建议大规模改写官网正文", "en": "The problem sits at \"can the AI find you\"; a large-scale rewrite of your site's copy is not advised now"},
    "caution.none": {"zh": "没有发现明显问题", "en": "No obvious problem found"},
    "genret.presence": {"zh": "本次 {n} 次搜索里,{k} 次结果中出现了你的官网", "en": "Your site appeared in {k} of {n} searches this time"},
    "genret.query_entry": {"zh": "AI 自己拟的搜索词里,{k}/{n} 次带上了你的品牌名", "en": "The AI's own search queries included your brand name in {k} of {n} runs"},
    "citation.rate": {"zh": "本次 {n} 次回答里,{k} 次拿你的官网当参考", "en": "In {k} of {n} answers this time, it used your site as a reference"},
    "citation.primary_rate": {"zh": "其中 {k} 次参考的是你的主站", "en": "{k} of those used your main site"},
    "citation.by_domain": {"zh": "分开看各个官网网址:{items}", "en": "By site: {items}"},
    "citation.domain_item": {"zh": "{domain} {k} 次", "en": "{domain} {k} times"},
    "citation.channel_rate": {"zh": "另外本次 {n} 次回答里,{k} 次参考了你的官方店铺或账号(单独算,不计入上面)", "en": "Separately, {k} of {n} answers cited your official store or account (counted apart, not included above)"},
    "citation.unresolved": {"zh": "有 {n} 条引用链接查不到最终去向,本次看不到,没有参与判断", "en": "{n} citation link(s) could not be traced to their destination; not observed this time and not used"},
    "repr.mention_rate": {"zh": "本次 {n} 次回答里,{k} 次提到了你", "en": "You were mentioned in {k} of {n} answers this time"},
    "common.prompt_coverage": {"zh": "{n} 道题里,{k} 道至少有一次提到你", "en": "You came up at least once in {k} of {n} questions"},
    "dependency.pair": {"zh": "用联网搜索时 {on_n} 次回答里 {on_k} 次提到你;不用联网搜索时 {off_n} 次里 {off_k} 次提到你", "en": "With web search you were mentioned in {on_k} of {on_n} answers; without it, in {off_k} of {off_n}"},
}


def render_msg(message: Msg | str, lang: str, register: str | None = None) -> str:
    """按语言组句;参数中的 Msg / Msg 列表递归渲染后代入。
    register="plain" 取用户视图人话版本(PLAIN),该码无人话版本即回落技术版。"""
    if isinstance(message, str):
        return message
    versions = PLAIN.get(message.code) if register == "plain" else None
    template = (versions or CATALOG[message.code])[lang]
    rendered: dict[str, Any] = {}
    for key, value in message.params.items():
        if isinstance(value, Msg):
            rendered[key] = render_msg(value, lang, register)
        elif isinstance(value, list) and value and all(isinstance(v, Msg) for v in value):
            rendered[key] = "、".join(render_msg(v, lang, register) for v in value)
        else:
            rendered[key] = value
    return template.format(**rendered)
