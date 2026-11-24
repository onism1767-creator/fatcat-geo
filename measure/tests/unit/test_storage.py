"""存储不变式测试(12.1):只追加必须由数据库拒绝,不靠代码纪律。

需要本地项目实例在运行(powershell scripts/pgdev.ps1 start),
否则整组跳过并提示。测试使用独立数据库 geo_tool_test,不碰开发库。
"""

from datetime import UTC, datetime

import psycopg
import pytest

from engine.detectors.citation import CitationDetector
from engine.detectors.runner import observe_runs, recompute_by_job
from engine.models import (
    Answer,
    BrandSpec,
    Citation,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from report.response_view import latest_observation  # 读的一方:同版本多行取最新

BRAND = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
# storage 夹具:tests/unit/conftest.py(会话级共享测试库)


def make_run(run_id: str, *, cited: bool = True) -> SurfaceRun:
    citations = (
        [Citation(url="https://aurora-crm.com/a", domain="aurora-crm.com", position=1)]
        if cited else []
    )
    return SurfaceRun(
        run_id=run_id,
        prompt_id="need-s1",
        run_index=1,
        provider="test_provider",
        surface="test_surface",
        model="m1",
        search_mode=SearchMode.DEFAULT,
        locale="zh-CN",
        region=None,
        outcome=Outcome.SUCCESS,
        search_trace=SearchTrace(search_triggered=True),
        answer=Answer(answer_text="推荐极光CRM。", citations_available=True, citations=citations),
        raw_payload={"answer": "推荐极光CRM。", "n": 1},
        timestamp=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        latency_ms=1200,
        cost_usd=0.0056,
    )


def test_schema_is_idempotent(storage):
    storage.ensure_schema()  # 重复执行不报错


def test_unreachable_database_fails_fast_with_storage_unavailable():
    """v0.4.22:连不上库要在有限时间内报 StorageUnavailable,不再无声挂起(2026-09-25 事故)。"""
    import time

    from engine.storage import Storage, StorageUnavailable

    t0 = time.perf_counter()
    with pytest.raises(StorageUnavailable, match="连不上数据库"):
        Storage("postgresql://geo@127.0.0.1:1/nowhere")  # 端口 1 没人听:立刻拒绝
    assert time.perf_counter() - t0 < 15


def test_job_snapshot_upsert_load_and_overwrite(storage):
    """任务快照(v0.4.9):运行状态可覆写——与观测四表的只追加互不相干。"""
    snapshot = {"job_id": "job-s1", "created_at": "2026-09-08T10:00:00+00:00",
                "state": "CONFIRMED", "fingerprint": "abcd1234", "extra": {"nested": 1}}
    storage.upsert_job_snapshot(snapshot)
    storage.upsert_job_snapshot(dict(snapshot, state="INTERRUPTED"))
    loaded = [s for s in storage.load_job_snapshots() if s["job_id"] == "job-s1"]
    assert len(loaded) == 1  # 覆写不是追加
    assert loaded[0]["state"] == "INTERRUPTED"
    assert loaded[0]["extra"] == {"nested": 1}  # 快照对 L2 不透明,原样往返


def test_call_stats_average_cost_and_latency(storage):
    """预估口径(v0.4.17):按 surface × 模式的历史均价与均时,来自真实运行档案。"""
    storage.insert_surface_run(make_run("run-stat-1"))
    storage.insert_surface_run(make_run("run-stat-2", cited=False))
    stat = next(s for s in storage.call_stats() if s.surface == "test_surface" and s.search_mode == "default")
    assert stat.n >= 2 and stat.avg_cost_usd > 0 and stat.avg_latency_ms > 0


def test_models_of_one_surface_are_kept_apart(storage):
    """v0.4.19 型号是口径的一部分:同一 surface 的两个型号,取样本与算均价都必须分开——
    否则续跑会拿甲型号的样本给乙型号当前缀,预估会把贵型号和便宜型号搅成一个价。"""
    for run_id, model, cost in (("run-mm-a1", "model-a", 0.01), ("run-mm-a2", "model-a", 0.03),
                                ("run-mm-b1", "model-b", 0.50)):
        storage.insert_surface_run(make_run(run_id).model_copy(update={
            "prompt_id": "prompt-mm",
            "surface": "surface-mm", "model": model, "cost_usd": cost,
        }))
    by_model = {
        model: {r.run_id for r in storage.runs_for_prompt("prompt-mm", surface="surface-mm", model=model)}
        for model in ("model-a", "model-b")
    }
    assert by_model == {"model-a": {"run-mm-a1", "run-mm-a2"}, "model-b": {"run-mm-b1"}}
    assert len(storage.runs_for_prompt("prompt-mm", surface="surface-mm")) == 3  # 不给型号 = 不筛
    stats = {s.model: s for s in storage.call_stats() if s.surface == "surface-mm"}
    assert stats["model-a"].n == 2 and stats["model-a"].avg_cost_usd == pytest.approx(0.02)
    assert stats["model-b"].n == 1 and stats["model-b"].avg_cost_usd == pytest.approx(0.50)


def test_call_stats_keep_languages_apart_and_average_the_usage(storage):
    """成本调研②:统计按语言分桶,平均用量来自派生表 call_usage;从别的单复制来的回答花费记 0,不算;
    prompt_ids 只看这些题下的运行(同品牌上一单优先)。"""
    for run_id, locale, cost, usage in (("run-u-zh1", "zh-CN", 0.02, {"input_tokens": 100.0, "searches": 1.0}),
                                        ("run-u-zh2", "zh-CN", 0.04, {"input_tokens": 300.0, "searches": 2.0}),
                                        ("run-u-en1", "en-US", 0.01, {"input_tokens": 50.0, "searches": 0.0})):
        storage.insert_surface_run(make_run(run_id).model_copy(update={
            "prompt_id": "prompt-u", "surface": "surface-u", "locale": locale, "cost_usd": cost}))
        storage.insert_call_usage(run_id, usage)
    stats = {s.locale: s for s in storage.call_stats() if s.surface == "surface-u"}
    assert stats["zh-CN"].n == 2 and stats["zh-CN"].n_usage == 2
    assert stats["zh-CN"].usage == {"input_tokens": 200.0, "searches": 1.5}
    assert stats["zh-CN"].usage_high == {} and stats["zh-CN"].n_high == 0  # 不足 10 次分不出「最贵的一成」
    assert stats["zh-CN"].avg_cost_usd == pytest.approx(0.03)
    assert stats["en-US"].usage == {"input_tokens": 50.0, "searches": 0.0}
    assert storage.call_stats(prompt_ids=["no-such-prompt"]) == []
    assert {s.locale for s in storage.call_stats(prompt_ids=["prompt-u"])} == {"zh-CN", "en-US"}
    # 排除的题不算(用户 2026-11-20 定:校准题不当实测);别的题照常
    assert not [s for s in storage.call_stats(exclude_prompt_ids=["prompt-u"]) if s.surface == "surface-u"]
    kept = [s for s in storage.call_stats(exclude_prompt_ids=["some-other-prompt"]) if s.surface == "surface-u"]
    assert {s.locale: s.n for s in kept} == {"zh-CN": 2, "en-US": 1}
    storage.insert_surface_run(make_run("run-u-copy").model_copy(update={
        "prompt_id": "prompt-u", "surface": "surface-u", "locale": "en-US", "cost_usd": 0.0}))
    storage.insert_cache_reuse(job_id="job-u", run_id="run-u-copy", source_run_id="run-u-en1", cache_age_s=60)
    stats = {s.locale: s for s in storage.call_stats() if s.surface == "surface-u"}
    assert stats["en-US"].n == 1 and stats["en-US"].avg_cost_usd == pytest.approx(0.01)
    # 用量可以重算覆盖;补老数据只找还没有记录的
    storage.insert_call_usage("run-u-zh1", {"input_tokens": 500.0, "searches": 1.0})
    zh = next(s for s in storage.call_stats() if s.surface == "surface-u" and s.locale == "zh-CN")
    assert zh.usage["input_tokens"] == 400.0
    missing = storage.run_ids_without_usage()
    assert "run-u-zh1" not in missing and "run-u-copy" in missing
    redo = storage.success_run_ids("surface-u")  # 记账规则改了整台重算(Q8):成功的都列,有没有用量记录都算
    assert {"run-u-zh1", "run-u-copy"} <= set(redo) and "run-stat-1" not in redo


def test_call_stats_average_the_priciest_tenth_separately(storage):
    """确认页「贵的时候约 $N」与续跑弹窗的建议值(用户 2026-11-03 定 Q1 乙 / Q7 乙):每桶按记的费用十等分,
    最后一档的平均用量单独给;20 次里最贵的 2 次就是「最贵的一成」。"""
    for i in range(1, 21):
        run_id = f"run-h-{i}"
        storage.insert_surface_run(make_run(run_id).model_copy(update={
            "prompt_id": "prompt-h", "surface": "surface-h", "cost_usd": i / 100}))
        storage.insert_call_usage(run_id, {"input_tokens": float(100 * i), "searches": float(i % 2)})
    st = next(s for s in storage.call_stats() if s.surface == "surface-h")
    assert st.n == 20 and st.n_usage == 20 and st.usage == {"input_tokens": 1050.0, "searches": 0.5}
    assert st.n_high == 2 and st.usage_high == {"input_tokens": 1950.0, "searches": 0.5}  # 第 19、20 次


def test_brand_prompt_ids_cover_every_job_of_that_brand(storage):
    # 测试库是整个会话共用的,别的测试也建「极光CRM」的单,这里用自己的品牌名
    for job_id, brand, prompts in (("job-b1", "牌子甲", ["p-b1"]), ("job-b2", "牌子甲", ["p-b2", "p-b3"]),
                                   ("job-b3", "牌子乙", ["p-x"])):
        storage.upsert_job_snapshot({"job_id": job_id, "created_at": "2026-11-03T10:00:00+00:00", "state": "DONE",
                                     "fingerprint": "f", "brand": {"name": brand},
                                     "prompts": [{"prompt_id": p} for p in prompts]})
    assert sorted(storage.brand_prompt_ids("牌子甲")) == ["p-b1", "p-b2", "p-b3"]
    assert storage.brand_prompt_ids("没有的") == []


def test_report_snapshot_is_versioned_and_append_only(storage):
    """报告快照(v0.4.16):同一任务多版并存,latest 取最新并报版本数;库层拒绝改写。"""
    assert storage.latest_report_snapshot("job-r1") is None
    storage.insert_report_snapshot(job_id="job-r1", fingerprint="f1", code_version="abc1234",
                                   spent_usd=1.5, payload={"class_reports": [], "v": 1})
    storage.insert_report_snapshot(job_id="job-r1", fingerprint="f1", code_version=None,
                                   spent_usd=1.5, payload={"class_reports": [], "v": 2})
    latest = storage.latest_report_snapshot("job-r1")
    assert latest["payload"]["v"] == 2 and latest["versions"] == 2 and latest["spent_usd"] == 1.5
    assert latest["code_version"] is None  # 拿不到程序版本就如实为空,不编
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE report_snapshot SET spent_usd = 0")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM report_snapshot")


def test_aux_call_ledger_sums_per_job_and_is_append_only(storage):
    """辅助调用账本(2026-09-29):每次尝试一行、整条 SurfaceRun 原样存;任务花费按任务汇总;库层拒绝改写。"""
    assert storage.aux_spent_usd("job-aux") == 0.0
    def costing(run_id: str, cost: float) -> SurfaceRun:
        return make_run(run_id).model_copy(update={"cost_usd": cost})

    storage.insert_aux_call(job_id="job-aux", task="出题", run=costing("aux-1", 0.01))
    storage.insert_aux_call(job_id="job-aux", task="猜品类", run=costing("aux-2", 0.02))
    storage.insert_aux_call(job_id="job-other", task="出题", run=costing("aux-3", 5.0))
    assert storage.aux_spent_usd("job-aux") == pytest.approx(0.03)
    archived = storage._conn.execute("SELECT run FROM aux_call WHERE call_id = 'aux-1'").fetchone()[0]
    assert SurfaceRun.model_validate(archived).run_id == "aux-1"  # 原样存档,可复原
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE aux_call SET cost_usd = 0")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM aux_call")


def test_observation_exactly_once_per_run_detector_version(storage):
    """幂等续跑重复评分不产生重复观测行(同 run × 判定器 × 版本至多一条)。"""
    run = make_run("run-once-1")
    storage.insert_surface_run(run)
    observe_runs(storage, [run], [CitationDetector()], BRAND)
    observe_runs(storage, [run], [CitationDetector()], BRAND)  # 续跑重复评分
    assert len(storage.observations_for_run("run-once-1")) == 1


def test_insert_prompt_is_idempotent(storage):
    prompt = Prompt(prompt_id="prompt-idem", text="原话?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN", created_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC))
    storage.insert_prompt(prompt)
    storage.insert_prompt(prompt)  # 续跑重复插入:静默跳过
    rows = storage._conn.execute(
        "SELECT text, intent, origin, locale FROM prompt WHERE prompt_id = %s", ("prompt-idem",)
    ).fetchall()
    assert rows == [("原话?", "category_discovery", "user", "zh-CN")]


def test_surface_run_round_trip(storage):
    original = make_run("run-rt-1")
    storage.insert_surface_run(original)
    fetched = storage.runs_for_prompt("need-s1", surface="test_surface")
    assert any(r == original for r in fetched)  # 逐字段等值往返


def test_nul_characters_are_stored_as_replacement_characters_instead_of_failing(storage):
    """第四轮审核:PostgreSQL 的 text 与 jsonb 都存不了空字符。原来原样写入整条报错——一条怪回答或怪网页
    (二进制内容被标成网页、标题里夹了 0x00)就让整单崩,钱花了、回答没存上,每次接着跑还再崩在同一条。"""
    from engine.models import PageCheck, PageFetchStatus

    odd = make_run("run-nul-1").model_copy(update={
        "prompt_id": "need-nul",
        "answer": Answer(answer_text="前\x00后", citations_available=True),
        "raw_payload": {"text": "a\x00b", "parts": ["\x00", {"k\x00": 1}]},
    })
    storage.insert_surface_run(odd)
    [stored] = storage.runs_for_prompt("need-nul", surface="test_surface")
    assert stored.answer.answer_text == "前\ufffd后"
    assert stored.raw_payload == {"text": "a\ufffdb", "parts": ["\ufffd", {"k\ufffd": 1}]}
    page = PageCheck(url="https://nul.test/a", normalized_url="https://nul.test/a", status=PageFetchStatus.OK,
                     title="标题\x00", text="正文\x00正文", error="x\x00", fetcher_version=1)
    storage.insert_page_check(page)
    kept = storage.recent_page_checks(["https://nul.test/a"])["https://nul.test/a"]
    assert (kept.title, kept.text, kept.error) == ("标题\ufffd", "正文\ufffd正文", "x\ufffd")


def test_failure_run_round_trip(storage):
    failure = make_run("run-rt-2").model_copy(update={
        "outcome": Outcome.TRANSIENT_FAILURE,
        "error": RunError(error_type=RunErrorType.TRANSIENT, message="限流",
                          http_status=429, attempts=3),
        "answer": None,
        "raw_payload": None,
        "cost_usd": 0.0,
    })
    storage.insert_surface_run(failure)
    fetched = {r.run_id: r for r in storage.runs_for_prompt("need-s1")}
    assert fetched["run-rt-2"] == failure


def test_surface_run_update_rejected_by_database(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE surface_run SET model = 'tampered' WHERE run_id = 'run-rt-1'")


def test_surface_run_delete_rejected_by_database(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM surface_run WHERE run_id = 'run-rt-1'")


def test_surface_run_truncate_rejected_by_database(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("TRUNCATE surface_run CASCADE")


def test_observations_and_recompute_audit(storage):
    brand = BrandSpec(name="极光CRM-重算", domains=["aurora-crm.com"])  # 独立品牌名:共享测试库里别的快照不掺和
    # 本测试的题号独占:共享测试库里别的测试也用默认题号 need-s1,全量重算按品牌找题,混进来就数不准
    run_cited = make_run("run-obs-1").model_copy(update={"prompt_id": "need-obs"})
    run_uncited = make_run("run-obs-2", cited=False).model_copy(update={"prompt_id": "need-obs"})
    other_brand_run = make_run("run-obs-other").model_copy(update={"prompt_id": "need-other-brand"})
    for run in (run_cited, run_uncited, other_brand_run):
        storage.insert_surface_run(run)
    storage.upsert_job_snapshot({  # 品牌只在任务快照里:全量重算按它找这个品牌的题与运行
        "job_id": "job-obs", "created_at": "2026-08-20T12:00:00+00:00", "state": "DONE", "fingerprint": "obs",
        "brand": {"name": brand.name}, "prompts": [{"prompt_id": "need-obs", "text": "q", "locale": "zh-CN"}],
    })

    detector = CitationDetector()
    written = observe_runs(storage, [run_cited, run_uncited], [detector], brand)
    assert written == 2
    obs = storage.observations_for_run("run-obs-1")
    assert len(obs) == 1
    assert obs[0]["observation_type"] == "citation"
    assert obs[0]["cited"] is True and obs[0]["positions"] == [1]
    assert obs[0]["detector_version"] == detector.version

    # 同版本、同判据重算:同样的判定不重复写(v0.4.9 幂等续跑);审计事件照落,rows_affected 是实际写入数;
    # 按单重算:这单的回答用这单冻结的品牌卡判,别的单的运行不在这单里(第二轮审核 B7;原来按品牌名取,黄 8)
    recomputed = recompute_by_job(storage, detector, job_ids=["job-obs"], note="测试重算")
    assert recomputed == 0
    assert len(storage.observations_for_run("run-obs-1")) == 1
    assert storage.observations_for_run("run-obs-other") == []
    event = storage._conn.execute(
        "SELECT detector_name, version_from, version_to, rows_affected, trigger_kind "
        "FROM recompute_event ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()
    assert event == (detector.name, detector.version, detector.version, 0, "manual")

    # 判据改了(官网名单换了)之后同版本重算:追加一行,旧行保留,读的一方取最新(审核 2026-10-27 黄 7)
    renamed = brand.model_copy(update={"domains": ["elsewhere.example"]})
    assert observe_runs(storage, [run_cited], [detector], renamed) == 1
    rows = storage.observations_for_run("run-obs-1")
    assert [r["cited"] for r in rows] == [True, False]  # 按算出时间序
    assert latest_observation(rows, detector.name)["cited"] is False
    assert observe_runs(storage, [run_cited], [detector], renamed) == 0  # 再算一遍一样:不写

    # 判定器升级重算:新版本追加,旧版本行保留(历史判定永不被覆盖)。
    # 用独立判定器名,避免污染共享测试库中真实判定器的版本序
    class UpgradedCitationDetector(CitationDetector):
        name = "citation_domain_match_upgrade_probe"
        version = CitationDetector.version + 1

    observe_runs(storage, [run_cited], [UpgradedCitationDetector()], brand)
    types_and_versions = sorted(
        {(o["detector_name"], o["detector_version"]) for o in storage.observations_for_run("run-obs-1")}
    )
    assert types_and_versions == [
        ("citation_domain_match", detector.version),
        ("citation_domain_match_upgrade_probe", detector.version + 1),
    ]


def test_derived_observation_update_rejected(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE derived_observation SET detector_version = 99")


def test_recompute_event_delete_rejected(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM recompute_event")


def test_max_detector_version(storage):
    assert storage.max_detector_version("citation_domain_match") == CitationDetector().version
    assert storage.max_detector_version("不存在的判定器") is None


def test_citation_resolution_is_append_only_and_latest_per_url(storage):
    """S4:跳转链还原记录只追加;同一链接多条取最新(调用方按状态与时效决定复不复用)。"""
    from engine.models import CitationResolution, ResolutionStatus

    older = CitationResolution(url="https://redir.test/a", hops=0, status=ResolutionStatus.FETCH_FAILED,
                               error="ConnectError", resolver_version=1,
                               resolved_at=datetime(2026, 10, 1, tzinfo=UTC))
    newer = CitationResolution(url="https://redir.test/a", final_url="https://brand.test/p", hops=2,
                               status=ResolutionStatus.RESOLVED, http_status=200, resolver_version=1,
                               resolved_at=datetime(2026, 10, 2, tzinfo=UTC))
    for row in (older, newer):
        storage.insert_citation_resolution(row)
    latest = storage.latest_citation_resolutions(["https://redir.test/a", "https://redir.test/never"])
    assert set(latest) == {"https://redir.test/a"}
    assert latest["https://redir.test/a"].final_url == "https://brand.test/p"
    assert latest["https://redir.test/a"].status is ResolutionStatus.RESOLVED
    assert storage.latest_citation_resolutions([]) == {}
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE citation_resolution SET final_url = NULL")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM citation_resolution")


def test_latest_row_is_the_last_written_even_when_the_timestamps_tie(storage):
    """「最新一行」只认库里的写入序号(第二轮审核 B5):判定行的算出时间由 Python 给,本机连取 20 万次只有 12 个不同值,
    两行时间一模一样时原来先后看运气(全量测试偶发失败就是它)。这里故意让两行时间相同。"""
    from engine.models import BrandSpec

    brand = BrandSpec(name="极光CRM-平局", domains=["aurora-crm.com"])
    run = make_run("run-tie-1").model_copy(update={"prompt_id": "need-tie"})
    storage.insert_surface_run(run)
    detector = CitationDetector()
    stamp = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    moved = brand.model_copy(update={"domains": ["elsewhere.example"]})
    first = detector.detect(run, brand).model_copy(update={"computed_at": stamp})
    second = detector.detect(run, moved).model_copy(update={"computed_at": stamp})
    assert storage.insert_observation(first) and storage.insert_observation(second)
    rows = storage.observations_for_run("run-tie-1")
    assert [r["cited"] for r in rows] == [True, False]  # 按写入先后
    assert latest_observation(rows, detector.name)["cited"] is False
    again = detector.detect(run, moved).model_copy(update={"computed_at": stamp})
    assert storage.insert_observation(again) is False  # 和最新一行一样:不重复写(比对的是写入序号上的最新一行)
    assert storage.observations_for_runs(["run-tie-1"])["run-tie-1"] == rows


def test_latest_observation_takes_the_highest_version_then_the_last_written():
    rows = [
        {"detector_name": "d", "detector_version": 2, "v": "old-v2"},
        {"detector_name": "d", "detector_version": 3, "v": "v3-first"},
        {"detector_name": "x", "detector_version": 9, "v": "other"},
        {"detector_name": "d", "detector_version": 3, "v": "v3-last"},
        {"detector_name": "d", "detector_version": 2, "v": "late-v2"},
    ]
    assert latest_observation(rows, "d")["v"] == "v3-last"
    assert latest_observation(rows, "missing") is None


def test_stored_judge_verdicts_take_the_latest_row_not_the_oldest():
    """装回裁判结论原来同版本多行时取到最旧那行(第二轮审核 B5)。"""
    from engine.detectors.brand_mention_judge import DETECTOR_NAME, SENTIMENT_DETECTOR_NAME
    from report.runner import _stored_verdicts

    class Rows:
        def observations_for_runs(self, run_ids):
            return {"r1": [
                {"detector_name": DETECTOR_NAME, "detector_version": 1, "mentioned": False, "confidence": 0.9},
                {"detector_name": SENTIMENT_DETECTOR_NAME, "detector_version": 1, "sentiment": "negative"},
                {"detector_name": DETECTOR_NAME, "detector_version": 1, "mentioned": True, "confidence": 0.8,
                 "matched_text": "AuroraCRM"},
                {"detector_name": SENTIMENT_DETECTOR_NAME, "detector_version": 1, "sentiment": "positive"},
            ]}

    verdict = _stored_verdicts(Rows(), [make_run("r1")])["r1"]
    assert verdict.mentioned is True and verdict.spans == ["AuroraCRM"] and verdict.sentiment.value == "positive"


def test_recompute_by_job_judges_each_job_with_its_own_frozen_brand_card(storage):
    """两单测同一个品牌,但冻结的官网名单不同:一单的回答只能拿它自己的品牌卡判(第二轮审核 B7)。
    原来拿一张品牌卡判这个品牌所有单的回答,会给别的单追加按错卡判的行。"""
    class ProbeDetector(CitationDetector):
        name = "citation_recompute_by_job_probe"  # 独立判定器名:共享测试库里不和真实判定器混

    run_a = make_run("run-bj-a").model_copy(update={"prompt_id": "need-bj-a"})
    run_b = make_run("run-bj-b").model_copy(update={"prompt_id": "need-bj-b"})
    for run in (run_a, run_b):
        storage.insert_surface_run(run)
    for job_id, prompt_id, domains in (("job-bj-a", "need-bj-a", ["aurora-crm.com"]),
                                       ("job-bj-b", "need-bj-b", ["elsewhere.example"])):
        storage.upsert_job_snapshot({
            "job_id": job_id, "created_at": "2026-10-29T12:00:00+00:00", "state": "DONE", "fingerprint": job_id,
            "brand": {"name": "极光CRM-按单", "domains": domains},
            "prompts": [{"prompt_id": prompt_id, "text": "q", "locale": "zh-CN"}],
        })
    written = recompute_by_job(storage, ProbeDetector(), job_ids=["job-bj-a", "job-bj-b"], note="按单")
    assert written == 2
    cited = {run_id: storage.observations_for_run(run_id)[-1]["cited"] for run_id in ("run-bj-a", "run-bj-b")}
    assert cited == {"run-bj-a": True, "run-bj-b": False}  # 各用各的官网名单


def test_version_first_seen_is_audited_once_and_a_missed_audit_is_filled_in(storage):
    """判定器版本第一次在库里落判定行记一条审计(第二轮审核 B6):同一版本再存不再记;上次存档中途断库、判定行写进去了
    审计没记上的,下一单补上(原来看见版本已在库里就永远不记了)。"""
    name = "citation_first_seen_probe"
    count = lambda: storage._conn.execute(  # noqa: E731
        "SELECT count(*) FROM recompute_event WHERE detector_name = %s", (name,)).fetchone()[0]
    assert storage.record_version_first_seen(detector_name=name, version=1, rows_affected=3, trigger_kind="job")
    assert not storage.record_version_first_seen(detector_name=name, version=1, rows_affected=2, trigger_kind="job")
    assert count() == 1
    # 模拟「判定行写进去了、审计没记上」:版本 2 的行先在库里,审计表里还没有它
    run = make_run("run-first-seen").model_copy(update={"prompt_id": "need-first-seen"})
    storage.insert_surface_run(run)

    class V2(CitationDetector):
        version = 2

    observation = V2().detect(run, BrandSpec(name="X", domains=["aurora-crm.com"]))
    storage.insert_observation(observation.model_copy(update={"detector_name": name}))
    assert storage.record_version_first_seen(detector_name=name, version=2, rows_affected=0, trigger_kind="job")
    event = storage._conn.execute(
        "SELECT version_from, version_to FROM recompute_event WHERE detector_name = %s AND version_to = 2", (name,)
    ).fetchone()
    assert event == (None, 2)  # 版本 1 只在审计里,库里没有版本 1 的判定行


def test_calibration_questions_are_known_by_id_so_estimates_can_leave_them_out():
    """估价不拿校准题的回答当实测(用户 2026-11-20 定「拿掉」):题号由品牌和题面算,和采集脚本存进库的是同一批。"""
    from engine.hard_cases import calibration_prompt_ids, load_real_brands, prompts_of

    ids = calibration_prompt_ids()
    assert ids == [p.prompt_id for brand in load_real_brands() for p in prompts_of(brand)]
    assert len(ids) == len(set(ids)) > 0
