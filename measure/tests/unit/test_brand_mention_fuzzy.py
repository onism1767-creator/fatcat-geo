"""提及判定器·模糊版(S9.2,中档):精确 → 规整 → 模糊三道关;短名字不模糊;中文不模糊;
与规则判定器并排存档;档位的主判定器决定提及率与情感句的定位。零网络零数据库。"""

from datetime import UTC, datetime

import pytest

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector, edit_budget
from engine.detectors.sentiment import SentimentDetector
from engine.models import (
    Answer,
    BrandSpec,
    Outcome,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from engine.observe import ObserveContext, mention_detector, observe, persist_observations
from engine.tiers import MentionMethod

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM", "Aurora CRM"], domains=["aurora-crm.com"])
FUZZY = FuzzyBrandMentionDetector()


def run_with(text: str, run_id: str = "r1", outcome: Outcome = Outcome.SUCCESS) -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id="p1", run_index=1, provider="test_provider", surface="s1", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=outcome,
        answer=Answer(answer_text=text, citations_available=True, citations=[]) if outcome is Outcome.SUCCESS else None,
        error=(None if outcome is Outcome.SUCCESS
               else RunError(error_type=RunErrorType.REFUSAL, message="content_filter")),
        search_trace=SearchTrace(), raw_payload={}, timestamp=datetime(2026, 10, 20, tzinfo=UTC), latency_ms=1,
        cost_usd=0.0,
    )


def detect(text: str, brand: BrandSpec = BRAND):
    return FUZZY.detect(run_with(text), brand)


def test_exact_hits_match_the_rules_detector_and_say_so():
    obs = detect("对于中小团队,极光CRM 和 Beta 都值得一试。")
    rules = BrandMentionDetector().detect(run_with("对于中小团队,极光CRM 和 Beta 都值得一试。"), BRAND)
    assert obs is not None and rules is not None
    assert (obs.mentioned, obs.matched_alias, obs.first_position) == (True, "极光CRM", rules.first_position)
    assert (obs.match_kind, obs.matched_text, obs.edit_distance) == ("exact", "极光CRM", 0)
    assert obs.detector_name == "brand_mention_fuzzy" and obs.detector_version == 1


@pytest.mark.parametrize("text, snippet", [
    ("我们用 极光 CRM 管客户。", "极光 CRM"),  # 名字里多了空格
    ("Try Aurora-CRM today.", "Aurora-CRM"),  # 连字符
    ("推荐 ＡｕｒｏｒａＣＲＭ 给大家。", "ＡｕｒｏｒａＣＲＭ"),  # 全角
    ("aurora.crm is fine", "aurora.crm"),  # 点号
])
def test_normalized_pass_ignores_separators_and_width(text: str, snippet: str):
    obs = detect(text)
    assert obs is not None and obs.mentioned and obs.match_kind == "normalized" and obs.edit_distance == 0
    assert obs.matched_text == snippet and text[obs.first_position:].startswith(snippet)


def test_normalized_pass_still_respects_word_boundaries():
    obs = detect("The Aurora-CRMS plugin is different.")  # 规整后 auroracrms:精确 / 规整都不算,模糊接住
    assert obs is not None and obs.mentioned and obs.match_kind == "fuzzy"


@pytest.mark.parametrize("text, snippet, distance", [
    ("The auroracrms plugin is popular.", "auroracrms", 1),  # 连写 + 复数(用户举的例子)
    ("Many teams pick AuroraCRN for pricing.", "AuroraCRN", 1),  # 打错一个字母
    ("Many teams pick Aurora CRMs.", "Aurora CRMs", 1),  # 两个词拼起来再比
])
def test_fuzzy_pass_catches_variants_within_the_budget(text: str, snippet: str, distance: int):
    obs = detect(text)
    assert obs is not None and obs.mentioned and obs.match_kind == "fuzzy"
    assert (obs.matched_text, obs.edit_distance) == (snippet, distance)
    assert text[obs.first_position:].startswith(snippet)


def test_short_names_are_exact_only_and_prefix_must_match():
    assert edit_budget(4) == 0 and edit_budget(6) == 1 and edit_budget(9) == 1 and edit_budget(10) == 2
    zoho = BrandSpec(name="Zoho")
    assert detect("Zoha is a different thing", zoho).mentioned is False  # 4 个字母:不模糊
    assert detect("We use zoho daily", zoho).mentioned is True
    notion = BrandSpec(name="Notion")
    assert detect("motion sensors everywhere", notion).mentioned is False  # 差 1 但首字母不同:不算
    assert detect("All my notions live there", notion).mentioned is True  # 复数:算
    long_name = BrandSpec(name="Salesforce Marketing Cloud")  # 规整后 24 个字符:允许差 2
    obs = detect("I tried salesforce marketting cloud last year", long_name)
    assert obs.mentioned and obs.match_kind == "fuzzy" and obs.edit_distance == 1
    assert detect("I tried salesforse marketting clod last year", long_name).mentioned is False  # 差 3


def test_cjk_names_are_substring_only_never_fuzzy():
    assert detect("极光CRN 不是我们。").mentioned is False  # 中英混合的名字不模糊
    only_cjk = BrandSpec(name="极光客户通")
    assert detect("极光客户宝 是别家。", only_cjk).mentioned is False
    assert detect("用 极光客户通 的人不少。", only_cjk).mentioned is True


def test_earliest_hit_wins_across_passes():
    obs = detect("auroracrms 很好;后面才写 AuroraCRM。")  # 模糊命中在前、精确在后:取最早的
    assert obs is not None and obs.first_position == 0 and obs.match_kind == "fuzzy"
    obs2 = detect("AuroraCRM 很好;后面 auroracrms。")
    assert obs2 is not None and obs2.first_position == 0 and obs2.match_kind == "exact"


def test_no_valid_answer_means_no_observation_and_absence_is_recorded():
    assert FUZZY.detect(run_with("", outcome=Outcome.REFUSED), BRAND) is None
    assert FUZZY.detect(run_with("   "), BRAND) is None
    obs = detect("常见选择包括 Alpha、Beta 与 Gamma。")
    assert obs is not None and obs.mentioned is False and obs.match_kind is None and obs.matched_text is None


# ── 档位的主判定器(engine/observe.py) ─────────────────────────────────────────────────


class ListStore:
    def __init__(self) -> None:
        self.rows: list = []
        self.events: list[dict] = []

    def insert_observation(self, observation) -> bool:
        self.rows.append(observation)
        return True

    def max_detector_version(self, detector_name: str) -> int | None:
        versions = [o.detector_version for o in self.rows if o.detector_name == detector_name]
        return max(versions) if versions else None

    def insert_recompute_event(self, **kw) -> None:
        self.events.append(kw)


def test_tier_method_picks_the_main_detector_and_sentiment_follows_it():
    run = run_with("用 auroracrms 的团队都说好用,推荐。")
    rules = observe(run, BRAND, ObserveContext(competitors=[], mention_method=MentionMethod.RULES))
    fuzzy = observe(run, BRAND, ObserveContext(competitors=[], mention_method=MentionMethod.RULES_FUZZY))
    assert rules.mention is not None and rules.mention.mentioned is False and rules.sentiment is None
    assert fuzzy.mention is not None and fuzzy.mention.mentioned is True
    assert fuzzy.mention.detector_name == "brand_mention_fuzzy"
    assert fuzzy.sentiment is not None and fuzzy.sentiment.sentiment.value == "positive"  # 情感句跟主判定器定位
    assert ObserveContext(competitors=[]).mention_method is MentionMethod.RULES
    assert mention_detector(MentionMethod.RULES).name == "brand_mention_rules"
    with pytest.raises(ValueError, match="裁判"):
        mention_detector(MentionMethod.RULES_FUZZY_JUDGE)  # 高档没装配裁判:明确报错,不静默退回


def test_both_mention_detectors_are_archived_side_by_side():
    store = ListStore()
    run = run_with("用 auroracrms 的团队都说好用。")
    context = ObserveContext(competitors=[], mention_method=MentionMethod.RULES_FUZZY)
    persist_observations(store, [observe(run, BRAND, context)])
    mentions = {(o.detector_name, o.detector_version): o.mentioned for o in store.rows
                if o.detector_name.startswith("brand_mention")}
    assert mentions == {("brand_mention_rules", 2): False, ("brand_mention_fuzzy", 1): True}
    tones = [o for o in store.rows if o.detector_name == "sentiment_lexicon"]
    assert len(tones) == 1 and tones[0].detector_version == 3  # 情感随主判定器(模糊)找到了句子
    # 判定器版本第一次出现在库里:落审计(13.5;审核 2026-10-27 黄 8);再存一遍版本都见过了,不再落
    assert {(e["detector_name"], e["version_from"], e["version_to"], e["trigger_kind"]) for e in store.events} >= {
        ("brand_mention_rules", None, 2, "job"), ("brand_mention_fuzzy", None, 1, "job")}
    seen = len(store.events)
    persist_observations(store, [observe(run, BRAND, context)], note="job-x")
    assert len(store.events) == seen


def test_sentiment_detector_needs_a_mention_detector():
    assert SentimentDetector(FUZZY).version == 3
    run = run_with("Aurora CRMs 不推荐。")
    assert SentimentDetector(BrandMentionDetector()).detect(run, BRAND) is None  # 规则没认出 → 情感不可判
    tone = SentimentDetector(FUZZY).detect(run, BRAND)
    assert tone is not None and tone.sentiment.value == "negative"
