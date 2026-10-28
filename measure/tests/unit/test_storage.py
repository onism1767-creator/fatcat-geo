"""存储不变式测试(12.1):只追加必须由数据库拒绝,不靠代码纪律。

需要本地项目实例在运行(powershell scripts/pgdev.ps1 start),
否则整组跳过并提示。测试使用独立数据库 geo_tool_test,不碰开发库。
"""

from datetime import UTC, datetime

import psycopg
import pytest

from engine.detectors.citation import CitationDetector
from engine.detectors.runner import observe_runs, recompute_all
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
    run_cited = make_run("run-obs-1")
    run_uncited = make_run("run-obs-2", cited=False)
    other_brand_run = make_run("run-obs-other").model_copy(update={"prompt_id": "need-other-brand"})
    for run in (run_cited, run_uncited, other_brand_run):
        storage.insert_surface_run(run)
    storage.upsert_job_snapshot({  # 品牌只在任务快照里:全量重算按它找这个品牌的题与运行
        "job_id": "job-obs", "created_at": "2026-08-20T12:00:00+00:00", "state": "DONE", "fingerprint": "obs",
        "brand": {"name": brand.name}, "prompts": [{"prompt_id": "need-s1", "text": "q", "locale": "zh-CN"}],
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
    # 别的品牌的运行不在重算范围(审核 2026-10-27 黄 8)
    recomputed = recompute_all(storage, detector, brand, note="测试重算")
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
