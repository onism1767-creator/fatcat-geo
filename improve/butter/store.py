"""落盘:全仓库唯一写文件的模块。每次运行一个新文件夹,只新增、不覆盖。

- 运行文件夹已存在就拒写;文件一律独占创建(已存在即报错)。
- 抓到的原始 HTML 原样存进 snapshot/,供不联网重跑;它是不可信数据,只存不执行。
- 改动包里的文件名由生成器给出:落盘前校验是相对路径、不含 ..。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from butter.contracts import AuditReport, CardEvent, FactBase, FetchedPage, PackageBundle, SiteSnapshot

SNAPSHOT_INDEX = "snapshot.json"
CARD_EVENTS = "events/cards.jsonl"  # 批次与卡片的事件:唯一事实来源,只追加(设计 §6)
CARD_INDEX = "index.sqlite"         # 由事件日志重建的索引:可删,删了能完整重建(ADR-B02)


class RunDirExists(FileExistsError):
    pass


def _write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _safe_relative(name: str) -> PurePosixPath:
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or ":" in name or "\\" in name:
        raise ValueError(f"改动包文件名不合法:{name!r}")
    return rel


def new_run_dir(out_root: Path, host: str, run_id: str) -> Path:
    safe_host = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in host) or "site"
    run_dir = out_root / safe_host / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RunDirExists(f"运行文件夹已存在,拒绝覆盖:{run_dir}") from exc
    return run_dir


def save_snapshot(run_dir: Path, snapshot: SiteSnapshot) -> None:
    snap_dir = run_dir / "snapshot"
    index = snapshot.model_dump(mode="json")
    for i, page in enumerate(snapshot.pages):
        html = page.html
        index["pages"][i]["html"] = ""
        if html:
            name = f"pages/{i:02d}.html"
            index["pages"][i]["html_file"] = name
            index["pages"][i]["html_sha256"] = hashlib.sha256(html.encode("utf-8")).hexdigest()
            _write_new(snap_dir / name, html)
    robots_text = index["robots"]["text"]
    if robots_text:
        index["robots"]["text"] = ""
        index["robots"]["text_file"] = "robots.txt"
        _write_new(snap_dir / "robots.txt", robots_text)
    _write_new(snap_dir / SNAPSHOT_INDEX, json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def load_snapshot(snapshot_dir: Path) -> SiteSnapshot:
    """读回一份存好的快照(只读)。可以给运行文件夹,也可以直接给其中的 snapshot/。"""
    snap_dir = snapshot_dir / "snapshot" if (snapshot_dir / "snapshot" / SNAPSHOT_INDEX).exists() else snapshot_dir
    index = json.loads((snap_dir / SNAPSHOT_INDEX).read_text(encoding="utf-8"))
    pages = []
    for raw in index["pages"]:
        html_file = raw.pop("html_file", None)
        raw.pop("html_sha256", None)
        if html_file:
            raw["html"] = (snap_dir / _safe_relative(html_file)).read_text(encoding="utf-8")
        pages.append(FetchedPage.model_validate(raw))
    robots = index["robots"]
    text_file = robots.pop("text_file", None)
    if text_file:
        robots["text"] = (snap_dir / _safe_relative(text_file)).read_text(encoding="utf-8")
    index["pages"] = [p.model_dump(mode="json") for p in pages]
    return SiteSnapshot.model_validate(index)


def save_outputs(
    run_dir: Path, *, factbase: FactBase, factbase_md: str, audit: AuditReport, audit_md: str,
    bundle: PackageBundle, run_meta: dict,
) -> None:
    _write_new(run_dir / "factbase.json", factbase.model_dump_json(indent=2) + "\n")
    _write_new(run_dir / "factbase.md", factbase_md)
    _write_new(run_dir / "audit.json", audit.model_dump_json(indent=2) + "\n")
    _write_new(run_dir / "audit.md", audit_md)
    package_dir = run_dir / "change-package"
    for name, text in bundle.files.items():
        _write_new(package_dir / _safe_relative(name), text)
    _write_new(run_dir / "run.json", json.dumps(run_meta, ensure_ascii=False, indent=2) + "\n")


def append_run_index(out_root: Path, record: dict) -> None:
    """只追加的运行索引:一行一次运行。"""
    out_root.mkdir(parents=True, exist_ok=True)
    with open(out_root / "runs.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cache_path(cache_dir: Path, digest: str) -> Path:
    if not digest or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"缓存指纹不合法:{digest!r}")
    return cache_dir / digest[:2] / f"{digest}.json"


def read_llm_cache(cache_dir: Path, digest: str) -> dict | None:
    """读一条模型回答的缓存(只读)。没有、或文件坏了,都当没有。"""
    path = _cache_path(cache_dir, digest)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None


def write_llm_cache(cache_dir: Path, digest: str, record: dict) -> None:
    """存一条模型回答。同一个指纹已经有了就不动(只新增、不覆盖)。里面没有密钥。"""
    path = _cache_path(cache_dir, digest)
    try:
        _write_new(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        pass


# ── 提案文档 ─────────────────────────────────────────────────────────

def save_text_files(out_dir: Path, files: dict[str, str]) -> list[Path]:
    """把一组文本文件(相对路径 → 内容)存进一个新文件夹。文件夹已存在就拒写:只新增、不覆盖。"""
    names = [(_safe_relative(name), text) for name, text in files.items()]
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RunDirExists(f"文件夹已存在,拒绝覆盖:{out_dir}") from exc
    written = []
    for rel, text in names:
        _write_new(out_dir / rel, text)
        written.append(out_dir / rel)
    return written


# ── 卡片库:事件日志(只追加)+ SQLite 索引(可重建) ─────────────────────

class CardLogBusy(OSError):
    pass


@contextmanager
def _single_writer(lock_path: Path) -> Iterator[None]:
    """同一时刻只让一个进程写事件日志(设计 §6 规则 2)。用系统的文件锁:进程没了锁自动放开,不会留下死锁文件。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # 拿不到就每秒重试,约 10 秒后放弃
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise CardLogBusy("事件日志正被另一个进程写,稍后再试") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_card_events(workspace: Path) -> tuple[list[CardEvent], list[str]]:
    """读整份事件日志(只读)。读不了的行跳过并报告;最后一行是没写完的半行也容忍(设计 §6 规则 2)。"""
    try:
        raw = (workspace / CARD_EVENTS).read_bytes()
    except FileNotFoundError:
        return [], []
    lines = raw.decode("utf-8", errors="replace").split("\n")
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    events: list[CardEvent] = []
    problems: list[str] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(CardEvent.model_validate_json(line))
        except ValueError:
            half = i == last and not raw.endswith(b"\n")
            problems.append(f"事件日志第 {i + 1} 行" + ("是没写完的半行,已忽略" if half else "读不了,已跳过"))
    return events, problems


def _append_lines(path: Path, lines: list[str]) -> None:
    """只追加;写完就刷盘。上一次留下半行时先补一个换行,新事件不会接在半行后面。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) != b"\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if needs_newline:
            handle.write("\n")
        for line in lines:
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


CardLogStep = Callable[[list[CardEvent], list[str]], tuple[Any, list[CardEvent], dict[str, list[dict]] | None]]


def with_card_log(workspace: Path, step: CardLogStep) -> tuple[Any, str | None]:
    """持锁做完一整步:读全部事件 → step 决定要追加什么 → 追加并刷盘 → 更新索引。

    step(events, problems) 返回(结果, 要追加的事件, 要更新的索引行或 None)。
    返回(结果, 索引没更新上的原因或 None)。事件一旦追加就算数;索引坏了不影响它,重建即可。
    """
    with _single_writer(workspace / "events" / "cards.lock"):
        events, problems = read_card_events(workspace)
        result, new_events, tables = step(events, problems)
        if new_events:
            _append_lines(workspace / CARD_EVENTS, [event.model_dump_json() for event in new_events])
        index_problem = None
        if tables is not None:
            try:
                upsert_card_index(workspace, tables)
            except sqlite3.Error as exc:
                index_problem = f"{type(exc).__name__}: {exc}"
    return result, index_problem


# 表 → 列定义。第一列是主键;先后顺序就是建表与写入顺序(卡片引用批次)。列名要和 butter/cards/index.py 出的行对得上。
_INDEX_TABLES: dict[str, tuple[str, ...]] = {
    "events": ("seq INTEGER PRIMARY KEY", "event_id TEXT NOT NULL UNIQUE", "at TEXT NOT NULL", "actor TEXT NOT NULL",
               "batch_id TEXT NOT NULL", "card_id TEXT", "type TEXT NOT NULL", "payload TEXT NOT NULL"),
    "batches": ("batch_id TEXT PRIMARY KEY", "site_host TEXT NOT NULL", "run_id TEXT NOT NULL", "phase TEXT NOT NULL",
                "baseline_day TEXT", "deploy_day TEXT", "recommended_from TEXT", "scored INTEGER NOT NULL",
                "outcome TEXT", "cards INTEGER NOT NULL", "created_at TEXT NOT NULL", "updated_at TEXT NOT NULL",
                "state TEXT NOT NULL"),
    "cards": ("card_id TEXT PRIMARY KEY", "batch_id TEXT NOT NULL REFERENCES batches(batch_id)",
              "entry_id TEXT NOT NULL", "change_item_id TEXT NOT NULL", "deployed INTEGER NOT NULL",
              "validation TEXT NOT NULL", "direction TEXT", "outcome TEXT", "card TEXT NOT NULL"),
    "methods": ("entry_id TEXT PRIMARY KEY", "more_state TEXT", "more_independent INTEGER NOT NULL",
                "fewer_state TEXT", "fewer_independent INTEGER NOT NULL", "cards_total INTEGER NOT NULL",
                "summary TEXT NOT NULL"),
}


def _columns(table: str) -> list[str]:
    return [definition.split()[0] for definition in _INDEX_TABLES[table]]


def _open_index(workspace: Path) -> sqlite3.Connection:
    workspace.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(workspace / CARD_INDEX, timeout=5, isolation_level=None)  # 事务自己开、自己提交
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    for table, definitions in _INDEX_TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(definitions)})")


def _upsert_rows(conn: sqlite3.Connection, tables: dict[str, list[dict]]) -> None:
    unknown = set(tables) - set(_INDEX_TABLES)
    if unknown:
        raise ValueError(f"索引里没有这些表:{sorted(unknown)}")
    for table in _INDEX_TABLES:
        columns = _columns(table)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns[1:])
        sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
               f"ON CONFLICT({columns[0]}) DO UPDATE SET {updates}")
        for row in tables.get(table, []):
            if set(row) != set(columns):
                raise ValueError(f"索引表 {table} 的行和列对不上:{sorted(set(row) ^ set(columns))}")
            conn.execute(sql, [row[c] for c in columns])


def upsert_card_index(workspace: Path, tables: dict[str, list[dict]]) -> None:
    """增量更新索引:有就改、没有就加。一次调用是一个事务。"""
    with closing(_open_index(workspace)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _create_tables(conn)
            _upsert_rows(conn, tables)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def rebuild_card_index(workspace: Path, tables: dict[str, list[dict]]) -> None:
    """整份重建索引:旧表全删,按事件日志推出来的行重新写。索引文件被删了也一样(会新建)。"""
    with closing(_open_index(workspace)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in reversed(list(_INDEX_TABLES)):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            _create_tables(conn)
            _upsert_rows(conn, tables)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def read_card_index(workspace: Path, table: str) -> list[dict]:
    """读索引里的一张表(按主键排好)。还没有索引文件,或表还没建,就是空的。"""
    if table not in _INDEX_TABLES:
        raise ValueError(f"索引里没有这张表:{table}")
    if not (workspace / CARD_INDEX).exists():
        return []
    columns = _columns(table)
    with closing(_open_index(workspace)) as conn:
        try:
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {columns[0]}").fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(zip(columns, row, strict=True)) for row in rows]
