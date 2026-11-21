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
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from engine.models import (
    ACCOUNT_ERROR_TYPES,
    Answer,
    BrandSpec,
    CitationResolution,
    DerivedObservation,
    FrozenModel,
    Outcome,
    PageCheck,
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
    """一个 surface × 型号 × 模式 × 语言的历史调用统计(v0.4.17;v0.4.19 加型号;2026-11-03 成本调研②加语言与用量):
    确认页预估用实测,不再拍脑袋。usage = 平均用量(有用量记录的那 n_usage 次;键是该仪器的计费项),
    预估按「平均用量 × 现牌价」算。"""

    surface: str
    model: str
    search_mode: str
    locale: str
    n: int
    avg_cost_usd: float
    avg_latency_ms: float
    usage: dict[str, float] = {}
    n_usage: int = 0
    # 最贵的一成调用(按记的费用排,十等分的最后一档)的平均用量与条数:确认页「贵的时候约 $N」与续跑弹窗的建议值按它算
    # (用户 2026-11-03 定 Q1 乙 / Q7 乙);不足 10 次分不出一成,空
    usage_high: dict[str, float] = {}
    n_high: int = 0


def _judgement(payload: dict[str, Any]) -> dict[str, Any]:
    """判定行里「判定本身」的部分:去掉每次都不同的行编号与算出时间,剩下的一样就是同一个判定。"""
    return {k: v for k, v in payload.items() if k not in ("observation_id", "computed_at")}


def _observation_type(observation: DerivedObservation) -> str:
    name = type(observation).__name__.removesuffix("Observation")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class StorageUnavailable(RuntimeError):
    """连不上数据库(v0.4.22):端口没开、或在听但不应答(2026-09-25 僵尸主进程)。
    不再无限挂起——调用方按错误代码 DB_UNREACHABLE 告诉用户该跑哪条命令。"""


# 「库不能用」的两种样子:一开始就连不上(StorageUnavailable),或者查到一半断了(psycopg 的 OperationalError)。
# 网页层据此给 WEB_DB_UNREACHABLE 的人话页(第二轮审核 B13:原来裸 500),不用自己认识 psycopg。
STORAGE_DOWN_ERRORS: tuple[type[Exception], ...] = (StorageUnavailable, psycopg.OperationalError)

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

    def insert_observation(self, observation: DerivedObservation) -> bool:
        """派生行只追加、可重算:同 (run, 判定器, 版本) 的最新一行若判定结果一样(同样的输入算出同样的东西——
        续跑、重新生成报告)就不再写;结果不一样(确认官网域名、加别名之后重算)就追加一行,读的一方取最新
        (report/response_view.latest_observation)。原来的「同 (run, 判定器, 版本) 至多一行」让判据改了之后的
        重算写不进库,单条回答页与结果文件和报告打架(审核 2026-10-27 黄 7)。返回这次有没有写。"""
        payload = observation.model_dump(mode="json")
        latest = self._conn.execute(
            "SELECT payload FROM derived_observation "
            "WHERE run_id = %s AND detector_name = %s AND detector_version = %s "
            "ORDER BY seq DESC LIMIT 1",  # 最新一行只认写入序号(第二轮审核 B5:按 Python 时间排会平局)
            (observation.run_id, observation.detector_name, observation.detector_version),
        ).fetchone()
        if latest is not None and _judgement(latest[0]) == _judgement(payload):
            return False
        self._conn.execute(
            "INSERT INTO derived_observation (observation_id, run_id, observation_type, "
            "detector_name, detector_version, computed_at, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                observation.observation_id,
                observation.run_id,
                _observation_type(observation),
                observation.detector_name,
                observation.detector_version,
                observation.computed_at,
                Jsonb(payload),
            ),
        )
        return True

    _PAGE_COLUMNS = ("check_id, url, normalized_url, fetched_at, status, http_status, final_url, title, text, "
                     "text_source, text_chars, truncated, error, fetcher_version, archive_url, archive_timestamp, "
                     "live_status")

    def insert_page_check(self, check: PageCheck) -> None:
        self._conn.execute(
            f"INSERT INTO page_check ({self._PAGE_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                check.check_id, check.url, check.normalized_url, check.fetched_at, check.status.value,
                check.http_status, check.final_url, check.title, check.text, check.text_source,
                check.text_chars, check.truncated, check.error, check.fetcher_version,
                check.archive_url, check.archive_timestamp,
                check.live_status.value if check.live_status is not None else None,
            ),
        )

    def recent_page_checks(self, normalized_urls: list[str], *, max_age_days: int = 7) -> dict[str, PageCheck]:
        """每个网址取时效窗口内最新一条打开记录(S6.1:同一网址 7 天内复用,不重抓)。"""
        if not normalized_urls:
            return {}
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        rows = self._conn.execute(
            f"SELECT DISTINCT ON (normalized_url) {self._PAGE_COLUMNS} "
            "FROM page_check WHERE normalized_url = ANY(%s) AND fetched_at > %s "
            "ORDER BY normalized_url, fetched_at DESC",
            (normalized_urls, cutoff),
        ).fetchall()
        return {
            row[2]: PageCheck(
                check_id=row[0], url=row[1], normalized_url=row[2], fetched_at=row[3], status=row[4],
                http_status=row[5], final_url=row[6], title=row[7], text=row[8], text_source=row[9],
                text_chars=row[10], truncated=row[11], error=row[12], fetcher_version=row[13],
                archive_url=row[14], archive_timestamp=row[15], live_status=row[16],
            )
            for row in rows
        }

    def insert_citation_resolution(self, resolution: CitationResolution) -> None:
        self._conn.execute(
            "INSERT INTO citation_resolution (resolution_id, url, final_url, hops, status, http_status, error, "
            "resolver_version, resolved_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                resolution.resolution_id, resolution.url, resolution.final_url, resolution.hops,
                resolution.status.value, resolution.http_status, resolution.error,
                resolution.resolver_version, resolution.resolved_at,
            ),
        )

    def latest_citation_resolutions(self, urls: list[str]) -> dict[str, CitationResolution]:
        """每条链接最新一条还原记录(按链接复用;时效与「失败要不要重试」由调用方定)。"""
        if not urls:
            return {}
        rows = self._conn.execute(
            "SELECT DISTINCT ON (url) resolution_id, url, final_url, hops, status, http_status, error, "
            "resolver_version, resolved_at FROM citation_resolution WHERE url = ANY(%s) "
            "ORDER BY url, resolved_at DESC",
            (urls,),
        ).fetchall()
        return {
            row[1]: CitationResolution(
                resolution_id=row[0], url=row[1], final_url=row[2], hops=row[3], status=row[4],
                http_status=row[5], error=row[6], resolver_version=row[7], resolved_at=row[8],
            )
            for row in rows
        }

    def record_version_first_seen(
        self, *, detector_name: str, version: int, rows_affected: int, trigger_kind: str, note: str | None = None,
    ) -> bool:
        """某判定器的这个版本第一次在库里落判定行:记一条重算审计(13.5),已经记过就不再记;返回这次有没有记。
        原来按「写之前库里有没有这个版本」猜:两单几乎同时存档会各记一条;存档中途断库,判定行写进去了、审计没记上,
        之后的单看见版本已在库里就永远不记了(第二轮审核 B6)。现在以审计表本身为准,按「判定器 + 版本」加事务级的
        咨询锁,两单同时来也只记一条;上次漏记的,下一单补上。"""
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"recompute:{detector_name}:{version}",))
            audited = self._conn.execute(
                "SELECT 1 FROM recompute_event WHERE detector_name = %s AND version_to = %s LIMIT 1",
                (detector_name, version),
            ).fetchone()
            if audited is not None:
                return False
            prior = self._conn.execute(
                "SELECT max(detector_version) FROM derived_observation "
                "WHERE detector_name = %s AND detector_version < %s",
                (detector_name, version),
            ).fetchone()
            self.insert_recompute_event(
                detector_name=detector_name, version_from=prior[0] if prior is not None else None, version_to=version,
                rows_affected=rows_affected, trigger_kind=trigger_kind, note=note,
            )
        return True

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

    def brand_prompts(self, brand_name: str, *, exclude_job_id: str) -> list[tuple[str, str, str]]:
        """同品牌别的任务里的题(prompt_id, 原文, 语言)——回答缓存的键里有品牌,品牌只在任务快照里(S11.2)。"""
        out: list[tuple[str, str, str]] = []
        for snapshot in self.load_job_snapshots():
            if snapshot.get("job_id") == exclude_job_id or (snapshot.get("brand") or {}).get("name") != brand_name:
                continue
            out += [(p["prompt_id"], p["text"], p["locale"]) for p in snapshot.get("prompts", [])]
        return out

    def insert_cache_reuse(self, *, job_id: str, run_id: str, source_run_id: str, cache_age_s: int) -> None:
        self._conn.execute(
            "INSERT INTO cache_reuse (reuse_id, job_id, run_id, source_run_id, reused_at, cache_age_s) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (str(uuid4()), job_id, run_id, source_run_id, datetime.now(UTC), cache_age_s),
        )

    def insert_call_usage(self, run_id: str, usage: dict[str, float]) -> None:
        """一次调用的计费用量(派生表 call_usage,成本调研②):从 raw_payload 读出来的,可重算,所以同一条可以覆盖;
        原始表 surface_run 一个字不动。"""
        self._conn.execute(
            "INSERT INTO call_usage (run_id, usage, extracted_at) VALUES (%s, %s, clock_timestamp()) "
            "ON CONFLICT (run_id) DO UPDATE SET usage = EXCLUDED.usage, extracted_at = EXCLUDED.extracted_at",
            (run_id, Jsonb(usage)),
        )

    def run_ids_without_usage(self) -> list[str]:
        """成功了却还没有用量记录的运行,按时间序(补老数据的脚本用)。"""
        rows = self._conn.execute(
            "SELECT run_id FROM surface_run r WHERE outcome = %s "
            "AND NOT EXISTS (SELECT 1 FROM call_usage u WHERE u.run_id = r.run_id) ORDER BY run_timestamp",
            (Outcome.SUCCESS.value,),
        ).fetchall()
        return [row[0] for row in rows]

    def success_run_ids(self, surface: str) -> list[str]:
        """某台仪器全部成功的运行,按时间序:记账规则改了,派生表 call_usage 整台重算(Q8;原始表一个字不动)。"""
        rows = self._conn.execute(
            "SELECT run_id FROM surface_run WHERE surface = %s AND outcome = %s ORDER BY run_timestamp",
            (surface, Outcome.SUCCESS.value),
        ).fetchall()
        return [row[0] for row in rows]

    def brand_prompt_ids(self, brand_name: str) -> list[str]:
        """同一个品牌名的全部单(含正在跑的这单)下的题:预估先看同品牌上一单的实测(成本调研 D5)。"""
        return [
            p["prompt_id"] for snapshot in self.load_job_snapshots()
            if (snapshot.get("brand") or {}).get("name") == brand_name
            for p in snapshot.get("prompts", [])
        ]

    def cache_reuse_count(self, job_id: str) -> int:
        row = self._conn.execute("SELECT count(*) FROM cache_reuse WHERE job_id = %s", (job_id,)).fetchone()
        return int(row[0]) if row is not None else 0

    def cache_copies(self, run_ids: list[str]) -> set[str]:
        """这些运行里哪些是从缓存复制来的:找缓存时只拿原件(审核 2026-10-27 黄 6)。"""
        if not run_ids:
            return set()
        rows = self._conn.execute("SELECT run_id FROM cache_reuse WHERE run_id = ANY(%s)", (run_ids,)).fetchall()
        return {r[0] for r in rows}

    def load_job_snapshots(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT snapshot FROM diagnosis_job ORDER BY created_at"
        ).fetchall()
        return [row[0] for row in rows]

    # ── 调用统计(v0.4.17 预估口径)──────────────────────────────────────

    def call_stats(
        self, prompt_ids: Iterable[str] | None = None, *, exclude_prompt_ids: Iterable[str] = (),
    ) -> list[CallStat]:
        """按 surface × 型号 × 模式 × 语言的历史实测均价、均时与平均用量,供确认页预估。只看成功的调用(v0.4.21):
        预估的是「拿到一次有效回答要花多少」,失败行记的 $0 与含退避的耗时不代表这个价。从别的单复制来的回答
        (cache_reuse)花费记 0,不算(成本调研 D13)。prompt_ids 给了只统计这些题下的运行(同品牌上一单优先,D5)。
        exclude_prompt_ids 里的题不算:调用方拿它把校准题排除在外(用户 2026-11-20 定「拿掉」——校准题里的反例题基本不搜,
        和真单的题不是一类,混进来估价会偏,错误日志 2026-11-19)。"""
        where = "r.outcome = %s AND NOT EXISTS (SELECT 1 FROM cache_reuse c WHERE c.run_id = r.run_id)"
        params: list[Any] = [Outcome.SUCCESS.value]
        if prompt_ids is not None:
            where += " AND r.prompt_id = ANY(%s)"
            params.append(list(prompt_ids))
        excluded = list(exclude_prompt_ids)
        if excluded:
            where += " AND NOT (r.prompt_id = ANY(%s))"
            params.append(excluded)
        rows = self._conn.execute(
            "SELECT r.surface, r.model, r.search_mode, r.locale, count(*), avg(r.cost_usd), avg(r.latency_ms) "
            f"FROM surface_run r WHERE {where} GROUP BY 1, 2, 3, 4",
            params,
        ).fetchall()
        usage_rows = self._conn.execute(
            "SELECT r.surface, r.model, r.search_mode, r.locale, e.key, sum(e.value::float), count(DISTINCT r.run_id) "
            "FROM surface_run r JOIN call_usage u ON u.run_id = r.run_id "
            f"CROSS JOIN LATERAL jsonb_each_text(u.usage) e WHERE {where} GROUP BY 1, 2, 3, 4, 5",
            params,
        ).fetchall()
        # 最贵的一成(Q1 / Q7):每桶按记的费用十等分,只取最后一档的用量平均;不足 10 次没有第 10 档
        high_rows = self._conn.execute(
            "WITH ranked AS (SELECT r.run_id, r.surface, r.model, r.search_mode, r.locale, "
            "ntile(10) OVER (PARTITION BY r.surface, r.model, r.search_mode, r.locale ORDER BY r.cost_usd) AS decile "
            f"FROM surface_run r WHERE {where}) "
            "SELECT k.surface, k.model, k.search_mode, k.locale, e.key, sum(e.value::float), count(DISTINCT k.run_id) "
            "FROM ranked k JOIN call_usage u ON u.run_id = k.run_id CROSS JOIN LATERAL jsonb_each_text(u.usage) e "
            "WHERE k.decile = 10 GROUP BY 1, 2, 3, 4, 5",
            params,
        ).fetchall()

        def averaged(found: list[tuple]) -> tuple[dict[tuple, dict[str, float]], dict[tuple, int]]:
            means: dict[tuple, dict[str, float]] = {}
            counts: dict[tuple, int] = {}
            for surface, model, mode, locale, key, total, n in found:
                group = (surface, model, mode, locale)
                means.setdefault(group, {})[key] = float(total) / n
                counts[group] = max(counts.get(group, 0), n)
            return means, counts

        usage, n_usage = averaged(usage_rows)
        usage_high, n_high = averaged(high_rows)
        return [
            CallStat(surface=surface, model=model, search_mode=mode, locale=locale, n=n,
                     avg_cost_usd=float(cost or 0.0), avg_latency_ms=float(latency or 0.0),
                     usage=usage.get((surface, model, mode, locale), {}),
                     n_usage=n_usage.get((surface, model, mode, locale), 0),
                     usage_high=usage_high.get((surface, model, mode, locale), {}),
                     n_high=n_high.get((surface, model, mode, locale), 0))
            for surface, model, mode, locale, n, cost, latency in rows
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

    def job_progress(self, prompt_ids: list[str], runs_per_prompt: int) -> tuple[int, float]:
        """一单已问了几个槽位(题 × 仪器 × 型号 × 模式下第 1–每题次数 次,账户问题那几行不算——和续跑复用槽位同一口径)、
        这些题上一共花了多少钱。重启后装回被中断 / 出错的单时用
        (第三轮审核:原来任务页写「已问 0/0」、首页「已花费 $0」)。"""
        row = self._conn.execute(
            "SELECT count(DISTINCT (prompt_id, surface, model, search_mode, run_index)) FILTER ("
            "  WHERE run_index <= %s AND NOT (coalesce(error->>'error_type', '') = ANY(%s))), "
            "coalesce(sum(cost_usd), 0) FROM surface_run WHERE prompt_id = ANY(%s)",
            (runs_per_prompt, [t.value for t in ACCOUNT_ERROR_TYPES], prompt_ids),
        ).fetchone()
        return (int(row[0]), float(row[1])) if row else (0, 0.0)

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

    def job_brands_and_runs(
        self, job_ids: Iterable[str] | None = None,
    ) -> list[tuple[str, BrandSpec, list[SurfaceRun]]]:
        """每一单:(任务编号, 它冻结的品牌卡, 它的题下的全部运行)——全量重算按单来(13.5;第二轮审核 B7):各单冻结的
        别名、官网名单可能不同,一单的回答只能拿它自己的品牌卡去判。job_ids 不给 = 全部单。"""
        wanted = set(job_ids) if job_ids is not None else None
        out: list[tuple[str, BrandSpec, list[SurfaceRun]]] = []
        for snapshot in self.load_job_snapshots():
            job_id = snapshot.get("job_id", "")
            if wanted is not None and job_id not in wanted:
                continue
            prompt_ids = sorted({p["prompt_id"] for p in snapshot.get("prompts", [])})
            rows = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM surface_run WHERE prompt_id = ANY(%s) ORDER BY run_timestamp",
                (prompt_ids,),
            ).fetchall() if prompt_ids else []
            out.append((job_id, BrandSpec.model_validate(snapshot["brand"]), [_row_to_run(row) for row in rows]))
        return out

    def run_by_id(self, run_id: str) -> SurfaceRun | None:
        """单条回答页用(S8.3):没有这条就 None,归属校验由调用方做。"""
        row = self._conn.execute(f"SELECT {_RUN_COLUMNS} FROM surface_run WHERE run_id = %s", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def observations_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT observation_type, detector_name, detector_version, payload "
            "FROM derived_observation WHERE run_id = %s ORDER BY seq",  # 按写入先后(同版本里最后一行 = 最新)
            (run_id,),
        ).fetchall()
        return [
            {"observation_type": r[0], "detector_name": r[1], "detector_version": r[2], **r[3]}
            for r in rows
        ]

    def observations_for_runs(self, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """多条回答的判定行一次取齐(结果文件打包用,S8.4):按 run_id 分组,组内按写入先后;没有行的 run_id 不在结果里。"""
        if not run_ids:
            return {}
        rows = self._conn.execute(
            "SELECT run_id, observation_type, detector_name, detector_version, payload "
            "FROM derived_observation WHERE run_id = ANY(%s) ORDER BY seq",
            (run_ids,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append(
                {"observation_type": r[1], "detector_name": r[2], "detector_version": r[3], **r[4]}
            )
        return grouped

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
