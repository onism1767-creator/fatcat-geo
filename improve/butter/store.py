"""落盘:全仓库唯一写文件的模块。每次运行一个新文件夹,只新增、不覆盖。

- 运行文件夹已存在就拒写;文件一律独占创建(已存在即报错)。
- 抓到的原始 HTML 原样存进 snapshot/,供不联网重跑;它是不可信数据,只存不执行。
- 改动包里的文件名由生成器给出:落盘前校验是相对路径、不含 ..。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from butter.contracts import AuditReport, FactBase, FetchedPage, PackageBundle, SiteSnapshot

SNAPSHOT_INDEX = "snapshot.json"


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
