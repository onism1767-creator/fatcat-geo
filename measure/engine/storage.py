"""L2 存储:只追加的原始层与派生层(铁律 8、13.5)。

- surface_run / derived_observation / recompute_event / aux_call(辅助调用账本)/ report_snapshot 由数据库触发器
  强制只追加:UPDATE / DELETE / TRUNCATE 一律被 RAISE EXCEPTION 拒绝。
  不可变性不靠代码纪律,靠数据库拒绝(13.5);tests/unit/test_storage.py
  的不变式测试直接对真库验证。
- 判定器重算 = 以新 detector_version 追加新观测行,旧行永远保留;
  每次全量重算落一条 RecomputeEvent 审计(没有这张表,指标变化时
  分不清是世界变了还是判定器变了)。
- 建表语句与触发器在 engine/schema.py(v1 单机,无迁移框架,幂等可重复执行)。
  表结构换代(v0.4.26 S2:题目取代需求 + 问法)用新库 geo_toast;旧方法的数据留在 geo_tool,原样不动。
"""

from __future__ import annotations

import os
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
    FrozenModel,
    Outcome,
    Prompt,
    RunError,
    SearchTrace,
    SurfaceRun,
)
from engine.schema import SCHEMA_STATEMENTS

_RUN_COLUMNS = (
    "run_id, prompt_id, run_index, provider, surface, model, "
    "search_mode, locale, region, outcome, error, search_trace, answer, raw_payload, "
    "run_timestamp, latency_ms, cost_usd"
)


class CallStat(FrozenModel):
    """一个 surface × 型号 × 模式的历史调用统计(v0.4.17;v0.4.19 加型号):
    确认页预估用实测均值,不再拍脑袋。"""

    surface: str
    model: str
    search_mode: str
    n: int
    avg_cost_usd: float
    avg_latency_ms: float


def _observation_type(observation: DerivedObservation) -> str:
    name = type(observation).__name__.removesuffix("Observation")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class StorageUnavailable(RuntimeError):
    """连不上数据库(v0.4.22):端口没开、或在听但不应答(2026-09-25 僵尸主进程)。
    不再无限挂起——调用方按错误代码 DB_UNREACHABLE 告诉用户该跑哪条命令。"""


CONNECT_TIMEOUT_S = 10
DEFAULT_DSN = "postgresql://geo@localhost:5433/geo_toast"  # 项目专用库(scripts/pgdev.ps1 管理)


def default_dsn() -> str:
    """项目库地址:环境变量 GEO_DATABASE_URL 优先,否则本机专用库。全项目只从这里取。"""
    return os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)


class Storage:
    def __init__(self, dsn: str) -> None:
        try:
            self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S)
        except psycopg.OperationalError as exc:
            raise StorageUnavailable(f"{CONNECT_TIMEOUT_S} 秒内连不上数据库: {exc}") from exc

    # ── 生命周期 ──────────────────────────────────────────────────────

    def ensure_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self._conn.execute(statement)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── 写入(只追加)──────────────────────────────────────────────────

    def insert_prompt(self, prompt: Prompt) -> None:
        """题目幂等落库(续跑重复插入静默跳过)。"""
        self._conn.execute(
            "INSERT INTO prompt (prompt_id, text, intent, origin, locale, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (prompt_id) DO NOTHING",
            (prompt.prompt_id, prompt.text, prompt.intent.value, prompt.origin.value,
             prompt.locale, prompt.created_at),
        )

    def insert_surface_run(self, run: SurfaceRun) -> None:
        self._conn.execute(
            f"INSERT INTO surface_run ({_RUN_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run.run_id,
                run.prompt_id,
                run.run_index,
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

    # ── 调用统计(v0.4.17 预估口径)──────────────────────────────────────

    def call_stats(self) -> list[CallStat]:
        """按 surface × 型号 × 模式的历史实测均价与均时,供确认页预估。只看成功的调用(v0.4.21):
        预估的是「拿到一次有效回答要花多少」,失败行记的 $0 与含退避的耗时不代表这个价。"""
        rows = self._conn.execute(
            "SELECT surface, model, search_mode, count(*), avg(cost_usd), avg(latency_ms) "
            "FROM surface_run WHERE outcome = %s GROUP BY surface, model, search_mode",
            (Outcome.SUCCESS.value,),
        ).fetchall()
        return [
            CallStat(surface=surface, model=model, search_mode=mode, n=n,
                     avg_cost_usd=float(cost or 0.0), avg_latency_ms=float(latency or 0.0))
            for surface, model, mode, n, cost, latency in rows
        ]

    # ── 报告快照(v0.4.16,只追加)────────────────────────────────────────

    def insert_report_snapshot(
        self, *, job_id: str, fingerprint: str, code_version: str | None,
        spent_usd: float, payload: dict[str, Any],
    ) -> None:
        """追加一版报告。payload 对 L2 不透明(结构由 L4 定义)。

        created_at 由数据库盖章(clock_timestamp,微秒级):「最新一版」靠它排序,
        而 Windows 上 Python 时钟粒度是毫秒级,紧挨着的两版会撞成同一时刻。"""
        self._conn.execute(
            "INSERT INTO report_snapshot "
            "(snapshot_id, job_id, created_at, fingerprint, code_version, spent_usd, payload) "
            "VALUES (%s, %s, clock_timestamp(), %s, %s, %s, %s)",
            (str(uuid4()), job_id, fingerprint, code_version, spent_usd, Jsonb(payload)),
        )

    def insert_aux_call(self, *, job_id: str, task: str, run: SurfaceRun) -> None:
        """辅助调用记一笔(整条 SurfaceRun 原样存档)。created_at 由数据库盖章。"""
        self._conn.execute(
            "INSERT INTO aux_call (call_id, job_id, task, created_at, cost_usd, outcome, run) "
            "VALUES (%s, %s, %s, clock_timestamp(), %s, %s, %s)",
            (run.run_id, job_id, task, run.cost_usd, run.outcome.value, Jsonb(run.model_dump(mode="json"))),
        )

    def aux_spent_usd(self, job_id: str) -> float:
        """该任务辅助调用的累计花费(没有记录 = 0)。"""
        row = self._conn.execute(
            "SELECT coalesce(sum(cost_usd), 0) FROM aux_call WHERE job_id = %s", (job_id,)
        ).fetchone()
        return float(row[0]) if row else 0.0

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

    # ── 读取 ─────────────────────────────────────────────────────────

    def runs_for_prompt(
        self, prompt_id: str, surface: str | None = None, model: str | None = None,
    ) -> list[SurfaceRun]:
        """一道题在一台仪器(可限型号)上的全部运行,按时间序。型号是口径的一部分(v0.4.19):
        取样本时必须按型号分开,否则续跑槽位与指标分母会串型号。"""
        sql = f"SELECT {_RUN_COLUMNS} FROM surface_run WHERE prompt_id = %s"
        params: list[Any] = [prompt_id]
        if surface is not None:
            sql += " AND surface = %s"
            params.append(surface)
        if model is not None:
            sql += " AND model = %s"
            params.append(model)
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
    (run_id, prompt_id, run_index, provider, surface, model, search_mode, locale, region,
     outcome, error, search_trace, answer, raw_payload, run_timestamp, latency_ms, cost_usd) = row
    return SurfaceRun(
        run_id=run_id,
        prompt_id=prompt_id,
        run_index=run_index,
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
