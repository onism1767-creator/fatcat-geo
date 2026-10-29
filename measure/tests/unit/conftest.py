"""共享测试夹具:项目本地 PostgreSQL 的会话级测试库(geo_tool_test)。

会话开始时整库重建(DROP TABLE + ensure_schema);本地实例未运行则
相关测试整组跳过并提示。多个测试文件共用同一个夹具,避免互相清库。
"""

import psycopg
import pytest

from engine.storage import Storage

SERVER_DSN = "postgresql://geo@localhost:5433/postgres"
TEST_DSN = "postgresql://geo@localhost:5433/geo_tool_test"


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
    for table in ("citation_evidence", "page_check", "citation_resolution", "cache_reuse", "derived_observation",
                  "recompute_event", "report_snapshot", "aux_call", "surface_run", "prompt", "diagnosis_job"):
        cleaner.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    cleaner.close()

    st = Storage(TEST_DSN)
    st.ensure_schema()
    yield st
    st.close()


@pytest.fixture(autouse=True)
def _test_client_host(monkeypatch):
    """网页测试的 TestClient 发的主机名是 testserver;正式运行只认本机两个名字(report/app.py 的 ALLOWED_HOSTS,
    第二轮审核 24)。测试期间补上 testserver,别的照旧拒绝——tests/unit/test_web_guards.py 用真主机名验证白名单本身。"""
    import report.app

    monkeypatch.setattr(report.app, "ALLOWED_HOSTS", (*report.app.ALLOWED_HOSTS, "testserver"))
