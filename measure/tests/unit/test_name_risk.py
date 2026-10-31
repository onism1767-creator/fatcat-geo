"""有风险的名字(BR4,engine/detectors/name_risk.py):分档、线索、说不准。零网络零数据库。"""

from datetime import UTC, datetime

import pytest

from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector, find_mention
from engine.detectors.name_risk import Risk, category_terms, is_risky, risk_of
from engine.models import Answer, BrandSpec, Citation, MentionContext, Outcome, SearchMode, SearchTrace, SurfaceRun


def run_with(text: str, citations: list[Citation] | None = None, locale: str = "en-US") -> SurfaceRun:
    return SurfaceRun(
        run_id="r1", prompt_id="p1", run_index=1, provider="test_provider", surface="s1", model="m1",
        search_mode=SearchMode.DEFAULT, locale=locale, outcome=Outcome.SUCCESS,
        answer=Answer(answer_text=text, citations_available=True, citations=citations or []),
        search_trace=SearchTrace(), raw_payload={}, timestamp=datetime(2026, 10, 31, tzinfo=UTC), latency_ms=1,
        cost_usd=0.0,
    )


def mentioned(brand: BrandSpec, text: str, context: MentionContext | None = None) -> bool | None:
    obs = FuzzyBrandMentionDetector(context).detect(run_with(text), brand)
    assert obs is not None
    return obs.mentioned


@pytest.mark.parametrize("name, risk", [
    ("On", Risk.WORD), ("HP", Risk.WORD), ("Notion", Risk.WORD), ("Canvas", Risk.WORD),
    ("小米", Risk.WORD), ("美的", Risk.WORD),
    ("Meta AI", Risk.PHRASE), ("On Running", Risk.PHRASE),
    ("Hoka", Risk.NONE), ("Salesforce", Risk.NONE), ("Monday.com", Risk.NONE), ("Dr. Martens", Risk.NONE),
    ("始祖鸟", Risk.NONE), ("极光CRM", Risk.NONE),
    ("기아", Risk.NONE), ("ドコモ", Risk.NONE),  # 这轮只管中英文
])
def test_names_are_sorted_by_how_risky_they_are(name: str, risk: Risk):
    assert risk_of(name) is risk


def test_a_brand_is_risky_when_any_of_its_names_needs_clues():
    assert is_risky(BrandSpec(name="Hoka", aliases=["On"])) is True
    assert is_risky(BrandSpec(name="Hoka", aliases=["Hoka One One"])) is False
    assert is_risky(BrandSpec(name="Meta AI")) is False  # 词组只看大小写,不靠线索,不用预留裁判


@pytest.mark.parametrize("category, text", [
    ("running shoes", "a running shoe"), ("running shoes", "Shoes"), ("家用空调", "空调"), ("跑鞋、运动鞋", "运动鞋"),
])
def test_category_words_match_singular_plural_and_the_last_two_hanzi(category: str, text: str):
    assert any(term.search(text.lower()) for term in category_terms(category))


def test_category_terms_skip_filler_words():
    assert not any(term.search("the best tools and brands") for term in category_terms("the tools and brands"))


def test_every_occurrence_is_read_not_just_the_first():
    assert mentioned(BrandSpec(name="On"), "Focus on form. Hoka and On lead here.",
                     MentionContext(peers=["Hoka"])) is True


def test_clues_only_count_nearby():
    far = "Hoka is great.\nA.\nB.\nC.\nOn is great."
    assert mentioned(BrandSpec(name="On"), far, MentionContext(peers=["Hoka"])) is None
    near = "Hoka is great.\nOn is great."
    assert mentioned(BrandSpec(name="On"), near, MentionContext(peers=["Hoka"])) is True


def test_a_peer_written_in_lowercase_is_no_clue():
    assert mentioned(BrandSpec(name="Apple"), "Apple pie and a hoka.", MentionContext(peers=["Hoka"])) is None


def test_a_peer_that_contains_the_name_is_no_clue():
    context = MentionContext(peers=["Apple Bank"])
    assert mentioned(BrandSpec(name="Apple"), "Apple Bank opened a branch.", context) is None


def test_another_registered_name_nearby_is_a_clue():
    brand = BrandSpec(name="小米", aliases=["Xiaomi"])
    assert mentioned(brand, "小米 (Xiaomi) 发布了新机。") is True


def test_the_official_site_anywhere_in_the_answer_is_a_clue():
    brand = BrandSpec(name="On", domains=["on-running.com"])
    assert mentioned(brand, "On is a Swiss firm.\nA.\nB.\nC.\nSee on-running.com for more.") is True


def test_a_citation_of_the_official_site_is_a_clue_even_behind_a_redirect():
    brand = BrandSpec(name="On", domains=["on-running.com"])
    direct = Citation(url="https://www.on-running.com/x", domain="www.on-running.com", position=1)
    redirect = Citation(url="https://redirect.example/abc", domain="redirect.example", title="on-running.com",
                        position=1)
    for citation in (direct, redirect):
        obs = FuzzyBrandMentionDetector().detect(run_with("On is a Swiss firm.", [citation]), brand)
        assert obs is not None and obs.mentioned is True and obs.clue is not None


def test_a_split_phrase_of_common_words_is_not_the_name():
    """BR2 试跑时发现:名字中间允许空格,普通短语拆开正好是名字。"""
    brand = BrandSpec(name="Salesforce")
    assert mentioned(brand, "They built a strong sales force last year.") is False
    assert mentioned(brand, "Sales Force is a CRM.") is None  # 大小写像名字,没线索
    assert mentioned(brand, "Sales Force is a CRM.", MentionContext(category="CRM")) is True
    assert mentioned(BrandSpec(name="极光CRM"), "我们用 极光 CRM 管客户。") is True  # 中文这种写法照认


def test_names_without_risk_behave_as_before():
    assert mentioned(BrandSpec(name="Hoka"), "hoka is great") is True  # 小写也算
    assert mentioned(BrandSpec(name="始祖鸟"), "始祖鸟很贵。") is True


def test_uppercase_counts_as_the_right_case():
    assert mentioned(BrandSpec(name="Notion"), "ＮＯＴＩＯＮ is an app.", MentionContext(category="app")) is True
    assert mentioned(BrandSpec(name="Notion"), "NOTION is an app.", MentionContext(category="app")) is True


def test_a_name_registered_in_lowercase_accepts_its_own_spelling():
    assert mentioned(BrandSpec(name="notion"), "notion is an app.", MentionContext(category="app")) is True


@pytest.mark.parametrize("text", ["小米粥很养胃。", "三星级酒店。"])
def test_compounds_found_in_the_large_word_list_are_not_the_brand(text: str):
    brand = BrandSpec(name=text[:2])
    assert mentioned(brand, text) is False


@pytest.mark.parametrize("name, text", [
    ("美的", "完美的旅行"), ("美的", "很美的确如此"), ("联想", "让人联想到海边"), ("三星", "三星期之后"),
])
def test_a_name_cutting_through_another_word_is_not_the_brand(name: str, text: str):
    assert mentioned(BrandSpec(name=name), text) is False


def test_a_cut_word_with_a_clue_stays_unsure_for_the_judge():
    context = MentionContext(category="空调")
    assert mentioned(BrandSpec(name="美的"), "建议选美的空调。", context) is None  # 「选美」切开了,但确实可能是品牌


def test_unsure_keeps_the_place_it_saw():
    obs = FuzzyBrandMentionDetector().detect(run_with("Notion is great."), BrandSpec(name="Notion"))
    assert obs is not None and obs.mentioned is None
    assert (obs.matched_text, obs.first_position, obs.matched_alias) == ("Notion", 0, "Notion")


def test_the_clue_is_recorded():
    detector = FuzzyBrandMentionDetector(MentionContext(peers=["Hoka"]))
    obs = detector.detect(run_with("Hoka and On."), BrandSpec(name="On"))
    assert obs is not None and obs.mentioned is True and obs.clue == "Hoka"


def test_question_checks_still_look_at_the_letters_only():
    """出题质检不看上下文:题目里写了名字就是写了(engine/drafting.py)。"""
    assert find_mention("Is Notion a good tool?", BrandSpec(name="Notion"), "en-US") is not None
    assert find_mention("the notion of justice", BrandSpec(name="Notion"), "en-US") is not None
