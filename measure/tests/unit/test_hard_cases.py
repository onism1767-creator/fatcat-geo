"""校准难题集(BR1,engine/hard_cases.py):机械变形造得对;配置里的难名字覆盖五类毛病,配套正例齐。零网络零数据库。"""

from __future__ import annotations

from engine.detectors.brand_mention_fuzzy import find_mention
from engine.hard_cases import (
    HardBrand,
    cases_of,
    kana_neighbours,
    korean_neighbours,
    load_hard_brands,
    load_real_brands,
    prompts_of,
    punctuation_splits,
    strip_marks,
    tone_neighbour,
)
from engine.models import BrandSpec, PromptIntent


def test_real_questions_cover_six_chinese_and_english_brands():
    brands = load_real_brands()
    assert [b.name for b in brands] == ["On", "Notion", "Canvas", "Meta AI", "小米", "美的"]
    assert {b.locale for b in brands} == {"en-US", "zh-CN"}  # 用户 2026-10-30 定:先只跑中英文
    assert all((len(b.naming), len(b.counter), len(b.open)) == (3, 2, 3) for b in brands)


def test_naming_questions_name_the_brand_and_open_questions_do_not():
    for brand in load_real_brands():
        spec = BrandSpec(name=brand.name, aliases=brand.aliases)
        assert all(find_mention(q, spec, brand.locale) is not None for q in brand.naming), brand.name
        assert all(find_mention(q, spec, brand.locale) is None for q in brand.open), brand.name


def test_real_prompts_keep_their_ids_so_a_rerun_does_not_ask_again():
    brand = load_real_brands()[0]
    first, second = prompts_of(brand), prompts_of(brand)
    assert [p.prompt_id for p in first] == [p.prompt_id for p in second]
    assert len({p.prompt_id for p in first}) == 8
    assert all(p.locale == brand.locale for p in first)
    assert [p.intent for p in first[:3]] == [PromptIntent.BRAND_FACT] * 3


def test_korean_names_get_a_final_consonant_added_or_removed():
    assert korean_neighbours("기아") == ["기압", "기안", "기암"]
    assert korean_neighbours("삼성") == ["삼서"]
    assert korean_neighbours("Kia") == [] and korean_neighbours("小米") == []


def test_japanese_names_get_their_voicing_marks_swapped():
    assert kana_neighbours("ドコモ") == ["トコモ", "ドゴモ"]
    assert "ゾニー" in kana_neighbours("ソニー")  # ニ、ー 没有对应的浊音,不造
    assert kana_neighbours("小米") == []


def test_vietnamese_names_get_one_tone_changed_or_all_stripped():
    swapped = tone_neighbour("Phở Hòa")
    assert swapped is not None and swapped != "Phở Hòa" and strip_marks(swapped) == "Pho Hoa"
    assert tone_neighbour("Notion") is None
    assert strip_marks("Citroën") == "Citroen"


def test_names_with_several_parts_are_split_by_punctuation():
    assert punctuation_splits("Meta AI") == ["Meta, AI", "Meta. AI", "Meta; AI", "Meta, a company"]
    assert punctuation_splits("Monday.com")[:2] == ["Monday, Com", "Monday. Com"]
    assert punctuation_splits("Notion") == [] and punctuation_splits("小米 手机") == []


def test_vietnamese_answers_depend_on_the_answer_language():
    """用户 2026-10-29 定 Q2:越南语单不去声调,别的语言单去声调算同一个名字。"""
    cases = cases_of(HardBrand(name="Phở Hòa", tone_language="vi-VN"))
    by_kind = {c.kind: c for c in cases}
    answers = {kind: (case.expected, case.locale) for kind, case in by_kind.items()}
    assert answers["越南文换声调"] == (False, "vi-VN")
    assert answers["越南文去声调(越南语单)"] == (False, "vi-VN")
    assert answers["越南文去声调(别的语言单)"] == (True, "en-US")


def test_the_config_covers_all_five_known_problems_with_matching_positives():
    """调研报告的五类毛病(常用词、尾巴撞名、中日韩越文字、跨标点、中文词中词)都要有难题,都要有配套正例防止修过头。"""
    brands = load_hard_brands()
    kinds = {(case.kind, case.expected) for brand in brands for case in cases_of(brand)}
    for kind in ("常用词", "少尾巴撞别家", "多尾巴撞别词", "韩文近形", "日文近形", "越南文换声调",
                 "越南文去声调(越南语单)", "跨标点", "中文词中词"):
        assert (kind, False) in kinds, kind
    for kind in ("带助词", "片假名复合词", "半角片假名", "所有格", "全角", "去重音", "语境里的品牌", "原样",
                 "越南文去声调(别的语言单)"):
        assert (kind, True) in kinds, kind
    names = [brand.name for brand in brands]
    assert len(names) == len(set(names))
    assert all(case.text.strip() for brand in brands for case in cases_of(brand))
