"""需求辅助生成测试(第 17 章):盲起草三步序、机械质检、竞品解析。

假适配器记录收到的每个 prompt——"真盲"不是靠嘴说,是断言出来的:
中立题的生成 prompt 里绝不允许出现品牌名/别名/域名。
"""

import json
from datetime import UTC, datetime

import pytest

from engine.models import Answer, BrandSpec, NeedIntent, Outcome, SurfaceRun
from engine.needs import (
    DEFAULT_NEUTRAL_COUNTS,
    brand_collisions,
    draft_need_candidates,
    suggest_competitors,
)

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM"], domains=["aurora-crm.com"])
CATEGORY = "客户关系管理软件(CRM)"

NEUTRAL_OK = {
    "discovery": [f"发现题{i}" for i in range(1, 7)],
    "comparison": [f"比较题{i}" for i in range(1, 5)],
    "scenario": [f"场景题{i}" for i in range(1, 5)],
}
PROBES_OK = {"brand_probe": ["极光CRM 怎么样?优缺点分别是什么?", "极光CRM 和同类相比性价比如何?"]}
COMPETITORS_OK = {
    "competitors": [
        {"name": "SalesLoop", "domain": "salesloop.example"},
        {"name": "极光CRM", "domain": "aurora-crm.com"},  # 焦点自己,应被剔除
        {"name": "SalesLoop", "domain": "salesloop.example"},  # 重复,应被剔除
        {"name": "CloudDesk", "domain": "clouddesk.example"},
    ]
}


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
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="test_provider", surface="test_surface", model="m1",
            search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS,
            answer=Answer(answer_text=json.dumps(payload, ensure_ascii=False),
                          citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0,
        )


def test_draft_orchestration_and_blindness():
    adapter = RecordingAdapter([NEUTRAL_OK, COMPETITORS_OK, PROBES_OK])
    candidates, competitors = draft_need_candidates(
        adapter, category=CATEGORY, brand=BRAND
    )
    # 配比:6/4/4 中立 + 2 探针
    by_intent = {intent: [c for c in candidates if c.intent is intent] for intent in NeedIntent}
    assert len(by_intent[NeedIntent.DISCOVERY]) == DEFAULT_NEUTRAL_COUNTS[NeedIntent.DISCOVERY]
    assert len(by_intent[NeedIntent.COMPARISON]) == DEFAULT_NEUTRAL_COUNTS[NeedIntent.COMPARISON]
    assert len(by_intent[NeedIntent.SCENARIO]) == DEFAULT_NEUTRAL_COUNTS[NeedIntent.SCENARIO]
    assert len(by_intent[NeedIntent.BRAND_PROBE]) == 2
    # 真盲:第一步(中立题)的 prompt 不含品牌名/别名/域名;第三步(探针)必须含
    neutral_prompt, competitor_prompt, probe_prompt = adapter.prompts
    for token in ("极光CRM", "auroracrm", "aurora-crm.com"):
        assert token.lower() not in neutral_prompt.lower(), f"中立题 prompt 泄漏品牌:{token}"
    assert "极光CRM" in competitor_prompt  # 竞品建议需要知道排除谁
    assert "极光CRM" in probe_prompt
    # 竞品:剔除焦点与重复
    assert [c.name for c in competitors] == ["SalesLoop", "CloudDesk"]


def test_neutral_brand_leak_is_rejected_then_retried():
    leaked = {**NEUTRAL_OK, "discovery": ["极光CRM 是最好的吗?"] + NEUTRAL_OK["discovery"][1:]}
    adapter = RecordingAdapter([leaked, NEUTRAL_OK, COMPETITORS_OK, PROBES_OK])
    candidates, _ = draft_need_candidates(adapter, category=CATEGORY, brand=BRAND)
    assert len(adapter.prompts) == 4  # 中立题第一次被质检打回,重试了一次
    assert not brand_collisions(
        [c.text for c in candidates if c.intent is not NeedIntent.BRAND_PROBE], BRAND
    )


def test_wrong_ratio_exhausts_retries_and_fails_loudly():
    short = {**NEUTRAL_OK, "comparison": ["只有一条"]}
    adapter = RecordingAdapter([short, short, short])
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_need_candidates(adapter, category=CATEGORY, brand=BRAND)


def test_probe_must_name_focal_brand():
    bad_probes = {"brand_probe": ["这家公司怎么样?", "它和同类相比如何?"]}
    adapter = RecordingAdapter([NEUTRAL_OK, COMPETITORS_OK] + [bad_probes] * 3)
    with pytest.raises(ValueError, match="未产出合格结果"):
        draft_need_candidates(adapter, category=CATEGORY, brand=BRAND)


def test_suggest_competitors_standalone():
    adapter = RecordingAdapter([COMPETITORS_OK])
    competitors = suggest_competitors(adapter, category=CATEGORY, brand=BRAND, k=1)
    assert [c.name for c in competitors] == ["SalesLoop"]  # 封顶 k


def test_brand_collisions_scans_name_alias_domain():
    texts = ["AURORACRM 好用吗", "无关问题", "见 aurora-crm.com 官网"]
    assert brand_collisions(texts, BRAND) == ["AURORACRM 好用吗", "见 aurora-crm.com 官网"]


def test_json_tolerates_markdown_fences():
    fenced = RecordingAdapter([NEUTRAL_OK, COMPETITORS_OK, PROBES_OK])
    fenced._payloads = list(fenced._payloads)  # 保持接口
    # 用带围栏的回答验证 _extract_json 容错:改造第一个 payload 的包装
    adapter = RecordingAdapter([NEUTRAL_OK, COMPETITORS_OK, PROBES_OK])

    original_ask = adapter.ask

    def ask_with_fence(request):
        run = original_ask(request)
        wrapped = f"好的,以下是结果:\n```json\n{run.answer.answer_text}\n```"
        return run.model_copy(update={
            "answer": Answer(answer_text=wrapped, citations_available=False)
        })

    adapter.ask = ask_with_fence
    candidates, _ = draft_need_candidates(adapter, category=CATEGORY, brand=BRAND)
    assert len(candidates) == 16
