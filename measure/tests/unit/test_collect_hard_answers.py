"""BR3 跑题脚本(scripts/collect_hard_answers.py):用假 AI 走一遍「真问」,不花钱。需要本地测试库。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapters.base import AdapterAccountError, CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from engine.models import (
    Answer,
    AskRequest,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    RunErrorType,
    StopReason,
    SurfaceRun,
)
from scripts.collect_hard_answers import collect
from tests.unit.conftest import TEST_DSN


class FakeAdapter(SurfaceAdapter):
    def __init__(self, surface: str, *, cost: float = 0.01, broke: bool = False) -> None:
        self._surface, self._cost, self._broke = surface, cost, broke
        self.asked: list[AskRequest] = []

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            display_name=self._surface, api_name=self._surface, provider=f"{self._surface}_co", surface=self._surface,
            models=["daily", "top"], default_model="top", consumer_default_model="daily", strongest_model="top",
            models_checked="test", capabilities=CapabilityRegistry(supports_citations=True),
            est_cost_per_call_usd={"daily": 0.01, "top": 0.5},
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        self.asked.append(request)
        if self._broke:
            raise AdapterAccountError("没额度了", error_type=RunErrorType.ACCOUNT_CREDIT)
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id, run_index=request.run_index,
            provider=f"{self._surface}_co", surface=self._surface, model=request.model or "top",
            search_mode=request.search_mode, locale=request.locale, outcome=Outcome.SUCCESS,
            answer=Answer(answer_text="一段回答", citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=10, cost_usd=self._cost,
        )

    def healthcheck(self) -> bool:
        return True


def prompts(*locales: str) -> list[Prompt]:
    return [Prompt(text=f"第 {i} 题", intent=PromptIntent.BRAND_FACT, origin=PromptOrigin.ASSISTED, locale=locale)
            for i, locale in enumerate(locales)]


def test_every_surface_asks_each_question_once_with_its_consumer_model_and_language(storage):
    questions = prompts("en-US", "en-US", "zh-CN")
    one, two = FakeAdapter("bridge_one"), FakeAdapter("bridge_two")

    results, problems = collect({"bridge_one": one, "bridge_two": two}, questions, TEST_DSN)

    assert problems == [] and len(results) == 4  # 两家 × 两种语言
    assert all(r.stop_reason is StopReason.COMPLETED for r in results)
    for adapter in (one, two):
        assert sorted(r.prompt_id for r in adapter.asked) == sorted(p.prompt_id for p in questions)
        assert {r.model for r in adapter.asked} == {"daily"}
        assert {r.run_index for r in adapter.asked} == {1}
    assert {r.prompt_id: r.locale for r in one.asked} == {p.prompt_id: p.locale for p in questions}
    stored = storage.runs_for_prompt(questions[2].prompt_id, surface="bridge_one", model="daily")
    assert [r.locale for r in stored] == ["zh-CN"]


def test_named_models_are_asked_instead_of_the_consumer_default(storage):
    """成本调研①:--models 只问点名的型号(给没实测过的型号攒实测),上限按那个型号的声明价算。"""
    questions = prompts("zh-CN", "zh-CN")
    picky, skipped = FakeAdapter("picky_surface"), FakeAdapter("skipped_surface")

    results, problems = collect({"picky_surface": picky, "skipped_surface": skipped}, questions, TEST_DSN, only=["top"])

    assert problems == [] and {r.model for r in picky.asked} == {"top"} and len(picky.asked) == 2
    assert {r.model for r in skipped.asked} == {"top"}  # 两家都有 top:各问各的
    assert [(r.surface, r.model, r.n_attempts) for r in results] == [
        ("picky_surface", "top", 2), ("skipped_surface", "top", 2)]


def test_running_again_only_asks_what_is_missing(storage):
    questions = prompts("en-US", "en-US")
    first = FakeAdapter("rerun_surface")
    collect({"rerun_surface": first}, questions[:1], TEST_DSN)

    second = FakeAdapter("rerun_surface")
    results, _ = collect({"rerun_surface": second}, questions, TEST_DSN)

    assert [r.prompt_id for r in second.asked] == [questions[1].prompt_id]
    assert results[0].n_attempts == 2 and results[0].total_cost_usd == 0.02


def test_a_surface_stops_at_its_cap(storage):
    expensive = FakeAdapter("pricey_surface", cost=0.05)  # 上限 = 0.01 × 3 × 1.1 = 0.033,问一题就超

    results, problems = collect({"pricey_surface": expensive}, prompts("en-US", "en-US", "en-US"), TEST_DSN)

    assert problems == [] and len(expensive.asked) == 1
    assert results[0].stop_reason is StopReason.BUDGET_EXHAUSTED


def test_an_account_problem_stops_only_that_surface(storage):
    questions = prompts("en-US", "zh-CN")
    broke, fine = FakeAdapter("broke_surface", broke=True), FakeAdapter("fine_surface")

    results, problems = collect({"broke_surface": broke, "fine_surface": fine}, questions, TEST_DSN)

    assert len(broke.asked) == 1  # 第一题就停,第二种语言不再问
    assert [p.surface for p in problems] == ["broke_surface"]
    assert {r.surface for r in results} == {"fine_surface"} and len(fine.asked) == 2
