"""S7 回答文字(v0.4.24 §4.1 / 4.2,用户定 2026-10-13):和竞品比谁被提得多(规则、免费;现成的提及判定器对竞品也跑一遍)
+ AI 原话只进证据区(每道题按采样顺序前 3 次回答,不挑不改;没提到只记没提到;无效回答另记)。"""

from datetime import UTC, datetime

from diagnosis.messages import render_msg
from diagnosis.representation_diag import compare_mentions, diagnose_representation
from engine.aggregate import QUOTES_PER_PROMPT, measure_target
from engine.detectors.matching import mention_sentence
from engine.metrics import rate
from engine.models import Answer, BrandSpec, Outcome, PromptIntent, SearchMode, StopReason, SurfaceRun, Target
from engine.observe import ObserveContext, observe

BRAND = BrandSpec(name="AuroraCRM", aliases=["Aurora CRM"], domains=["aurora-crm.com"])
RIVAL = BrandSpec(name="Beta CRM", domains=["betacrm.com"])
OTHER = BrandSpec(name="Gamma", domains=["gamma.io"])


def _run(run_id: str, prompt_id: str, index: int, text: str) -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id=prompt_id, run_index=index, provider="p", surface="s1", model="m",
        search_mode=SearchMode.DEFAULT, locale="en-US", outcome=Outcome.SUCCESS,
        answer=Answer(answer_text=text, citations_available=False, citations=[]),
        raw_payload={}, timestamp=datetime(2026, 10, 13, tzinfo=UTC), latency_ms=1, cost_usd=0.0,
    )


RUNS = [
    _run("r1", "p1", 1,
         "Beta CRM is the market leader. AuroraCRM is a solid alternative for small teams. Gamma is new."),
    _run("r2", "p1", 2, "Beta CRM and Gamma are popular picks."),
    _run("r3", "p1", 3, ""),  # 空回答 = 无效,不进分母,原话记「没有有效回答」
    _run("r4", "p1", 4, "Aurora CRM wins."),  # 第 4 次:算进比例,但原话只取前 3 次
    _run("r5", "p2", 1, "Try Beta CRM."),
    _run("r6", "p2", 2, "Nothing specific."),
]


def _measure():
    context = ObserveContext(competitors=[RIVAL, OTHER])
    observations = [observe(run, BRAND, context) for run in RUNS]
    return measure_target(Target(surface="s1", model="m"), "p", PromptIntent.CATEGORY_DISCOVERY, observations,
                          ["p1", "p2"], stop_reason=StopReason.COMPLETED, competitor_names=["Beta CRM", "Gamma"],
                          exposes_retrieval=False)


def test_mention_sentence_is_the_sentence_containing_the_position():
    text = "极光CRM很好。另一家一般!再说一句"
    assert mention_sentence(text, text.index("另")) == "另一家一般"
    assert mention_sentence(text, text.index("再")) == "再说一句"
    en = "Beta leads. AuroraCRM fits small teams. Done."
    assert mention_sentence(en, en.index("AuroraCRM")).strip() == "AuroraCRM fits small teams"


def test_competitor_mentions_are_counted_with_the_same_detector_over_valid_answers():
    m = _measure()
    assert m.mention.rate == rate(2, 5)  # r1、r4 提到你;r3 无效不进分母
    assert m.competitor_mentions == [("Beta CRM", rate(3, 5)), ("Gamma", rate(2, 5))]


def test_quotes_take_the_first_three_answers_per_prompt_in_sampling_order_without_picking():
    m = _measure()
    assert QUOTES_PER_PROMPT == 3
    rows = [(q.prompt_no, q.run_index, q.mentioned, q.sentence) for q in m.quotes]
    assert rows == [
        (1, 1, True, "AuroraCRM is a solid alternative for small teams"),  # 第一次提到你的那一句,原样
        (1, 2, False, None),  # 没提到只记没提到(用户定 Q1)
        (1, 3, None, None),  # 无效回答
        (2, 1, False, None),
        (2, 2, False, None),
    ]  # r4 是第 4 次,不取
    assert [q.run_id for q in m.quotes][:3] == ["r1", "r2", "r3"]


def test_compare_mentions_says_more_or_less_only_beyond_the_gap():
    m = _measure()
    compare = compare_mentions("AuroraCRM", m.mention, m.competitor_mentions)
    assert compare is not None and compare.gap == 0.2
    assert (compare.brand.name, compare.brand.rate, compare.brand.relation) == ("AuroraCRM", rate(2, 5), None)
    assert [(c.name, c.rate, c.relation) for c in compare.competitors] == [
        ("Beta CRM", rate(3, 5), "more"),  # 60% 对 40%:差 20 个百分点 → 比你多
        ("Gamma", rate(2, 5), "close"),  # 一样多 → 差不多
    ]
    assert compare_mentions("AuroraCRM", m.mention, []) is None  # 没有竞品就没有这一块
    assert compare_mentions("AuroraCRM", None, m.competitor_mentions) is None  # 你的比例看不到也没有


def test_representation_evidence_lists_competitor_mentions_in_both_registers():
    m = _measure()
    finding = diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, m.mention, {}, competitors=m.competitor_mentions)
    lines = [line for line in finding.evidence_lines if line.code.startswith("mention.compare.")]
    assert [line.code for line in lines] == ["mention.compare.more", "mention.compare.close"]
    assert render_msg(lines[0], "zh") == "回答里提到竞品 Beta CRM:3/5 = 60%,比提到你多 20 个百分点以上"
    assert render_msg(lines[1], "zh") == "回答里提到竞品 Gamma:2/5 = 40%,与提到你的次数相差不足 20 个百分点,分不出多少"
    assert render_msg(lines[0], "zh", "plain") == "5 次回答里提到 Beta CRM 3 次(60%),比你多"
    assert render_msg(lines[1], "en", "plain") == "Gamma came up in 2 of 5 answers (40%) — about the same as you"
    without = diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, m.mention, {})
    assert not any(line.code.startswith("mention.compare.") for line in without.evidence_lines)
