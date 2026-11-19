"""再测比对与默认值(设计 §9.4、§9.5、§11)。"""

from __future__ import annotations

import pytest

from butter.compare import compare_batch, jeffreys, label_text, plan_rules
from butter.compare import stats
from butter.config import Defaults, DefaultsError, load_defaults, parse_defaults, pick_tier
from butter.contracts import BatchFacts, DefaultValue, LegData, NoiseTier, QuestionObs

DEFAULTS = load_defaults()
LEG = ("zh", "anthropic", "mention")


def obs(qid: str, pre: int, post: int, n: int = 3) -> QuestionObs:
    return QuestionObs(question_id=qid, pre_hits=pre, pre_n=n, post_hits=post, post_n=n)


def facts(n_t: int = 12, n_c: int = 12, **kw) -> BatchFacts:
    base = dict(batch_id="b1", treated_q=[f"t{i}" for i in range(n_t)], control_q=[f"c{i}" for i in range(n_c)],
                baseline_day="2026-11-01", deploy_day="2026-11-03", remeasure_day="2026-11-29")
    return BatchFacts(**(base | kw))


def leg(t_pairs, c_pairs, **kw) -> LegData:
    qs = [obs(f"t{i}", a, b) for i, (a, b) in enumerate(t_pairs)] + [obs(f"c{i}", a, b) for i, (a, b) in enumerate(c_pairs)]
    base = dict(lang="zh", provider="anthropic", metric="mention", questions=qs, pre_ok=len(qs) * 3,
                pre_expected=len(qs) * 3, post_ok=len(qs) * 3, post_expected=len(qs) * 3)
    return LegData(**(base | kw))


def run(t_pairs, c_pairs, f: BatchFacts | None = None, **leg_kw):
    f = f or facts(len(t_pairs), len(c_pairs))
    rules = plan_rules(DEFAULTS, [(leg_kw.get("lang", "zh"), leg_kw.get("provider", "anthropic"),
                                   leg_kw.get("metric", "mention"))], len(f.treated_q), len(f.control_q),
                       sitewide=f.sitewide)
    return compare_batch(rules, f, [leg(t_pairs, c_pairs, **leg_kw)]).legs[0]


FLAT = [(1, 1)] * 12
UP = [(0, 3)] * 12
DOWN = [(3, 0)] * 12


# ---- 默认值 ----

def test_every_default_has_source_and_plain_sentence():
    assert len(DEFAULTS) >= 20
    for v in DEFAULTS.all():
        assert v.source.strip() and v.source_plain.strip() and v.measured_on.startswith("2026-"), v.key
        for word in ("σ", "τ", "单次观测", "临时", "事后", "预注册", "E0"):  # 界面那句话不出现研究用语(§11)
            assert word not in v.source_plain, (v.key, word)


def test_noise_defaults_cover_six_legs_and_two_metrics():
    tiers = DEFAULTS.noise_tiers()
    keys = {(t.metric, t.lang, t.provider, t.days) for t in tiers}
    for metric in ("mention", "citation"):
        for lang in ("zh", "en"):
            for provider in ("anthropic", "openai", "gemini"):
                assert (metric, lang, provider, 7) in keys
    by = {(t.metric, t.lang, t.provider): t for t in tiers}
    for (metric, lang, provider), t in by.items():
        assert 0.05 < t.sigma < 0.4 and 0 <= t.tau < 0.1
        if metric == "citation":  # 被引率的波动范围不比提及率窄
            m = by[("mention", lang, provider)]
            assert t.sigma >= m.sigma and t.tau >= m.tau


def test_interval_default_is_28_days():
    assert DEFAULTS.get("remeasure.interval_days") == 28
    assert DEFAULTS.thresholds().recommended_gap_days == 28


def test_duplicate_or_malformed_defaults_are_rejected():
    v = DEFAULTS.all()[0]
    with pytest.raises(DefaultsError):
        Defaults([v, v])
    with pytest.raises(DefaultsError):
        parse_defaults({"defaults": [{"key": "x", "value": 1}]}, origin="t")
    with pytest.raises(DefaultsError):
        parse_defaults([], origin="t")
    with pytest.raises(DefaultsError):
        DEFAULTS.get("no.such.key")


def test_pick_tier_takes_nearest_not_shorter_else_longest():
    tiers = [NoiseTier(metric="mention", lang="zh", provider="x", days=d, sigma=0.1, tau=0.0) for d in (7, 14, 21, 28)]
    assert pick_tier(tiers, "mention", "zh", "x", 17).days == 21
    assert pick_tier(tiers, "mention", "zh", "x", 40).days == 28
    assert pick_tier(tiers, "mention", "zh", "x", 3).days == 7
    assert pick_tier(tiers, "mention", "zh", "x", 28).days == 28
    assert pick_tier(tiers, "mention", "en", "x", 7) is None


# ---- 统计小工具 ----

def test_rng_is_deterministic_and_spread():
    a, b = stats.Rng(7), stats.Rng(7)
    xs = [a.randrange(10) for _ in range(2000)]
    assert xs == [b.randrange(10) for _ in range(2000)]
    assert set(xs) == set(range(10)) and all(120 < xs.count(i) < 280 for i in range(10))


def test_jeffreys_interval_matches_known_values():
    lo, hi = jeffreys(1, 3)
    assert lo == pytest.approx(0.0387, abs=1e-3) and hi == pytest.approx(0.8233, abs=1e-3)  # 与 scipy beta.ppf 对过
    assert jeffreys(0, 3)[0] == 0.0 and jeffreys(3, 3)[1] == 1.0
    lo0, hi0 = jeffreys(0, 3)
    assert 0.4 < hi0 < 0.7
    assert jeffreys(0, 0) == (0.0, 1.0)
    lo, hi = jeffreys(50, 100)
    assert lo == pytest.approx(0.4032, abs=1e-3) and hi == pytest.approx(0.5968, abs=1e-3)


def test_bands_follow_the_formulas():
    assert stats.band_pp(0.2, 0.0, 16, 1.96) == pytest.approx(1.96 * 0.05)
    assert stats.band_pp(0.2, 0.03, 10_000, 1.96) == pytest.approx(1.96 * 0.03, rel=0.01)  # τ 是压不下去的下限
    assert stats.band_did(0.2, 8, 8, 1.96) == pytest.approx(1.96 * 0.1)


# ---- 主判读预选 ----

def test_primary_is_the_narrower_band_and_is_frozen_at_baseline():
    few = plan_rules(DEFAULTS, [LEG], 6, 6).legs[0]
    assert few.primary == "pp" and few.band_pp < few.band_did  # 题少:前后差更窄
    many = plan_rules(DEFAULTS, [("zh", "gemini", "mention")], 200, 200).legs[0]
    assert many.primary == "did" and many.band_did < many.band_pp  # 题多、τ 大:差中差更窄
    no_control = plan_rules(DEFAULTS, [LEG], 12, 2).legs[0]
    assert no_control.primary == "pp" and no_control.band_did is None and not no_control.control_valid
    sitewide = plan_rules(DEFAULTS, [LEG], 12, 12, sitewide=True).legs[0]
    assert sitewide.primary == "pp" and not sitewide.control_valid
    unknown = plan_rules(DEFAULTS, [("zh", "perplexity", "mention")], 12, 12).legs[0]
    assert unknown.primary is None and unknown.band_pp is None


def test_rules_snapshot_carries_the_defaults_it_used():
    rules = plan_rules(DEFAULTS, [LEG], 12, 12)
    keys = {v.key for v in rules.defaults_used}
    assert "noise.mention.zh.anthropic.7d" in keys and "compare.min_treated_questions" in keys
    assert "noise.mention.en.openai.7d" not in keys
    assert all(isinstance(v, DefaultValue) for v in rules.defaults_used)


# ---- 总表,按行 ----

def test_row1_domestic_platform():
    r = run(UP, FLAT, facts(domestic=True))
    assert r.label == "not_remeasurable" and r.outcome is None


def test_row2_not_deployed():
    r = run(UP, FLAT, facts(deployed=False))
    assert r.label == "not_deployed" and r.outcome is None


def test_row3_config_changed():
    r = run(UP, FLAT, facts(config_mismatch=["题板", "引擎型号"]))
    assert r.label == "config_changed" and r.outcome == "dnf" and "引擎型号" in r.reason


def test_row4_incomplete_measurement():
    r = run(UP, FLAT, post_ok=60, post_expected=72)
    assert r.label == "incomplete" and r.outcome == "dnf" and "再测" in r.reason
    assert run(UP, FLAT, post_ok=65, post_expected=72).label == "up"  # 90.3% 够


def test_row5_baseline_invalid():
    assert run(UP, FLAT, facts(deploy_day="2026-10-30")).label == "baseline_invalid"  # 基线晚于部署
    assert run(UP, FLAT, facts(deploy_day="2026-11-09")).label == "baseline_invalid"  # 部署在基线 8 天后
    assert run(UP, FLAT, facts(deploy_day=None)).label == "baseline_invalid"
    assert run(UP, FLAT, facts(deploy_day="2026-11-08")).label == "up"  # 正好 7 天


def test_row6_too_few_treated_or_leg_not_compared():
    r = run(UP[:4], FLAT)
    assert r.label == "not_comparable" and r.outcome == "dnf" and r.n_T == 4
    r = run(UP, FLAT, metric="citation", provider="gemini", pre_searched_share=0.02)
    assert r.label == "not_comparable" and "不比被引率" in r.reason
    assert run(UP, FLAT, metric="citation", pre_searched_share=None).label == "not_comparable"
    assert run(UP, FLAT, metric="citation", pre_searched_share=0.5).label == "up"
    bare = run(UP, FLAT, provider="perplexity")
    assert bare.label == "not_comparable" and bare.band_T is None and bare.ci_T is None  # 没有这条腿的波动数据:不判
    assert bare.pre_T is not None and bare.d_T == pytest.approx(bare.post_T - bare.pre_T) and bare.d_T > 0  # 但数照列


def test_row7_no_valid_control_keeps_direction_but_is_inconclusive():
    r = run(UP, FLAT[:3])
    assert r.label == "up" and r.no_control and r.outcome == "inconclusive"
    assert label_text(r) == "超出波动上升(无有效对照)"
    r = run(DOWN, FLAT, facts(sitewide=True))
    assert r.label == "down" and r.no_control and r.outcome == "inconclusive" and "全站" in r.reason
    r = run(FLAT, [])
    assert r.label == "within" and r.no_control and r.did is None


def test_row8_drift_when_control_moves_too():
    r = run(UP, UP)
    assert r.primary == "pp" and r.label == "drift" and r.outcome == "inconclusive"


def test_row9_up_row10_down_row11_within():
    up = run(UP, FLAT)
    assert up.label == "up" and up.outcome == "won" and up.d_T == pytest.approx(1.0) and up.ci_T[0] > 0
    down = run(DOWN, FLAT)
    assert down.label == "down" and down.outcome == "lost" and down.ci_T[1] < 0
    flat = run(FLAT, FLAT)
    assert flat.label == "within" and flat.outcome == "inconclusive" and flat.d_T == 0
    assert label_text(flat) == "在波动内"


def test_needs_both_band_and_interval_to_call_a_change():
    # 均值超出波动范围,但只靠一两道题撑着:区间跨过 0,不算
    t = [(0, 3), (0, 3), (0, 3)] + [(1, 1)] * 5 + [(3, 2)] * 4
    r = run(t, FLAT)
    assert r.label == "within"
    small = [(1, 2)] * 3 + [(1, 1)] * 9  # 区间不含 0,但没超出波动范围
    r = run(small, FLAT)
    assert r.d_T > 0 and r.d_T < r.band_T and r.label == "within"


def test_did_primary_cancels_common_drift():
    t = [(0, 2)] * 100 + [(1, 3)] * 100
    c = [(0, 2)] * 100 + [(1, 3)] * 100  # 两组一起涨 2/3:差中差为 0
    f = facts(200, 200)
    r = run(t, c, f, provider="gemini")
    assert r.primary == "did" and r.did == pytest.approx(0.0) and r.label == "within"
    r = run(t, [(1, 1)] * 200, f, provider="gemini")
    assert r.primary == "did" and r.label == "up" and r.outcome == "won"


def test_tier_follows_actual_gap_and_questions_missing_a_side_are_dropped():
    r = run(UP, FLAT)
    assert r.gap_days == 28 and r.tier_days == 7  # 现在只有 7 天档:取最长的一档
    f = facts(12, 12)
    data = leg(UP, FLAT)
    qs = list(data.questions)
    qs[0] = QuestionObs(question_id="t0", pre_hits=0, pre_n=3, post_hits=0, post_n=0)  # 再测没拿到回答
    rules = plan_rules(DEFAULTS, [LEG], 12, 12)
    r = compare_batch(rules, f, [data.model_copy(update={"questions": qs})]).legs[0]
    assert r.n_T == 11 and r.label == "up"


def test_result_is_reproducible_and_counts_legs():
    f = facts(12, 12)
    legs = [leg(UP, FLAT), leg(FLAT, FLAT, provider="openai"), leg(UP, FLAT, provider="perplexity")]
    rules = plan_rules(DEFAULTS, [("zh", p, "mention") for p in ("anthropic", "openai", "perplexity")], 12, 12)
    a, b = compare_batch(rules, f, legs), compare_batch(rules, f, legs)
    assert a == b
    assert a.judged == 2 and a.exceeded == 1 and not a.errors and a.method_version == "compare/1"


def test_bad_data_goes_into_errors_not_an_exception():
    rules = plan_rules(DEFAULTS, [LEG], 12, 12)
    res = compare_batch(rules, facts(remeasure_day="not-a-date"), [leg(UP, FLAT)])
    assert res.errors and res.legs[0].label == "not_comparable" and res.legs[0].outcome == "dnf"


def test_placebo_unchanged_questions_stay_within_noise():
    """什么都没改:按默认值里的 σ、τ 造正态分布的「不改动」数据,误报「变了」的应在 5% 上下。

    波动范围是 95% 的范围,所以在理想的正态数据上误报率就是 5% 左右(这里放到 8% 以内,留出抽样误差)。
    设计 §16.2 的完工标准(未改动数据 ≥ 95% 不报「变了」)用 E0 真实数据验:
    experiments/E0/analysis/placebo_compare.py,2026-11-17 结果是 5700 次里误报 25 次(0.4%)。
    """
    tier = next(t for t in DEFAULTS.noise_tiers() if (t.lang, t.provider, t.metric) == LEG)
    rng = stats.Rng(42)

    def gauss() -> float:  # Box-Muller 的近似:12 个均匀数之和
        return sum(rng.next_u64() / 2 ** 64 for _ in range(12)) - 6.0

    f = facts(12, 12)
    rules = plan_rules(DEFAULTS, [LEG], 12, 12)
    trials, within = 400, 0
    for _ in range(trials):
        shift = tier.tau * gauss()
        qs = []
        for qid in f.treated_q + f.control_q:
            d = shift + tier.sigma * gauss()
            pre = 500
            qs.append(QuestionObs(question_id=qid, pre_hits=pre, pre_n=1000,
                                  post_hits=max(0, min(1000, round(pre + d * 1000))), post_n=1000))
        data = LegData(lang="zh", provider="anthropic", metric="mention", questions=qs, pre_ok=72, pre_expected=72,
                       post_ok=72, post_expected=72)
        within += compare_batch(rules, f, [data]).legs[0].label in ("within", "drift")
    assert within / trials >= 0.92
