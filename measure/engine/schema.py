"""L2 库表结构(storage 用):全部建表语句与只追加触发器,幂等可重复执行(v1 单机,无迁移框架)。

不可变性不靠代码纪律,靠数据库拒绝(13.5):只追加的表一律挂 reject_mutation 触发器,
UPDATE / DELETE / TRUNCATE 被 RAISE EXCEPTION 拒绝。
"""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS prompt (
        prompt_id text PRIMARY KEY,
        text text NOT NULL,
        intent text NOT NULL,
        origin text NOT NULL,
        locale text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS surface_run (
        run_id text PRIMARY KEY,
        prompt_id text NOT NULL,
        run_index integer NOT NULL,
        provider text NOT NULL,
        surface text NOT NULL,
        model text NOT NULL,
        search_mode text NOT NULL,
        locale text NOT NULL,
        region text,
        outcome text NOT NULL,
        error jsonb,
        search_trace jsonb NOT NULL,
        answer jsonb,
        raw_payload jsonb,
        run_timestamp timestamptz NOT NULL,
        latency_ms integer NOT NULL,
        cost_usd double precision NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS surface_run_prompt_idx ON surface_run (prompt_id, surface, model)",
    """
    CREATE TABLE IF NOT EXISTS derived_observation (
        observation_id text PRIMARY KEY,
        run_id text NOT NULL REFERENCES surface_run(run_id),
        observation_type text NOT NULL,
        detector_name text NOT NULL,
        detector_version integer NOT NULL,
        computed_at timestamptz NOT NULL,
        payload jsonb NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS derived_obs_run_idx ON derived_observation (run_id)",
    "CREATE INDEX IF NOT EXISTS derived_obs_detector_idx ON derived_observation (detector_name, detector_version)",
    # exactly-once(v0.4.9):同 (run, 判定器, 版本) 至多一条观测——幂等续跑重复评分
    # 不产生重复行;判定器升级以新版本追加,不受影响
    """
    CREATE UNIQUE INDEX IF NOT EXISTS derived_obs_exactly_once_idx
        ON derived_observation (run_id, detector_name, detector_version)
    """,
    # 任务快照(v0.4.9,16.3 修订):任务是运行状态而非观测,允许覆写;
    # 观测四表的只追加铁律不受影响
    """
    CREATE TABLE IF NOT EXISTS diagnosis_job (
        job_id text PRIMARY KEY,
        created_at timestamptz NOT NULL,
        state text NOT NULL,
        fingerprint text NOT NULL,
        snapshot jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS page_check (
        check_id text PRIMARY KEY,
        url text NOT NULL,
        normalized_url text NOT NULL,
        fetched_at timestamptz NOT NULL,
        status text NOT NULL,
        http_status integer,
        final_url text,
        title text,
        text text,
        text_source text,
        text_chars integer,
        truncated boolean NOT NULL,
        error text,
        fetcher_version integer NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS page_check_url_idx ON page_check (normalized_url, fetched_at DESC)",
    # 引用链接还原跳转(v0.4.26 S4,C2):原链接 → 终点链接、跳数、状态、还原器版本;只追加,按链接复用
    """
    CREATE TABLE IF NOT EXISTS citation_resolution (
        resolution_id text PRIMARY KEY,
        url text NOT NULL,
        final_url text,
        hops integer NOT NULL,
        status text NOT NULL,
        http_status integer,
        error text,
        resolver_version integer NOT NULL,
        resolved_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS citation_resolution_url_idx ON citation_resolution (url, resolved_at DESC)",
    # 触发器函数必须先于全部触发器语句创建(全新库依赖此顺序)
    """
    CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only (iron rule 8): % rejected', TG_TABLE_NAME, TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE TRIGGER page_check_append_only
        BEFORE UPDATE OR DELETE ON page_check
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER page_check_no_truncate
        BEFORE TRUNCATE ON page_check
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE TABLE IF NOT EXISTS recompute_event (
        event_id text PRIMARY KEY,
        occurred_at timestamptz NOT NULL,
        detector_name text NOT NULL,
        version_from integer,
        version_to integer NOT NULL,
        rows_affected integer NOT NULL,
        trigger_kind text NOT NULL,
        note text
    )
    """,
    """
    CREATE OR REPLACE TRIGGER surface_run_append_only
        BEFORE UPDATE OR DELETE ON surface_run
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER surface_run_no_truncate
        BEFORE TRUNCATE ON surface_run
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER derived_observation_append_only
        BEFORE UPDATE OR DELETE ON derived_observation
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER derived_observation_no_truncate
        BEFORE TRUNCATE ON derived_observation
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER recompute_event_append_only
        BEFORE UPDATE OR DELETE ON recompute_event
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER recompute_event_no_truncate
        BEFORE TRUNCATE ON recompute_event
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER citation_resolution_append_only
        BEFORE UPDATE OR DELETE ON citation_resolution
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER citation_resolution_no_truncate
        BEFORE TRUNCATE ON citation_resolution
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    # 辅助调用账本(2026-09-29,用户定「专门一张账本」):出题、改写问法、以后的 AI 裁判——
    # 每次尝试(含失败)一行,原样存整条 SurfaceRun。不是观测采样,不进任何指标分母;
    # 任务花费 = 采样 + 本表。只追加。
    """
    CREATE TABLE IF NOT EXISTS aux_call (
        call_id text PRIMARY KEY,
        job_id text NOT NULL,
        task text NOT NULL,
        created_at timestamptz NOT NULL,
        cost_usd double precision NOT NULL,
        outcome text NOT NULL,
        run jsonb NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS aux_call_job_idx ON aux_call (job_id)",
    """
    CREATE OR REPLACE TRIGGER aux_call_append_only
        BEFORE UPDATE OR DELETE ON aux_call
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER aux_call_no_truncate
        BEFORE TRUNCATE ON aux_call
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
    # 报告快照(v0.4.16):派生层的档案——每次生成报告追加一版,永不改写;
    # payload 对 L2 不透明,结构由 L4 report/jobs.py 定义。原始层仍是唯一底账。
    """
    CREATE TABLE IF NOT EXISTS report_snapshot (
        snapshot_id text PRIMARY KEY,
        job_id text NOT NULL,
        created_at timestamptz NOT NULL,
        fingerprint text NOT NULL,
        code_version text,
        spent_usd double precision NOT NULL,
        payload jsonb NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS report_snapshot_job_idx ON report_snapshot (job_id, created_at DESC)",
    """
    CREATE OR REPLACE TRIGGER report_snapshot_append_only
        BEFORE UPDATE OR DELETE ON report_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER report_snapshot_no_truncate
        BEFORE TRUNCATE ON report_snapshot
        FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()
    """,
)
