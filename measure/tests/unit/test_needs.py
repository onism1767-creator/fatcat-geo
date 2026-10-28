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
    suggest_competitors,
)
from engine.models import Answer, BrandSpec, Outcome, PromptIntent, PromptOrigin, SurfaceRun

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM"], domains=["aurora-crm.com"])
CATEGORY = "客户关系管理软件(CRM)"

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
        [c.text for c in candidates if c.intent is not PromptIntent.BRAND_FACT], BRAND
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
    assert brand_collisions(texts, BRAND) == ["AURORACRM 好用吗", "见 aurora-crm.com 官网"]


def test_brand_collisions_respect_word_boundaries_for_short_latin_names():
    """品牌叫 On:含 "on" 的题不是撞车,点名 "On" 的才是(审核 2026-10-27 黄 10);中文名照旧按子串。"""
    on = BrandSpec(name="On", aliases=["On Running"], domains=["on.com"])
    texts = ["What should I look for in a marathon shoe?", "Is On a good running brand?",
             "Compare on-running shoes", "see on.com for details"]  # on-running:连字符是词边界,这是点名了
    assert brand_collisions(texts, on) == ["Is On a good running brand?", "Compare on-running shoes",
                                           "see on.com for details"]
    assert brand_collisions(["极光CRM系统怎么样", "极光好看吗"], BRAND) == ["极光CRM系统怎么样"]


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
