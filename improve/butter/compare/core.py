"""再测比对(纯层)。设计 §9.4:按题配对的前后差为主,对照组看住漂移,差中差并列(ADR-B05)。

两步:
  plan_rules     测基线时调:按这批题的题数和默认值,先算两条波动范围,选更窄的当主判读,整份冻结。
  compare_batch  再测后调:按冻结的规则逐腿逐指标判一次。总是返回结果对象,数据有问题写进结果,不抛异常。

总表第 2 行"站长没部署这条"是按卡片判的:整批都没部署时这里给 not_deployed,单张卡没部署由卡片层处理。
"""

from __future__ import annotations

import zlib
from datetime import date

from butter.compare import stats
from butter.config.core import Defaults, pick_tier
from butter.contracts import (
    BatchFacts, CompareResult, CompareRules, LabelCode, LegData, LegResult, LegRule, Metric, Outcome,
)

METHOD_VERSION = "compare/1"

LABEL_TEXT: dict[LabelCode, str] = {
    "not_remeasurable": "不可再测",
    "not_deployed": "未部署",
    "config_changed": "不可比:配置变了",
    "incomplete": "不可比:测量不完整",
    "baseline_invalid": "基线无效:只看时序",
    "not_comparable": "不可比:样本不够 / 该腿不比",
    "drift": "同期漂移",
    "up": "超出波动上升",
    "down": "超出波动下降",
    "within": "在波动内",
}
NO_CONTROL_SUFFIX = "(无有效对照)"
OUTCOME: dict[LabelCode, Outcome | None] = {
    "not_remeasurable": None, "not_deployed": None, "config_changed": "dnf", "incomplete": "dnf",
    "baseline_invalid": "dnf", "not_comparable": "dnf", "drift": "inconclusive", "up": "won", "down": "lost",
    "within": "inconclusive",
}


def label_text(leg: LegResult) -> str:
    return LABEL_TEXT[leg.label] + (NO_CONTROL_SUFFIX if leg.no_control else "")


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def plan_rules(defaults: Defaults, legs: list[tuple[str, str, Metric]], n_t: int, n_c: int, *,
               sitewide: bool = False) -> CompareRules:
    """测基线时定规则。波动范围和主判读都按建议间隔那一档取(没有那么长的档就取最长的)。"""
    th = defaults.thresholds()
    tiers = defaults.noise_tiers()
    control_valid = n_c >= th.min_control and not sitewide
    rules, used = [], set()
    for lang, provider, metric in legs:
        tier = pick_tier(tiers, metric, lang, provider, th.recommended_gap_days)
        if tier is None or n_t <= 0:
            rules.append(LegRule(lang=lang, provider=provider, metric=metric, n_T=n_t, n_C=n_c,
                                 control_valid=control_valid, primary=None, tier_days=None, band_pp=None,
                                 band_did=None))
            continue
        used.add(f"noise.{metric}.{lang}.{provider}.{tier.days}d")
        b_pp = stats.band_pp(tier.sigma, tier.tau, n_t, th.z)
        b_did = stats.band_did(tier.sigma, n_t, n_c, th.z) if control_valid else None
        primary = "did" if b_did is not None and b_did < b_pp else "pp"
        rules.append(LegRule(lang=lang, provider=provider, metric=metric, n_T=n_t, n_C=n_c,
                             control_valid=control_valid, primary=primary, tier_days=tier.days, band_pp=b_pp,
                             band_did=b_did))
    used_values = [v for v in defaults.all() if v.key in used or not v.key.startswith("noise.")]
    return CompareRules(method_version=METHOD_VERSION, thresholds=th, noise=tiers, legs=rules,
                        defaults_used=used_values)


def _judge(rules: CompareRules, facts: BatchFacts, data: LegData) -> LegResult:
    th = rules.thresholds
    base = {"lang": data.lang, "provider": data.provider, "metric": data.metric}

    def stop(label: LabelCode, reason: str, **extra) -> LegResult:
        return LegResult(**base, label=label, outcome=OUTCOME[label], reason=reason, **extra)

    if facts.domestic:
        return stop("not_remeasurable", "国内平台,不做再测")
    if not facts.deployed:
        return stop("not_deployed", "这一批改动还没部署")
    if facts.config_mismatch:
        return stop("config_changed", "前后不一致:" + "、".join(facts.config_mismatch))
    for name, ok, expected in (("基线", data.pre_ok, data.pre_expected), ("再测", data.post_ok, data.post_expected)):
        if expected <= 0 or ok / expected < th.min_success_rate:
            return stop("incomplete", f"{name}成功 {ok}/{expected},低于 {th.min_success_rate:.0%}")
    if facts.deploy_day is None:
        return stop("baseline_invalid", "没有部署日期")
    lead = _days(facts.baseline_day, facts.deploy_day)
    if lead < 0:
        return stop("baseline_invalid", "基线晚于部署")
    if lead > th.baseline_valid_days:
        return stop("baseline_invalid", f"部署在基线之后 {lead} 天,超过 {th.baseline_valid_days} 天")
    gap = _days(facts.baseline_day, facts.remeasure_day)

    by_id = {q.question_id: q for q in data.questions if q.pre_n > 0 and q.post_n > 0}
    t = [by_id[i] for i in facts.treated_q if i in by_id]
    c = [by_id[i] for i in facts.control_q if i in by_id]
    sizes = {"n_T": len(t), "n_C": len(c), "gap_days": gap}
    if data.metric == "citation" and (data.pre_searched_share is None
                                      or data.pre_searched_share < th.citation_min_searched_share):
        share = "未知" if data.pre_searched_share is None else f"{data.pre_searched_share:.0%}"
        return stop("not_comparable", f"基线里搜索过的回答只占 {share},这条腿不比被引率", **sizes)
    if len(t) < th.min_treated:
        return stop("not_comparable", f"处理组只有 {len(t)} 题,少于 {th.min_treated} 题", **sizes)
    tier = pick_tier(rules.noise, data.metric, data.lang, data.provider, gap)
    if tier is None:
        return stop("not_comparable", "没有这条腿的波动数据", **sizes)

    d_t = [q.post_hits / q.post_n - q.pre_hits / q.pre_n for q in t]
    d_c = [q.post_hits / q.post_n - q.pre_hits / q.pre_n for q in c]
    seed = th.seed ^ zlib.crc32(f"{data.lang}|{data.provider}|{data.metric}".encode())
    ci_t, ci_did = stats.bootstrap_ci(d_t, d_c, th.bootstrap_draws, stats.Rng(seed))
    delta_t = stats.mean(d_t)
    b_t = stats.band_pp(tier.sigma, tier.tau, len(t), th.z)
    out = {
        **sizes, "tier_days": tier.days, "comparable": True,
        "pre_T": stats.mean([q.pre_hits / q.pre_n for q in t]), "post_T": stats.mean([q.post_hits / q.post_n for q in t]),
        "d_T": delta_t, "ci_T": ci_t, "band_T": b_t,
    }
    if d_c:
        delta_c = stats.mean(d_c)
        out |= {"d_C": delta_c, "band_C": stats.band_pp(tier.sigma, tier.tau, len(c), th.z),
                "did": delta_t - delta_c, "ci_did": ci_did, "band_did": stats.band_did(tier.sigma, len(t), len(c), th.z)}

    control_valid = len(c) >= th.min_control and not facts.sitewide
    planned = next((r for r in rules.legs if (r.lang, r.provider, r.metric) == (data.lang, data.provider, data.metric)),
                   None)
    if not control_valid:
        primary = "pp"
    elif planned is not None and planned.primary is not None and planned.control_valid:
        primary = planned.primary  # 测基线时定的,不随再测结果改
    else:
        primary = "did" if out["band_did"] < b_t else "pp"
    out["primary"] = primary

    if control_valid and primary == "pp" and abs(out["d_C"]) > out["band_C"]:
        return LegResult(**base, **out, label="drift", outcome="inconclusive",
                         reason="对照组自己也超出了波动范围")
    stat, band, ci = (out["did"], out["band_did"], out["ci_did"]) if primary == "did" else (delta_t, b_t, ci_t)
    label: LabelCode = "up" if stat > band and ci[0] > 0 else "down" if stat < -band and ci[1] < 0 else "within"
    if not control_valid:
        why = "批次含全站改动" if facts.sitewide else f"对照组只有 {len(c)} 题,少于 {th.min_control} 题"
        return LegResult(**base, **out, label=label, no_control=True, outcome="inconclusive", reason=why)
    return LegResult(**base, **out, label=label, outcome=OUTCOME[label])


def compare_batch(rules: CompareRules, facts: BatchFacts, legs: list[LegData]) -> CompareResult:
    results, errors = [], []
    for data in legs:
        try:
            results.append(_judge(rules, facts, data))
        except Exception as exc:  # 数据有问题写进结果,不让整批比对崩掉
            errors.append(f"{data.lang} × {data.provider} × {data.metric}:{type(exc).__name__}: {exc}")
            results.append(LegResult(lang=data.lang, provider=data.provider, metric=data.metric,
                                     label="not_comparable", outcome="dnf", reason="数据有问题,见 errors"))
    judged = [r for r in results if r.label in ("up", "down", "within")]
    return CompareResult(batch_id=facts.batch_id, method_version=rules.method_version, legs=results,
                         judged=len(judged), exceeded=sum(1 for r in judged if r.label != "within"),
                         defaults_used=rules.defaults_used, errors=errors)
