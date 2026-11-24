"""出题测试(产品文档 15.2 五类 + 第 17 章盲起草):四步序、机械质检、竞品解析。

假适配器记录收到的每个 prompt——"真盲"不是靠嘴说,是断言出来的:
不点名的题的生成 prompt 里绝不允许出现品牌名/别名/域名。
"""

import json
from datetime import UTC, datetime

import pytest

from engine.drafting import (
    DEFAULT_COUNTS,
    brand_collisions,
    draft_prompt_candidates,
    manual_intent,
    suggest_competitors,
)
from engine.models import Answer, BrandSpec, Outcome, PromptIntent, PromptOrigin, SurfaceRun

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM"], domains=["aurora-crm.com"])
CATEGORY = "客户关系管理软件(CRM)"
LOCALE = "zh-CN"

BLIND_OK = {
    "category_discovery": [f"发现题{i}" for i in range(1, 9)],
    "problem_solution": [f"场景题{i}" for i in range(1, 5)],
    "recommendation": [f"推荐题{i}" for i in range(1, 5)],
}
COMPETITORS_OK = {
    "competitors": [
        {"name": "SalesLoop", "domain": "salesloop.example"},
        {"name": "极光CRM", "domain": "aurora-crm.com"},  # 焦点自己,应被剔除
        {"name": "SalesLoop", "domain": "salesloop.example"},  # 重复,应被剔除
        {"name": "CloudDesk", "domain": "clouddesk.example"},
    ]
}
COMPARISON_OK = {"comparison_alternative": ["SalesLoop 的替代品有哪些?", "SalesLoop 和 CloudDesk 怎么选?",
                                            "这类软件还有哪些更好的选择?", "CloudDesk 值不值得买?"]}
FACTS_OK = {"brand_fact": ["极光CRM 是做什么的?", "极光CRM 怎么样?优缺点分别是什么?",
                           "极光CRM 和同类相比性价比如何?", "极光CRM 适合什么样的团队?"]}


class RecordingAdapter:
    """按调用次序返回脚本化 JSON 回答,并记录每个收到的 prompt。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.prompts: list[str] = []

    def ask(self, request):
        self.prompts.append(request.prompt_text)
        payload = self._payloads.pop(0)
        return SurfaceRun(
            run_id=request.run_id,
            prompt_id=request.prompt_id,
            run_index=request.run_index,
            provider="test_provider", surface="test_surface", model="m1",
            search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS,
            answer=Answer(answer_text=json.dumps(payload, ensure_ascii=False),
                          citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0,
        )


def test_draft_orchestration_and_blindness():
    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK, FACTS_OK])
    candidates, competitors = draft_prompt_candidates(
        adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None
    )
    # 配比:8 / 4 / 4 / 4 / 4 = 24(产品文档 15.2)
    by_intent = {intent: [c for c in candidates if c.intent is intent] for intent in PromptIntent}
    assert {intent: len(items) for intent, items in by_intent.items()} == DEFAULT_COUNTS
    assert len(candidates) == 24 and all(c.origin is PromptOrigin.ASSISTED for c in candidates)
    # 真盲:第一步(不点名的三类)的 prompt 不含品牌名/别名/域名;对比题 prompt 带竞品名;品牌事实必须含焦点名
    blind_prompt, competitor_prompt, comparison_prompt, facts_prompt = adapter.prompts
    for token in ("极光CRM", "auroracrm", "aurora-crm.com"):
        assert token.lower() not in blind_prompt.lower(), f"不点名的题 prompt 泄漏品牌:{token}"
        assert token.lower() not in comparison_prompt.lower(), f"对比题 prompt 泄漏品牌:{token}"
    assert "极光CRM" in competitor_prompt  # 竞品建议需要知道排除谁
    assert "SalesLoop" in comparison_prompt and "CloudDesk" in comparison_prompt
    assert "极光CRM" in facts_prompt
    # 竞品:剔除焦点与重复
    assert [c.name for c in competitors] == ["SalesLoop", "CloudDesk"]


def test_blind_brand_leak_is_rejected_then_retried():
    leaked = {**BLIND_OK, "category_discovery": ["极光CRM 是最好的吗?"] + BLIND_OK["category_discovery"][1:]}
    adapter = RecordingAdapter([leaked, BLIND_OK, COMPETITORS_OK, COMPARISON_OK, FACTS_OK])
    candidates, _ = draft_prompt_candidates(adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None)
    assert len(adapter.prompts) == 5  # 第一步第一次被质检打回,重试了一次
    assert not brand_collisions(
        [c.text for c in candidates if c.intent is not PromptIntent.BRAND_FACT], BRAND, LOCALE
    )


def test_wrong_count_exhausts_retries_and_fails_loudly():
    short = {**BLIND_OK, "problem_solution": ["只有一条"]}
    adapter = RecordingAdapter([short, short, short])
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_prompt_candidates(adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None)


def test_comparison_prompts_must_not_name_focal_brand():
    bad = {"comparison_alternative": ["极光CRM 的替代品有哪些?", "a", "b", "c"]}
    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK] + [bad] * 3)
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_prompt_candidates(adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None)


def test_brand_fact_prompts_must_name_focal_brand():
    bad_facts = {"brand_fact": ["这家公司怎么样?", "它和同类相比如何?", "它是做什么的?", "它适合谁?"]}
    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK] + [bad_facts] * 3)
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_prompt_candidates(adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None)


def test_suggest_competitors_standalone():
    adapter = RecordingAdapter([COMPETITORS_OK])
    competitors = suggest_competitors(adapter, category=CATEGORY, brand=BRAND, k=1, ledger=lambda run: None)
    assert [c.name for c in competitors] == ["SalesLoop"]  # 封顶 k


def test_brand_collisions_scans_name_alias_domain():
    texts = ["AURORACRM 好用吗", "无关问题", "见 aurora-crm.com 官网"]
    assert brand_collisions(texts, BRAND, LOCALE) == ["AURORACRM 好用吗", "见 aurora-crm.com 官网"]


def test_prompt_checks_recognise_what_the_medium_tier_recognises():
    """第 9 条:题里点没点名按档位里最宽的免费判法认,比判定器窄的话回答里算提及的写法在题里却算没点名,提及率虚高。"""
    wide = ["Is Aurora-CRM any good?", "auroracrms 好用吗", "ＡｕｒｏｒａＣＲＭ 怎么样"]
    assert brand_collisions(wide, BRAND, LOCALE) == wide
    assert all(manual_intent(text, BRAND, LOCALE) is PromptIntent.BRAND_FACT for text in wide)
    assert manual_intent("哪些 CRM 适合小团队?", BRAND, LOCALE) is PromptIntent.CATEGORY_DISCOVERY
    assert not brand_collisions(["Which targets matter?"], BrandSpec(name="Target"), "en-US")  # 加尾成了普通词


def test_brand_fact_prompts_count_as_naming_only_when_the_detector_says_so():
    """品牌事实题原来按子串查名字:品牌 On 时任何含 on 的题都算点了名(reputation)。别名点的也算。"""
    on = BrandSpec(name="On", aliases=["On Running"], domains=["on.com"])
    facts = {"brand_fact": ["What is its reputation?", "Is it a good option?", "Does it last in this condition?",
                            "Why only online?"]}
    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK] + [facts] * 3)
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_prompt_candidates(adapter, category=CATEGORY, brand=on, ledger=lambda run: None, locale="en-US")


def test_brand_collisions_respect_word_boundaries_for_short_latin_names():
    """品牌叫 On:含 "on" 的题不是撞车,点名 "On" 的才是(审核 2026-10-27 黄 10);中文名照旧按子串。"""
    on = BrandSpec(name="On", aliases=["On Running"], domains=["on.com"])
    texts = ["What should I look for in a marathon shoe?", "Is On a good running brand?",
             "Compare on-running shoes", "see on.com for details"]  # on-running:连字符是词边界,这是点名了
    assert brand_collisions(texts, on, "en-US") == ["Is On a good running brand?", "Compare on-running shoes",
                                                    "see on.com for details"]
    assert brand_collisions(["极光CRM系统怎么样", "极光好看吗"], BRAND, LOCALE) == ["极光CRM系统怎么样"]


def test_json_tolerates_markdown_fences():
    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK, FACTS_OK])
    original_ask = adapter.ask

    def ask_with_fence(request):
        run = original_ask(request)
        wrapped = f"好的,以下是结果:\n```json\n{run.answer.answer_text}\n```"
        return run.model_copy(update={
            "answer": Answer(answer_text=wrapped, citations_available=False)
        })

    adapter.ask = ask_with_fence
    candidates, _ = draft_prompt_candidates(adapter, category=CATEGORY, brand=BRAND, ledger=lambda run: None)
    assert len(candidates) == 24


def test_brand_domain_collisions_need_a_boundary_on_both_sides():
    """品牌 On 的域名 on.com:题里的 amazon.com 不算撞车(原来按子串算,第二轮审核 B11);它自己的子域、带路径的照算。"""
    on = BrandSpec(name="On", domains=["on.com"])
    texts = ["Is amazon.com cheaper for running shoes?", "Visit shop.on.com today", "see on.com/shoes",
             "neon.com deals"]
    assert brand_collisions(texts, on, "en-US") == ["Visit shop.on.com today", "see on.com/shoes"]


def test_suggested_competitors_never_include_the_brands_own_alias_or_spelling():
    """第四轮审核:AI 把品牌自己的别名 / 连写当竞品给了(别名 AuroraCRM、写法 Aurora-CRM)。原来只排除和品牌名
    完全相同的:它进了竞品候选,对比题一点它的名就被判成「题里点了品牌名」,三轮都撞、出题失败,钱已经花了。"""
    own = {"competitors": [{"name": "AuroraCRM", "domain": "aurora-crm.com"},
                           {"name": "Aurora-CRM", "domain": "aurora.example"},
                           {"name": "SalesLoop", "domain": "salesloop.example"}]}
    competitors = suggest_competitors(RecordingAdapter([own]), category=CATEGORY, brand=BRAND,
                                      ledger=lambda run: None)
    assert [c.name for c in competitors] == ["SalesLoop"]
