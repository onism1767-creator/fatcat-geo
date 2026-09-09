"""L3 结论码目录与渲染器(16.5 v1.1)。

L3 逻辑只产出 Msg(code+params),本模块负责按语言组句。
zh 模板与 v1.0 的成品语句逐字一致(保持报告连续性);en 为对应译文。
参数值若为 Msg(或 Msg 列表)则递归渲染后再代入——用于嵌套从句。
未登记的 code 渲染时直接 KeyError:宁可炸,不可静默吞结论。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Msg(BaseModel):
    """一条结构化结论:code 定语义,params 带事实(可嵌套 Msg)。
    定义在本模块(零 engine 依赖),供含 access_audit 在内的全部 L3 使用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    params: dict[str, Any] = {}


def msg(code: str, **params: Any) -> Msg:
    return Msg(code=code, params=params)


_EST = "{point:.0%} [95% 可信区间(Jeffreys 先验): {low:.0%}–{high:.0%}](N={n})"
_EST_EN = "{point:.0%} [95% credible interval (Jeffreys prior): {low:.0%}–{high:.0%}] (N={n})"

CATALOG: dict[str, dict[str, str]] = {
    # ── 公共 ──────────────────────────────────────────────────────────
    "common.low_confidence_downgrade": {
        "zh": "无效样本占比超阈值(LOW_CONFIDENCE),确定性结论降权为 WEAK(7.3)",
        "en": "Invalid-sample share exceeds threshold (LOW_CONFIDENCE); definitive verdicts are downgraded to WEAK (§7.3)",
    },
    "common.caliber": {
        "zh": "口径(冻结问法集上的观测率,不外推到需求总体):surface={surface} model={model} "
              "问法数={n_variants} N_valid={n_valid}/{n_total} stop_reasons=[{stop_reasons}] "
              "拒答率={refusal:.0%} 失败率={failure:.0%}",
        "en": "Caliber (observed rate on the frozen prompt set; not extrapolated to the need population): "
              "surface={surface} model={model} prompts={n_variants} N_valid={n_valid}/{n_total} "
              "stop_reasons=[{stop_reasons}] refusal={refusal:.0%} failure={failure:.0%}",
    },
    "common.per_variant": {
        "zh": "各问法并列(问法是语义层的独立单位,运行只是问法内重复):{items}",
        "en": "Per prompt (prompts are the independent semantic units; runs only repeat within a prompt): {items}",
    },
    "common.variant_item": {
        "zh": "问法{idx} {point:.0%} [{low:.0%}–{high:.0%}](N={n})",
        "en": "prompt {idx} {point:.0%} [{low:.0%}–{high:.0%}] (N={n})",
    },
    "common.variant_item_none": {
        "zh": "问法{idx} 无有效样本",
        "en": "prompt {idx} no valid samples",
    },
    "common.variant_sensitive": {
        "zh": "问法间可信区间不重叠:措辞敏感,需求层合并估计不稳,建议增加问法数后复测(v0.4.13 条目 2)",
        "en": "Credible intervals of different prompts do not overlap: wording-sensitive — the pooled "
              "need-level estimate is unstable; add prompts and re-measure (v0.4.13 item 2)",
    },
    # ── RETRIEVAL ────────────────────────────────────────────────────
    "retrieval.headline.fail": {
        "zh": "品牌网站基本不进检索候选——缺席从检索层开始",
        "en": "The brand site is largely absent from retrieval candidates — the absence starts at retrieval",
    },
    "retrieval.headline.weak": {
        "zh": "品牌网站在检索候选中不稳定在场",
        "en": "The brand site appears inconsistently among retrieval candidates",
    },
    "retrieval.headline.pass": {
        "zh": "品牌网站稳定进入检索候选",
        "en": "The brand site reliably enters retrieval candidates",
    },
    "retrieval.headline.not_observable": {
        "zh": "无检索观测通道或无有效样本,检索阶段不可观测",
        "en": "No retrieval observation channel or no valid samples — the retrieval stage is not observable",
    },
    "retrieval.not_observable_note": {
        "zh": "禁止用引用集反推检索排名(铁律 7),故此阶段如实记不可观测",
        "en": "Inferring retrieval rank from citations is forbidden (iron rule 7), so this stage is honestly recorded as not observable",
    },
    "retrieval.presence": {
        "zh": f"检索候选在场率:{_EST}",
        "en": f"Retrieval-candidate presence rate: {_EST_EN}",
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
        "zh": "操作阈值(v1 初始值,未经校准):FAIL 上界≤{fail:.0%},PASS 下界≥{pass_:.0%}",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if CI upper ≤{fail:.0%}, PASS if CI lower ≥{pass_:.0%}",
    },
    # ── RETRIEVAL 生成端通道(v0.4.8 双通道架构)───────────────────────
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
        "zh": f"候选在场率(模型自拟检索词换来的候选集中出现品牌域名):{_EST}",
        "en": f"Candidate presence rate (brand domain among results fetched for model-authored queries): {_EST_EN}",
    },
    "genret.query_entry": {
        "zh": f"检索词品牌入场率(模型自拟检索词中出现品牌名/别名):{_EST}",
        "en": f"Query entry rate (brand name/alias appears in model-authored queries): {_EST_EN}",
    },
    "genret.funnel.pair": {
        "zh": "同仪器漏斗:候选在场 {presence_point:.0%} [{presence_low:.0%}–{presence_high:.0%}](N={presence_n})"
              " vs 被引 {cited_point:.0%} [{cited_low:.0%}–{cited_high:.0%}](N={cited_n})",
        "en": "Same-instrument funnel: candidate presence {presence_point:.0%} [{presence_low:.0%}–{presence_high:.0%}] (N={presence_n})"
              " vs cited {cited_point:.0%} [{cited_low:.0%}–{cited_high:.0%}] (N={cited_n})",
    },
    "genret.funnel.gap": {
        "zh": "被引区间显著低于在场区间——资料递到了模型手上却常落选,缺席主因偏向内容竞争力而非检索可见度(同一批运行内部对比,零跨仪器混淆)",
        "en": "The cited interval is significantly below the presence interval — material reaches the model but often loses out; the absence leans toward content competitiveness rather than retrieval visibility (same-run comparison, no cross-instrument confound)",
    },
    "genret.funnel.aligned": {
        "zh": "在场率与被引率区间重叠——递上来的资料被采用的程度与在场程度无法区分",
        "en": "Presence and cited intervals overlap — adoption of delivered material is indistinguishable from its presence",
    },
    "genret.triggered": {
        "zh": f"检索触发率:{_EST};未触发检索的运行计入各分母——不检索本身就是自然行为的一部分",
        "en": f"Search trigger rate: {_EST_EN}; runs without search count in denominators — not searching is itself part of natural behavior",
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
    "citation.headline.weak_zero": {
        "zh": "样本内未见被引;样本量不足以下 FAIL 结论",
        "en": "No citations observed in the sample; sample size is too small for a FAIL verdict",
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
        "zh": f"被引概率:{_EST}",
        "en": f"Citation probability: {_EST_EN}",
    },
    "citation.thresholds": {
        "zh": "操作阈值(v1 初始值,未经校准):FAIL 上界≤{fail:.0%},PASS 下界≥{pass_:.0%}",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if CI upper ≤{fail:.0%}, PASS if CI lower ≥{pass_:.0%}",
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
        "zh": f"显著落后于竞品 {{name}}(其被引 {_EST})",
        "en": f"Significantly behind competitor {{name}} (their citation rate: {_EST_EN})",
    },
    "citation.compare.ahead": {
        "zh": f"显著领先竞品 {{name}}(其被引 {_EST})",
        "en": f"Significantly ahead of competitor {{name}} (their citation rate: {_EST_EN})",
    },
    "citation.compare.overlap": {
        "zh": f"与竞品 {{name}} 区间重叠,无法区分(其被引 {_EST})",
        "en": f"Interval overlaps with competitor {{name}} — not distinguishable (their citation rate: {_EST_EN})",
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
        "zh": f"提及率:{_EST}",
        "en": f"Mention rate: {_EST_EN}",
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
        "zh": "口径(冻结问法集上的观测率,不外推到需求总体):surface={surface} model={model} "
              "问法数={n_variants} N_valid={n_valid}/{n_total} stop_reasons=[{stop_reasons}] 意图={intent}",
        "en": "Caliber (observed rate on the frozen prompt set; not extrapolated to the need population): "
              "surface={surface} model={model} prompts={n_variants} N_valid={n_valid}/{n_total} "
              "stop_reasons=[{stop_reasons}] intent={intent}",
    },
    "repr.thresholds.neutral": {
        "zh": "操作阈值(v1 初始值,未经校准):FAIL 上界≤{fail:.0%},PASS 下界≥{pass_:.0%},负面占比 >{neg:.0%} 封顶 WEAK",
        "en": "Operating thresholds (v1 initial values, uncalibrated): FAIL if CI upper ≤{fail:.0%}, PASS if CI lower ≥{pass_:.0%}; negative share >{neg:.0%} caps at WEAK",
    },
    "repr.thresholds.probe": {
        "zh": "操作阈值(v1 初始值,未经校准):负面占比 >{weak:.0%} 为 WEAK,≥{fail:.0%} 为 FAIL(探针需求提及率不参与判定)",
        "en": "Operating thresholds (v1 initial values, uncalibrated): negative share >{weak:.0%} → WEAK, ≥{fail:.0%} → FAIL (mention rate is not judged for probe questions)",
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
        "zh": "检索依赖:高——禁用检索后提及率不足开卷下界的一半,可见度主要靠实时检索支撑",
        "en": "Retrieval dependency: HIGH — with search disabled the mention rate falls below half of the search-on lower bound; visibility relies mainly on live retrieval",
    },
    "dependency.headline.medium": {
        "zh": "检索依赖:中——禁用检索工具后提及率显著下降,两种模式下的观测差异明显",
        "en": "Retrieval dependency: MEDIUM — the mention rate drops significantly with the search tool disabled; the observed gap between modes is substantial",
    },
    "dependency.headline.low": {
        "zh": "检索依赖:低——开卷与闭卷提及率无法区分,两种模式下未观测到差异",
        "en": "Retrieval dependency: LOW — search-on and search-off mention rates are indistinguishable; no gap observed between modes",
    },
    "dependency.headline.inconclusive": {
        "zh": "检索依赖:无法判定",
        "en": "Retrieval dependency: INCONCLUSIVE",
    },
    "dependency.pair": {
        "zh": "开卷(正常检索)提及:{on_point:.0%} [95% 可信区间(Jeffreys 先验): {on_low:.0%}–{on_high:.0%}](N={on_n});"
              "闭卷(禁用检索)提及:{off_point:.0%} [95% 可信区间(Jeffreys 先验): {off_low:.0%}–{off_high:.0%}](N={off_n})",
        "en": "Search-on mention: {on_point:.0%} [95% credible interval (Jeffreys prior): {on_low:.0%}–{on_high:.0%}] (N={on_n}); "
              "search-off mention: {off_point:.0%} [95% credible interval (Jeffreys prior): {off_low:.0%}–{off_high:.0%}] (N={off_n})",
    },
    "dependency.note.meaning": {
        "zh": "口径说明:闭卷 = 同一问法在不提供检索工具下重复采样;闭卷能答不等于「AI 记住了品牌」。"
              "禁用检索工具同时可能改变平台的路由与回答风格,差异是两种模式下的观测差异,"
              "不解读为实时检索的因果增量(第 9 章)。本观测不参与瓶颈定位",
        "en": "Caliber note: search-off = the same prompts re-sampled without the search tool; answering "
              "search-off does not mean the AI \"remembers\" the brand. Disabling the tool may also change "
              "the platform's routing and answer style, so the gap is an observed difference between modes, "
              "not a causal increment of live retrieval (§9). This observation does not participate in "
              "bottleneck localization",
    },
    "dependency.note.absent_online": {
        "zh": "开卷提及本身处于缺席水平(区间上界不高于缺席阈值),依赖度无从谈起",
        "en": "Search-on mention is itself at absence level (CI upper bound at or below the absence threshold); dependency is moot",
    },
    "dependency.note.missing_leg": {
        "zh": "开卷或闭卷缺少有效样本,无法对比",
        "en": "The search-on or search-off leg has no valid samples; comparison is not possible",
    },
    "dependency.thresholds": {
        "zh": "判定规则(只认区间端点):区间重叠 → LOW;分离 → MEDIUM;闭卷上界 < 开卷下界×{ratio:.0%} → HIGH;"
              "开卷上界 ≤ {absent:.0%} 或任一腿缺样本 → 无法判定",
        "en": "Verdict rule (interval endpoints only): overlap → LOW; separated → MEDIUM; search-off upper < "
              "search-on lower ×{ratio:.0%} → HIGH; search-on upper ≤ {absent:.0%} or a missing leg → INCONCLUSIVE",
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
        "zh": "检索观测通道显示品牌缺席,但引用阶段 PASS——被引必先被检索,两通道结论矛盾。"
              "这说明裸检索观测通道与生成端实际执行的检索(含查询改写与多语言扩展)不等同,"
              "检索结论按观测通道分歧处理,不据此定位瓶颈(架构书风险 #5:观测通道不当真值)",
        "en": "The retrieval observation channel shows the brand absent, yet the citation stage PASSes — "
              "being cited requires being retrieved, so the two channels contradict. The bare-search channel "
              "is not equivalent to the generator's actual retrieval (query rewriting, multilingual fan-out); "
              "the retrieval result is treated as channel divergence and not used for bottleneck localization "
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
}


def render_msg(message: Msg | str, lang: str) -> str:
    """按语言组句;参数中的 Msg / Msg 列表递归渲染后代入。"""
    if isinstance(message, str):
        return message
    template = CATALOG[message.code][lang]
    rendered: dict[str, Any] = {}
    for key, value in message.params.items():
        if isinstance(value, Msg):
            rendered[key] = render_msg(value, lang)
        elif isinstance(value, list) and value and all(isinstance(v, Msg) for v in value):
            rendered[key] = "、".join(render_msg(v, lang) for v in value)
        else:
            rendered[key] = value
    return template.format(**rendered)
