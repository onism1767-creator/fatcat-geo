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
    InformationNeed,
    NeedIntent,
    Outcome,
    PromptOrigin,
    PromptVariant,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from engine.storage import Storage

SERVER_DSN = "postgresql://geo@localhost:5433/postgres"
TEST_DSN = "postgresql://geo@localhost:5433/geo_tool_test"

BRAND = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])


@pytest.fixture(scope="session")
def storage():
    try:
        admin = psycopg.connect(SERVER_DSN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("本地 PostgreSQL 未运行:powershell scripts/pgdev.ps1 start")
    if not admin.execute("SELECT 1 FROM pg_database WHERE datname='geo_tool_test'").fetchone():
        admin.execute("CREATE DATABASE geo_tool_test")
    admin.close()

    cleaner = psycopg.connect(TEST_DSN, autocommit=True)
    for table in ("derived_observation", "recompute_event", "surface_run",
                  "prompt_variant", "information_need"):
        cleaner.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    cleaner.close()

    st = Storage(TEST_DSN)
    st.ensure_schema()
    yield st
    st.close()


def make_run(run_id: str, *, cited: bool = True) -> SurfaceRun:
    citations = (
        [Citation(url="https://aurora-crm.com/a", domain="aurora-crm.com", position=1)]
        if cited else []
    )
    return SurfaceRun(
        run_id=run_id,
        information_need_id="need-s1",
        prompt_variant_id="variant-s1",
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


def test_surface_run_round_trip(storage):
    storage.insert_information_need(InformationNeed(need_id="need-s1", statement="测试", intent=NeedIntent.DISCOVERY))
    storage.insert_prompt_variant(PromptVariant(
        variant_id="variant-s1", need_id="need-s1", prompt_text="问?", origin=PromptOrigin.USER,
    ))
    original = make_run("run-rt-1")
    storage.insert_surface_run(original)
    fetched = storage.runs_for_variant("variant-s1", surface="test_surface")
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
    fetched = {r.run_id: r for r in storage.runs_for_variant("variant-s1")}
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
    run_cited = make_run("run-obs-1")
    run_uncited = make_run("run-obs-2", cited=False)
    storage.insert_surface_run(run_cited)
    storage.insert_surface_run(run_uncited)

    detector = CitationDetector()
    written = observe_runs(storage, [run_cited, run_uncited], [detector], BRAND)
    assert written == 2
    obs = storage.observations_for_run("run-obs-1")
    assert len(obs) == 1
    assert obs[0]["observation_type"] == "citation"
    assert obs[0]["cited"] is True and obs[0]["positions"] == [1]
    assert obs[0]["detector_version"] == detector.version

    # 全量重算:新增观测行 + 审计事件,旧观测行保留
    recomputed = recompute_all(storage, detector, BRAND, note="测试重算")
    assert recomputed >= 2
    assert len(storage.observations_for_run("run-obs-1")) == 2  # 旧行未被覆盖
    event = storage._conn.execute(
        "SELECT detector_name, version_from, version_to, rows_affected, trigger_kind "
        "FROM recompute_event ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()
    assert event == (detector.name, detector.version, detector.version, recomputed, "manual")


def test_derived_observation_update_rejected(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE derived_observation SET detector_version = 99")


def test_recompute_event_delete_rejected(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM recompute_event")


def test_max_detector_version(storage):
    assert storage.max_detector_version("citation_domain_match") == CitationDetector().version
    assert storage.max_detector_version("不存在的判定器") is None
