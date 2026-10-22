"""回答缓存(S11.2):同品牌、同题(规范化)、同型号 × 模式、12 小时内的有效回答复用——
复制成本单的运行、花费记 0、复用关系进只追加表;
别的品牌 / 过期 / 失败 / 另一种模式的不复用。零网络;库表部分用真库。"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from engine.cache import CACHE_HOURS, cached_answers, fill_from_cache, normalize_prompt
from engine.models import (
    Answer,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    SearchMode,
    SearchTrace,
    SurfaceRun,
    Target,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
TARGET = Target(surface="s1", model="m1")


def make_run(prompt_id: str, run_id: str, *, hours_ago: float = 1, mode: SearchMode = SearchMode.DEFAULT,
             outcome: Outcome = Outcome.SUCCESS, text: str = "回答") -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id=prompt_id, run_index=1, provider="p", surface="s1", model="m1",
        search_mode=mode, locale="zh-CN", outcome=outcome,
        answer=Answer(answer_text=text, citations_available=True) if outcome is Outcome.SUCCESS else None,
        error=None if outcome is Outcome.SUCCESS else {"error_type": "TRANSIENT", "message": "x"},
        search_trace=SearchTrace(), raw_payload={"k": run_id}, timestamp=NOW - timedelta(hours=hours_ago),
        latency_ms=5, cost_usd=0.02,
    )


class FakeStore:
    def __init__(self, prompts, runs):
        self._prompts, self._runs = prompts, runs
        self.inserted: list[SurfaceRun] = []
        self.reuses: list[dict] = []

    def brand_prompts(self, brand_name, *, exclude_job_id):
        return [(pid, text, locale) for brand, pid, text, locale in self._prompts if brand == brand_name]

    def runs_for_prompt(self, prompt_id, surface=None, model=None):
        return [r for r in self._runs if r.prompt_id == prompt_id and r.surface == surface and r.model == model]

    def insert_surface_run(self, run):
        self.inserted.append(run)

    def insert_cache_reuse(self, **kw):
        self.reuses.append(kw)


def test_normalize_prompt_ignores_width_case_and_spacing():
    assert normalize_prompt("  中小团队用什么　CRM 好?") == normalize_prompt("中小团队用什么 crm 好?")
    assert normalize_prompt("Best CRM") != normalize_prompt("Best CRMs")


def test_cached_answers_follow_the_key_and_the_12_hour_window():
    prompts = [("极光CRM", "p-old", "中小团队用什么 CRM 好?", "zh-CN"),
               ("极光CRM", "p-en", "中小团队用什么 CRM 好?", "en-US"),
               ("别家", "p-other", "中小团队用什么 CRM 好?", "zh-CN"), ("极光CRM", "p-diff", "别的问题?", "zh-CN")]
    runs = [make_run("p-old", "fresh", hours_ago=2), make_run("p-old", "stale", hours_ago=CACHE_HOURS + 1),
            make_run("p-old", "failed", hours_ago=1, outcome=Outcome.TRANSIENT_FAILURE),
            make_run("p-old", "offline", hours_ago=1, mode=SearchMode.SEARCH_DISABLED),
            make_run("p-old", "newer", hours_ago=0.5), make_run("p-en", "english", hours_ago=1),
            make_run("p-other", "other-brand", hours_ago=1), make_run("p-diff", "other-question", hours_ago=1)]
    prompt = Prompt(text="中小团队用什么　crm 好?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER,
                    locale="zh-CN")
    hits = cached_answers(FakeStore(prompts, runs), job_id="me", brand_name="极光CRM", prompt=prompt, target=TARGET,
                          search_mode=SearchMode.DEFAULT, now=NOW)
    assert [r.run_id for r in hits] == ["newer", "fresh"]  # 新的在前;过期 / 失败 / 闭卷 / 英文 / 别家 / 别题都不算


def test_fill_from_cache_copies_into_empty_slots_with_zero_cost_and_records_reuse():
    prompts = [("极光CRM", "p-old", "中小团队用什么 CRM 好?", "zh-CN")]
    runs = [make_run("p-old", "a", hours_ago=3, text="甲"), make_run("p-old", "b", hours_ago=1, text="乙")]
    store = FakeStore(prompts, runs)
    prompt = Prompt(prompt_id="p-new", text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    copies = fill_from_cache(store, job_id="me", brand_name="极光CRM", prompt=prompt, target=TARGET,
                             search_mode=SearchMode.DEFAULT, missing_indices=[3, 1, 2], now=NOW)
    # 只有两条可用:新的先填小槽位,花费记 0
    assert [(c.run_index, c.answer.answer_text, c.cost_usd) for c in copies] == [(1, "乙", 0.0), (2, "甲", 0.0)]
    assert all(c.prompt_id == "p-new" and c.run_id not in ("a", "b") for c in copies)
    assert copies[0].raw_payload == {"k": "b"} and copies[0].timestamp == runs[1].timestamp  # 原文原样,时间照旧
    assert store.inserted == copies
    assert [(r["source_run_id"], r["cache_age_s"]) for r in store.reuses] == [("b", 3600), ("a", 3 * 3600)]
    assert fill_from_cache(store, job_id="me", brand_name="极光CRM", prompt=prompt, target=TARGET,
                           search_mode=SearchMode.DEFAULT, missing_indices=[], now=NOW) == []


def test_cache_reuse_table_is_append_only_and_counted_per_job(storage):
    run = make_run("p-cache", "cache-src-1")
    copy = run.model_copy(update={"run_id": "cache-copy-1", "prompt_id": "p-cache-2"})
    storage.insert_surface_run(run)
    storage.insert_surface_run(copy)
    storage.insert_cache_reuse(job_id="job-cache", run_id="cache-copy-1", source_run_id="cache-src-1", cache_age_s=60)
    assert storage.cache_reuse_count("job-cache") == 1 and storage.cache_reuse_count("job-none") == 0
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE cache_reuse SET cache_age_s = 0")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM cache_reuse")
