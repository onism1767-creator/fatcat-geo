"""L2 存储:只追加的原始层与派生层(铁律 8、13.5)。

- surface_run / derived_observation / recompute_event 三表由数据库触发器
  强制只追加:UPDATE / DELETE / TRUNCATE 一律被 RAISE EXCEPTION 拒绝。
  不可变性不靠代码纪律,靠数据库拒绝(13.5);tests/unit/test_storage.py
  的不变式测试直接对真库验证。
- 判定器重算 = 以新 detector_version 追加新观测行,旧行永远保留;
  每次全量重算落一条 RecomputeEvent 审计(没有这张表,指标变化时
  分不清是世界变了还是判定器变了)。
- schema 以语句列表内联管理(v1 单机,无迁移框架,幂等可重复执行)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from engine.models import (
    Answer,
    CitationEvidence,
    DerivedObservation,
    InformationNeed,
    PromptVariant,
    RunError,
    SearchTrace,
    SurfaceRun,
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS information_need (
        need_id text PRIMARY KEY,
        statement text NOT NULL,
        intent text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prompt_variant (
        variant_id text PRIMARY KEY,
        need_id text NOT NULL REFERENCES information_need(need_id),
        prompt_text text NOT NULL,
        origin text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS surface_run (
        run_id text PRIMARY KEY,
        information_need_id text NOT NULL,
        prompt_variant_id text NOT NULL,
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
    "CREATE INDEX IF NOT EXISTS surface_run_variant_idx ON surface_run (prompt_variant_id, surface)",
    "CREATE INDEX IF NOT EXISTS surface_run_need_idx ON surface_run (information_need_id)",
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
    CREATE TABLE IF NOT EXISTS citation_evidence (
        evidence_id text PRIMARY KEY,
        url text NOT NULL,
        normalized_url text NOT NULL,
        fetched_at timestamptz NOT NULL,
        reachable boolean NOT NULL,
        http_status integer,
        final_url text,
        checked_brands jsonb NOT NULL,
        brands_found jsonb NOT NULL,
        visible_chars integer,
        error text
    )
    """,
    "CREATE INDEX IF NOT EXISTS citation_evidence_url_idx ON citation_evidence (normalized_url, fetched_at DESC)",
    # 触发器函数必须先于全部触发器语句创建(全新库依赖此顺序)
    """
    CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only (iron rule 8): % rejected', TG_TABLE_NAME, TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE TRIGGER citation_evidence_append_only
        BEFORE UPDATE OR DELETE ON citation_evidence
        FOR EACH ROW EXECUTE FUNCTION reject_mutation()
    """,
    """
    CREATE OR REPLACE TRIGGER citation_evidence_no_truncate
        BEFORE TRUNCATE ON citation_evidence
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

_RUN_COLUMNS = (
    "run_id, information_need_id, prompt_variant_id, provider, surface, model, "
    "search_mode, locale, region, outcome, error, search_trace, answer, raw_payload, "
    "run_timestamp, latency_ms, cost_usd"
)


def _observation_type(observation: DerivedObservation) -> str:
    name = type(observation).__name__.removesuffix("Observation")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class Storage:
    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)

    # ── 生命周期 ──────────────────────────────────────────────────────

    def ensure_schema(self) -> None:
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── 写入(只追加)──────────────────────────────────────────────────

    def insert_information_need(self, need: InformationNeed) -> None:
        self._conn.execute(
            "INSERT INTO information_need (need_id, statement, intent, created_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (need_id) DO NOTHING",
            (need.need_id, need.statement, need.intent.value, need.created_at),
        )

    def insert_prompt_variant(self, variant: PromptVariant) -> None:
        self._conn.execute(
            "INSERT INTO prompt_variant (variant_id, need_id, prompt_text, origin, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (variant_id) DO NOTHING",
            (variant.variant_id, variant.need_id, variant.prompt_text,
             variant.origin.value, variant.created_at),
        )

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self._conn.execute(
            f"INSERT INTO surface_run ({_RUN_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run.run_id,
                run.information_need_id,
                run.prompt_variant_id,
                run.provider,
                run.surface,
                run.model,
                run.search_mode.value,
                run.locale,
                run.region,
                run.outcome.value,
                Jsonb(run.error.model_dump(mode="json")) if run.error else None,
                Jsonb(run.search_trace.model_dump(mode="json")),
                Jsonb(run.answer.model_dump(mode="json")) if run.answer else None,
                Jsonb(run.raw_payload) if run.raw_payload is not None else None,
                run.timestamp,
                run.latency_ms,
                run.cost_usd,
            ),
        )

    def insert_observation(self, observation: DerivedObservation) -> None:
        """幂等写入:同 (run, 判定器, 版本) 已有观测时静默跳过(续跑重复评分不产生
        重复行)。注意 recompute 审计的 rows_affected 因此是"尝试数"口径。"""
        self._conn.execute(
            "INSERT INTO derived_observation (observation_id, run_id, observation_type, "
            "detector_name, detector_version, computed_at, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, detector_name, detector_version) DO NOTHING",
            (
                observation.observation_id,
                observation.run_id,
                _observation_type(observation),
                observation.detector_name,
                observation.detector_version,
                observation.computed_at,
                Jsonb(observation.model_dump(mode="json")),
            ),
        )

    def insert_citation_evidence(self, evidence: CitationEvidence) -> None:
        self._conn.execute(
            "INSERT INTO citation_evidence (evidence_id, url, normalized_url, fetched_at, "
            "reachable, http_status, final_url, checked_brands, brands_found, visible_chars, error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                evidence.evidence_id, evidence.url, evidence.normalized_url,
                evidence.fetched_at, evidence.reachable, evidence.http_status,
                evidence.final_url, Jsonb(evidence.checked_brands),
                Jsonb(evidence.brands_found), evidence.visible_chars, evidence.error,
            ),
        )

    def recent_citation_evidence(
        self, normalized_urls: list[str], *, max_age_days: int = 7
    ) -> dict[str, CitationEvidence]:
        """每个 URL 取时效窗口内最新一条核验记录(核验缓存,Potato 式按 URL 去重)。"""
        if not normalized_urls:
            return {}
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        rows = self._conn.execute(
            "SELECT DISTINCT ON (normalized_url) evidence_id, url, normalized_url, fetched_at, "
            "reachable, http_status, final_url, checked_brands, brands_found, visible_chars, error "
            "FROM citation_evidence WHERE normalized_url = ANY(%s) AND fetched_at > %s "
            "ORDER BY normalized_url, fetched_at DESC",
            (normalized_urls, cutoff),
        ).fetchall()
        return {
            row[2]: CitationEvidence(
                evidence_id=row[0], url=row[1], normalized_url=row[2], fetched_at=row[3],
                reachable=row[4], http_status=row[5], final_url=row[6],
                checked_brands=row[7], brands_found=row[8], visible_chars=row[9], error=row[10],
            )
            for row in rows
        }

    def insert_recompute_event(
        self,
        *,
        detector_name: str,
        version_from: int | None,
        version_to: int,
        rows_affected: int,
        trigger_kind: str,
        note: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO recompute_event (event_id, occurred_at, detector_name, "
            "version_from, version_to, rows_affected, trigger_kind, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid4()), datetime.now(UTC), detector_name,
             version_from, version_to, rows_affected, trigger_kind, note),
        )

    # ── 任务快照(v0.4.9 幂等续跑)─────────────────────────────────────

    def upsert_job_snapshot(self, snapshot: dict[str, Any]) -> None:
        """整快照覆写(任务是运行状态,非观测层)。snapshot 对 L2 是不透明 JSON,
        结构由 L4 report/jobs.py 定义,须含 job_id/created_at/state/fingerprint。"""
        self._conn.execute(
            "INSERT INTO diagnosis_job (job_id, created_at, state, fingerprint, snapshot) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id) DO UPDATE SET state = EXCLUDED.state, snapshot = EXCLUDED.snapshot",
            (
                snapshot["job_id"],
                datetime.fromisoformat(snapshot["created_at"]),
                snapshot["state"],
                snapshot["fingerprint"],
                Jsonb(snapshot),
            ),
        )

    def load_job_snapshots(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT snapshot FROM diagnosis_job ORDER BY created_at"
        ).fetchall()
        return [row[0] for row in rows]

    # ── 报告快照(v0.4.16,只追加)────────────────────────────────────────

    def insert_report_snapshot(
        self, *, job_id: str, fingerprint: str, code_version: str | None,
        spent_usd: float, payload: dict[str, Any],
    ) -> None:
        """追加一版报告。payload 对 L2 不透明(结构由 L4 定义)。"""
        self._conn.execute(
            "INSERT INTO report_snapshot "
            "(snapshot_id, job_id, created_at, fingerprint, code_version, spent_usd, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (str(uuid4()), job_id, datetime.now(UTC), fingerprint, code_version, spent_usd, Jsonb(payload)),
        )

    def latest_report_snapshot(self, job_id: str) -> dict[str, Any] | None:
        """该任务最新一版报告(含版本序号);没有则 None。"""
        row = self._conn.execute(
            "SELECT created_at, fingerprint, code_version, spent_usd, payload, count(*) OVER () "
            "FROM report_snapshot WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        created_at, fingerprint, code_version, spent_usd, payload, versions = row
        return {
            "created_at": created_at, "fingerprint": fingerprint, "code_version": code_version,
            "spent_usd": spent_usd, "payload": payload, "versions": versions,
        }

    def variants_for_need(self, need_id: str) -> list[PromptVariant]:
        """续跑复用已存问法(不重烧起草费)。按创建时间序:用户原话先于生成变体。"""
        rows = self._conn.execute(
            "SELECT variant_id, need_id, prompt_text, origin, created_at "
            "FROM prompt_variant WHERE need_id = %s ORDER BY created_at",
            (need_id,),
        ).fetchall()
        return [
            PromptVariant(variant_id=r[0], need_id=r[1], prompt_text=r[2],
                          origin=r[3], created_at=r[4])
            for r in rows
        ]

    # ── 读取 ─────────────────────────────────────────────────────────

    def runs_for_variant(self, prompt_variant_id: str, surface: str | None = None) -> list[SurfaceRun]:
        sql = f"SELECT {_RUN_COLUMNS} FROM surface_run WHERE prompt_variant_id = %s"
        params: list[Any] = [prompt_variant_id]
        if surface is not None:
            sql += " AND surface = %s"
            params.append(surface)
        sql += " ORDER BY run_timestamp"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_run(row) for row in rows]

    def all_runs(self) -> list[SurfaceRun]:
        rows = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM surface_run ORDER BY run_timestamp"
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def observations_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT observation_type, detector_name, detector_version, payload "
            "FROM derived_observation WHERE run_id = %s ORDER BY computed_at",
            (run_id,),
        ).fetchall()
        return [
            {"observation_type": r[0], "detector_name": r[1], "detector_version": r[2], **r[3]}
            for r in rows
        ]

    def max_detector_version(self, detector_name: str) -> int | None:
        row = self._conn.execute(
            "SELECT max(detector_version) FROM derived_observation WHERE detector_name = %s",
            (detector_name,),
        ).fetchone()
        return row[0] if row is not None else None


def _row_to_run(row: tuple) -> SurfaceRun:
    (run_id, need_id, variant_id, provider, surface, model, search_mode, locale, region,
     outcome, error, search_trace, answer, raw_payload, run_timestamp, latency_ms, cost_usd) = row
    return SurfaceRun(
        run_id=run_id,
        information_need_id=need_id,
        prompt_variant_id=variant_id,
        provider=provider,
        surface=surface,
        model=model,
        search_mode=search_mode,
        locale=locale,
        region=region,
        outcome=outcome,
        error=RunError.model_validate(error) if error is not None else None,
        search_trace=SearchTrace.model_validate(search_trace),
        answer=Answer.model_validate(answer) if answer is not None else None,
        raw_payload=raw_payload,
        timestamp=run_timestamp,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )
