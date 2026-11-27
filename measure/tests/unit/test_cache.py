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
    def __init__(self, prompts, runs, copies=()):
        self._prompts, self._runs = prompts, runs
        self._copies = set(copies)  # 库里已有的「从缓存复制来的」运行
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

    def cache_copies(self, run_ids):
        copied = self._copies | {r["run_id"] for r in self.reuses}
        return {rid for rid in run_ids if rid in copied}

    def cache_sources(self, job_id):
        return {r["source_run_id"] for r in self.reuses if r["job_id"] == job_id}


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
                          search_mode=SearchMode.DEFAULT, now=NOW, region=None)
    assert [r.run_id for r in hits] == ["newer", "fresh"]  # 新的在前;过期 / 失败 / 闭卷 / 英文 / 别家 / 别题都不算


def test_fill_from_cache_copies_into_empty_slots_with_zero_cost_and_records_reuse():
    prompts = [("极光CRM", "p-old", "中小团队用什么 CRM 好?", "zh-CN")]
    runs = [make_run("p-old", "a", hours_ago=3, text="甲"), make_run("p-old", "b", hours_ago=1, text="乙")]
    store = FakeStore(prompts, runs)
    prompt = Prompt(prompt_id="p-new", text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    copies = fill_from_cache(store, job_id="me", brand_name="极光CRM", prompt=prompt, target=TARGET,
                             search_mode=SearchMode.DEFAULT, missing_indices=[3, 1, 2], now=NOW, region=None)
    # 只有两条可用:新的先填小槽位,花费记 0
    assert [(c.run_index, c.answer.answer_text, c.cost_usd) for c in copies] == [(1, "乙", 0.0), (2, "甲", 0.0)]
    assert all(c.prompt_id == "p-new" and c.run_id not in ("a", "b") for c in copies)
    assert copies[0].raw_payload == {"k": "b"} and copies[0].timestamp == runs[1].timestamp  # 原文原样,时间照旧
    assert store.inserted == copies
    assert [(r["source_run_id"], r["cache_age_s"]) for r in store.reuses] == [("b", 3600), ("a", 3 * 3600)]
    assert fill_from_cache(store, job_id="me", brand_name="极光CRM", prompt=prompt, target=TARGET,
                           search_mode=SearchMode.DEFAULT, missing_indices=[], now=NOW, region=None) == []


def test_copies_made_from_cache_are_not_served_again_as_a_second_answer():
    """单 B 从缓存复制了单 A 的回答(时间戳、原文照旧);单 C 再找缓存只拿原件——
    原件和复制件不能当成两条、填进同一道题的两个槽位(审核 2026-10-27 黄 6)。"""
    prompts = [("极光CRM", "p-a", "中小团队用什么 CRM 好?", "zh-CN"),
               ("极光CRM", "p-b", "中小团队用什么 CRM 好?", "zh-CN")]
    original = make_run("p-a", "orig", hours_ago=2)
    copy = original.model_copy(update={"run_id": "copy", "prompt_id": "p-b"})
    store = FakeStore(prompts, [original, copy], copies={"copy"})
    prompt = Prompt(prompt_id="p-c", text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    hits = cached_answers(store, job_id="c", brand_name="极光CRM", prompt=prompt, target=TARGET,
                          search_mode=SearchMode.DEFAULT, now=NOW, region=None)
    assert [r.run_id for r in hits] == ["orig"]
    filled = fill_from_cache(store, job_id="c", brand_name="极光CRM", prompt=prompt, target=TARGET,
                             search_mode=SearchMode.DEFAULT, missing_indices=[1, 2], now=NOW, region=None)
    assert [(c.run_index, c.raw_payload) for c in filled] == [(1, {"k": "orig"})]  # 第二个槽位空着,等真提问


def test_resuming_does_not_copy_the_same_cached_answer_into_a_second_slot():
    """第四轮审核(两位审核员各自查到):一单开场从缓存复用了 3 条、还差 2 条没问就停了(到上限 / 叫停 / 中断);
    接着跑时原来又把那 3 条里的 2 条填进剩下的槽位——同一道题里同一条回答算两次,该真问的 2 次再也不问。
    现在本单用过的原件不再给;别的单照样能用。"""
    prompts = [("极光CRM", "p-a", "中小团队用什么 CRM 好?", "zh-CN")]
    runs = [make_run("p-a", f"a{i}", hours_ago=i, text=f"回答{i}") for i in (1, 2, 3)]
    store = FakeStore(prompts, runs)
    prompt = Prompt(prompt_id="p-b", text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    common = dict(brand_name="极光CRM", prompt=prompt, target=TARGET, search_mode=SearchMode.DEFAULT, now=NOW,
                  region=None)
    first = fill_from_cache(store, job_id="b", missing_indices=[1, 2, 3, 4, 5], **common)
    assert [c.run_index for c in first] == [1, 2, 3]  # 只有 3 条可用:4、5 等着真问
    again = fill_from_cache(store, job_id="b", missing_indices=[4, 5], **common)  # 接着跑
    assert again == [] and len(store.inserted) == 3
    assert [r.run_id for r in cached_answers(store, job_id="c", **common)] == ["a1", "a2", "a3"]  # 别的单不受影响


def test_cache_reuse_table_is_append_only_and_counted_per_job(storage):
    run = make_run("p-cache", "cache-src-1")
    copy = run.model_copy(update={"run_id": "cache-copy-1", "prompt_id": "p-cache-2"})
    storage.insert_surface_run(run)
    storage.insert_surface_run(copy)
    storage.insert_cache_reuse(job_id="job-cache", run_id="cache-copy-1", source_run_id="cache-src-1", cache_age_s=60)
    assert storage.cache_reuse_count("job-cache") == 1 and storage.cache_reuse_count("job-none") == 0
    assert storage.cache_copies(["cache-copy-1", "cache-src-1", "nope"]) == {"cache-copy-1"}  # 复制件认得出来
    assert storage.cache_copies([]) == set()
    # 这一单用过哪些原件(接着跑时不再拿同一条填第二个槽位)
    assert storage.cache_sources("job-cache") == {"cache-src-1"} and storage.cache_sources("job-none") == set()
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE cache_reuse SET cache_age_s = 0")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM cache_reuse")


def test_region_is_part_of_the_cache_key():
    """地区是键的一部分(用户 2026-10-28 定):指定了国家的回答不给没指定的用,反过来也不行;
    今天调度不指定地区,None 对 None 照旧复用。"""
    prompts = [("极光CRM", "p-a", "中小团队用什么 CRM 好?", "zh-CN")]
    plain = make_run("p-a", "plain", hours_ago=1)
    japan = make_run("p-a", "japan", hours_ago=1).model_copy(update={"region": "JP"})
    store = FakeStore(prompts, [plain, japan])
    prompt = Prompt(prompt_id="p-c", text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    common = dict(job_id="c", brand_name="极光CRM", prompt=prompt, target=TARGET, search_mode=SearchMode.DEFAULT,
                  now=NOW)
    assert [r.run_id for r in cached_answers(store, region=None, **common)] == ["plain"]
    assert [r.run_id for r in cached_answers(store, region="JP", **common)] == ["japan"]
    assert cached_answers(store, region="US", **common) == []


def test_the_region_must_be_passed_explicitly():
    """地区是缓存键的一部分,必须明说(第二轮审核 B8):原来默认空,以后采样钉了地区、缓存这边忘传,就会把没钉地区的回答
    填进钉了地区的单。现在忘传直接报错。"""
    import inspect

    import pytest as _pytest

    for fn in (cached_answers, fill_from_cache):
        assert inspect.signature(fn).parameters["region"].default is inspect.Parameter.empty
    with _pytest.raises(TypeError, match="region"):
        cached_answers(None, job_id="x", brand_name="b", prompt=None, target=TARGET,  # type: ignore[arg-type]
                       search_mode=SearchMode.DEFAULT, now=NOW)
