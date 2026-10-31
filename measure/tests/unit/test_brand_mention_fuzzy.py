"""提及判定器·模糊版(S9.2,中档):精确 → 规整 → 模糊三道关;短名字不模糊;中文不模糊;
与规则判定器并排存档;档位的主判定器决定提及率与情感句的定位。零网络零数据库。"""

from datetime import UTC, datetime

import pytest

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector, find_mention
from engine.detectors.sentiment import SentimentDetector
from engine.models import (
    Answer,
    BrandSpec,
    MentionContext,
    Outcome,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from engine.observe import ObserveContext, observe, persist_observations
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
    assert obs.detector_name == "brand_mention_fuzzy" and obs.detector_version == 4


@pytest.mark.parametrize("text, snippet", [
    ("我们用 极光 CRM 管客户。", "极光 CRM"),  # 名字里多了空格
    ("Try Aurora-CRM today.", "Aurora-CRM"),  # 连字符
    ("推荐 ＡｕｒｏｒａＣＲＭ 给大家。", "ＡｕｒｏｒａＣＲＭ"),  # 全角
])
def test_normalized_pass_ignores_spaces_hyphens_and_width(text: str, snippet: str):
    obs = detect(text)
    assert obs is not None and obs.mentioned and obs.match_kind == "normalized" and obs.edit_distance == 0
    assert obs.matched_text == snippet and text[obs.first_position:].startswith(snippet)


@pytest.mark.parametrize("name, text", [
    ("Meta AI", "Meta, a company that makes headsets"),  # 第 11b 条
    ("Meta AI", "Meta, AI"), ("Meta AI", "Meta. AI assistants vary."), ("Meta AI", "Meta; AI"),
    ("Monday.com", "See you on Monday. Com tickets pile up."),
    ("Monday.com", "Monday, com"),
    ("Dr. Martens", "Dr, Martens"),
    ("AuroraCRM", "aurora.crm is fine"),  # 名字里没有的点号:回答里隔着就不算(v3)
    ("Aurora CRM", "Aurora 的 CRM"),  # 中间隔着别的字
    ("Aurora CRM", "Aurora\nCRM"),  # 不跨换行
])
def test_names_do_not_cross_punctuation(name: str, text: str):
    assert detect(text, BrandSpec(name=name)).mentioned is False


@pytest.mark.parametrize("name, text, snippet", [
    ("Meta AI", "Try MetaAI now", "MetaAI"),
    ("Meta AI", "Try Meta-AI now", "Meta-AI"),
    ("Meta AI", "Try ＭＥＴＡ　ＡＩ now", "ＭＥＴＡ　ＡＩ"),  # 全角空格
    ("Monday.com", "Monday com works", "Monday com"),  # 名字自带的点可以不写
    ("Monday.com", "ｍｏｎｄａｙ．ｃｏｍ works", "ｍｏｎｄａｙ．ｃｏｍ"),  # 全角的点还是那个点
    ("Dr. Martens", "Dr Martens boots", "Dr Martens"),
    ("Levi's", "Levi’s jeans", "Levi’s"),  # 弯撇号和直撇号是同一个
    ("Levi's", "Levis jeans", "Levis"),
    ("Arc'teryx", "ARC’TERYX jackets", "ARC’TERYX"),
    ("Ben & Jerry's", "Ben & Jerry’s tubs", "Ben & Jerry’s"),
])
def test_separators_the_name_itself_carries_may_be_written_or_dropped(name: str, text: str, snippet: str):
    obs = detect(text, BrandSpec(name=name))
    assert obs.mentioned and obs.match_kind == "normalized" and obs.matched_text == snippet


@pytest.mark.parametrize("name, text", [
    ("기아", "기압이 낮습니다"), ("기아", "기안을 올렸습니다"), ("삼성", "삼서"),  # 韩文收音不同是别的字(第 6 条)
    ("ドコモ", "トコモという店"), ("ドコモ", "ドゴモ"),  # 浊点不同是别的字
    ("Йод", "Иод"),  # 西里尔字母也不去符号,й 不是 и
])
def test_only_latin_letters_lose_their_marks(name: str, text: str):
    assert detect(text, BrandSpec(name=name)).mentioned is False


@pytest.mark.parametrize("name, text, snippet", [
    ("기아", "기아는 한국 회사입니다", "기아"),  # 带助词:照认
    ("ドコモ", "ドコモショップで契約", "ドコモ"),  # 片假名复合词:照认
    ("ドコモ", "ﾄﾞｺﾓ で契約", "ﾄﾞｺﾓ"),  # 半角片假名:ﾄ + ﾞ 合成 ド
    ("Citroën", "Citroën cars", "Citroën"),  # 拆开写的重音
    ("기아", "기아 회사", "기아"),  # 拆开写的韩文字母
])
def test_composed_forms_are_still_recognised(name: str, text: str, snippet: str):
    obs = detect(text, BrandSpec(name=name))
    assert obs.mentioned and obs.matched_text == snippet and text[obs.first_position:].startswith(snippet)


def test_vietnamese_tones_are_kept_only_on_vietnamese_jobs():
    """Q2(用户 2026-10-29 定):越南文里声调不同是别的词,越南语单不去;别的语言的回答写越南品牌常常不带声调,照去。"""
    brand = BrandSpec(name="Phở Hòa")
    assert find_mention("Pho Hoa", brand, "vi-VN") is None
    assert find_mention("Phờ Hòa", brand, "vi-VN") is None  # 换了声调
    assert find_mention("Phở Hòa", brand, "vi-VN").kind == "exact"
    assert find_mention("Phở Hòa", brand, "vi-VN").kind == "normalized"  # 同样的字拆开写
    assert find_mention("Pho Hoa", brand, "en-US").kind == "normalized"
    assert find_mention("Pho Hoa", brand, "zh-CN").kind == "normalized"
    assert find_mention("Citroen", BrandSpec(name="Citroën"), "vi-VN") is None  # 口径按语言,不按字母:越南语单一律不去


def test_normalized_pass_still_respects_word_boundaries():
    obs = detect("The Aurora-CRMS plugin is different.")  # 规整后 auroracrms:精确 / 规整都不算,模糊接住
    assert obs is not None and obs.mentioned and obs.match_kind == "fuzzy"


@pytest.mark.parametrize("text, snippet, distance", [
    ("The auroracrms plugin is popular.", "auroracrms", 1),  # 连写 + 复数(用户举的例子)
    ("Many teams pick Aurora CRMs.", "Aurora CRMs", 1),  # 两个词拼起来再比
])
def test_fuzzy_pass_catches_tail_variants_within_the_budget(text: str, snippet: str, distance: int):
    obs = detect(text)
    assert obs is not None and obs.mentioned and obs.match_kind == "fuzzy"
    assert (obs.matched_text, obs.edit_distance) == (snippet, distance)
    assert text[obs.first_position:].startswith(snippet)


def test_a_letter_changed_in_the_middle_is_another_word():
    """v2(用户 2026-10-27 定,审核黄 9):中间换一个字母就是别的词——Colombia 不是 Columbia;
    AI 回答几乎不会把品牌名中间拼错,AuroraCRN 这种也不再认。"""
    assert detect("Many teams pick AuroraCRN for pricing.").mentioned is False
    assert detect("I tried AuroxaCRM once.").mentioned is False
    columbia = BrandSpec(name="Columbia")
    assert detect("Great coffee comes from Colombia.", columbia).mentioned is False
    jackets = FuzzyBrandMentionDetector(MentionContext(category="jackets"))
    assert jackets.detect(run_with("Columbia jackets are warm."), columbia).mentioned is True


@pytest.mark.parametrize("name, text", [
    ("Canvas", "Canva is a design tool."),  # 少尾巴是别家
    ("Columbia", "Columbian coffee is famous."),  # 多别的尾巴是别的词
    ("Patagonia", "Patagonian stores opened in Tokyo."),
    ("AuroraCRM", "AuroraCR is another tool."),
    ("Target", "Each team has its own targets."),  # 加尾后成了普通词(Q1)
    ("Square", "Cut the cake into squares."),
    ("Notion", "All my notions live there."),
    ("Notion", "Notions of quality differ."),  # 大写也一样
    ("Monday", "Mondays are busy."),
])
def test_tails_are_a_closed_list_and_common_words_are_out(name: str, text: str):
    """Q1(用户 2026-10-29 定):少尾巴不认;多尾巴只认复数 / 所有格;加尾后成了普通词的不认。"""
    assert detect(text, BrandSpec(name=name)).mentioned is False


def test_a_common_word_tail_does_not_hide_a_later_real_tail():
    obs = FuzzyBrandMentionDetector(MentionContext(category="plan")).detect(
        run_with("Both notions and the Notiones plan."), BrandSpec(name="Notion"))
    assert obs.mentioned and obs.match_kind == "fuzzy" and obs.matched_text == "Notiones"


def test_possessive_and_plural_tails_are_still_recognised():
    patagonia = BrandSpec(name="Patagonia")
    tail = detect("Two Patagonias sat on the shelf.", patagonia)
    assert tail.mentioned and tail.match_kind == "fuzzy" and tail.edit_distance == 1
    assert detect("Patagonia's stores opened.", patagonia).match_kind == "exact"
    assert detect("Patagonia’s stores opened.", patagonia).match_kind == "exact"  # 弯撇号
    possessive = detect("I like AuroraCRM's pricing.")  # 所有格:撇号本来就是词边界,精确那道关直接认
    assert possessive.mentioned and possessive.match_kind == "exact"
    assert possessive.matched_text == "AuroraCRM"


@pytest.mark.parametrize("brand_name, text, snippet", [
    ("Citroën", "Citroen makes small cars.", "Citroen"),  # 回答里把重音写掉了
    ("Citroen", "Citroën makes small cars.", "Citroën"),  # 反过来:登记名没重音、回答带重音
    ("Moët", "A bottle of Moet, please.", "Moet"),  # 4 个字母:不走模糊,靠规整那道关
    ("Hermès", "Hermès and Hermes are the same house.", "Hermès"),  # 原样在前:精确先命中
])
def test_accents_are_folded_in_the_normalized_pass(brand_name: str, text: str, snippet: str):
    obs = detect(text, BrandSpec(name=brand_name))
    assert obs is not None and obs.mentioned and obs.edit_distance == 0
    assert obs.matched_text == snippet and text[obs.first_position:].startswith(snippet)


def test_short_names_take_no_tail():
    zoho = BrandSpec(name="Zoho")
    assert detect("Zoha is a different thing", zoho).mentioned is False  # 4 个字母:不模糊
    assert detect("Zohos are a different thing", zoho).mentioned is False
    assert detect("We use zoho daily", zoho).mentioned is True
    assert detect("Hokas are comfy", BrandSpec(name="Hoka")).mentioned is False
    assert detect("motion sensors everywhere", BrandSpec(name="Notion")).mentioned is False
    long_name = BrandSpec(name="Salesforce Marketing Cloud")
    obs = detect("I tried salesforce marketing clouds last year", long_name)  # 只看整个名字加尾,不看最后一个词
    assert obs.mentioned and obs.match_kind == "fuzzy" and obs.edit_distance == 1
    assert detect("I tried salesforce marketting cloud last year", long_name).mentioned is False  # 中间多字母:别的词
    assert detect("I tried salesforce marketing cloudiest last year", long_name).mentioned is False  # 不是复数尾巴
    assert detect("Patagoniaes are rare", BrandSpec(name="Patagonia")).edit_distance == 2  # es 算两个字符


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

    def record_version_first_seen(self, *, detector_name, version, rows_affected, trigger_kind, note=None) -> bool:
        """与 Storage 同规:以审计本身为准,记过就不再记。"""
        if any(e["detector_name"] == detector_name and e["version_to"] == version for e in self.events):
            return False
        prior = [o.detector_version for o in self.rows
                 if o.detector_name == detector_name and o.detector_version < version]
        self.events.append(dict(detector_name=detector_name, version_from=max(prior) if prior else None,
                                version_to=version, rows_affected=rows_affected, trigger_kind=trigger_kind, note=note))
        return True


def test_tier_method_picks_the_main_detector_and_sentiment_follows_it():
    run = run_with("用 auroracrms 的团队都说好用,推荐。")
    rules = observe(run, BRAND, ObserveContext(competitors=[], mention_method=MentionMethod.RULES))
    fuzzy = observe(run, BRAND, ObserveContext(competitors=[], mention_method=MentionMethod.RULES_FUZZY))
    assert rules.mention is not None and rules.mention.mentioned is False and rules.sentiment is None
    assert fuzzy.mention is not None and fuzzy.mention.mentioned is True
    assert fuzzy.mention.detector_name == "brand_mention_fuzzy"
    assert fuzzy.sentiment is not None and fuzzy.sentiment.sentiment.value == "positive"  # 情感句跟主判定器定位
    assert ObserveContext(competitors=[]).mention_method is MentionMethod.RULES
    assert rules.mention.detector_name == "brand_mention_rules"
    # 高档一家裁判都请不到:按模糊判定器算,报告写明(BR6,用户定)
    no_judge = observe(run, BRAND, ObserveContext(competitors=[], mention_method=MentionMethod.RULES_FUZZY_JUDGE))
    assert no_judge.mention is not None and no_judge.mention.detector_name == "brand_mention_fuzzy"
    assert no_judge.sentiment is not None and no_judge.sentiment.detector_name == "sentiment_lexicon"


def test_both_mention_detectors_are_archived_side_by_side():
    store = ListStore()
    run = run_with("用 auroracrms 的团队都说好用。")
    context = ObserveContext(competitors=[], mention_method=MentionMethod.RULES_FUZZY)
    persist_observations(store, [observe(run, BRAND, context)])
    mentions = {(o.detector_name, o.detector_version): o.mentioned for o in store.rows
                if o.detector_name.startswith("brand_mention")}
    assert mentions == {("brand_mention_rules", 3): False, ("brand_mention_fuzzy", 4): True}
    tones = [o for o in store.rows if o.detector_name == "sentiment_lexicon"]
    assert len(tones) == 1 and tones[0].detector_version == 3  # 情感随主判定器(模糊)找到了句子
    # 判定器版本第一次出现在库里:落审计(13.5;审核 2026-10-27 黄 8);再存一遍版本都见过了,不再落
    assert {(e["detector_name"], e["version_from"], e["version_to"], e["trigger_kind"]) for e in store.events} >= {
        ("brand_mention_rules", None, 3, "job"), ("brand_mention_fuzzy", None, 4, "job")}
    seen = len(store.events)
    persist_observations(store, [observe(run, BRAND, context)], note="job-x")
    assert len(store.events) == seen


def test_sentiment_detector_needs_a_mention_detector():
    assert SentimentDetector(FUZZY).version == 3
    run = run_with("Aurora CRMs 不推荐。")
    assert SentimentDetector(BrandMentionDetector()).detect(run, BRAND) is None  # 规则没认出 → 情感不可判
    tone = SentimentDetector(FUZZY).detect(run, BRAND)
    assert tone is not None and tone.sentiment.value == "negative"
