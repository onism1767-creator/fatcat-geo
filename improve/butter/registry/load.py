"""读注册表文件(外壳:只读,不写)。

Toast 的文件在本机 → 用它;找不到 → 用 Butter 自带的快照,来历里写明快照日期(设计 §11)。
Toast 的文件在、但按 Toast 的规则加载不过 → 报错,不悄悄换成快照:那说明 Toast 的格式变了,Butter 要跟着改。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from butter.contracts import EvidenceEntry, RegistryOrigin, RegistrySource
from butter.registry.core import Registry, RegistryError, parse_entries

HERE = Path(__file__).resolve().parent
EXTENSION_PATH = HERE / "registry_extension.yaml"
SNAPSHOT_PATH = HERE / "toast_snapshot" / "evidence_registry.yaml"
SNAPSHOT_META_PATH = HERE / "toast_snapshot" / "SNAPSHOT.yaml"
TOAST_DIR_NAME = "你的网页被增强了快上"


def default_toast_registry_path() -> Path:
    """默认位置:与本项目同级的 Toast 文件夹。以后由设置页给(设计 §11),这里只是默认值。"""
    return HERE.parents[3] / TOAST_DIR_NAME / "config" / "evidence_registry.yaml"


def content_sha256(raw: bytes) -> str:
    """按「换行统一为 LF 后」的内容算 sha256。Toast 的文件是 CRLF,本仓库入库一律 LF,不统一就永远对不上。"""
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def read_snapshot_meta() -> dict[str, Any]:
    return yaml.safe_load(SNAPSHOT_META_PATH.read_text(encoding="utf-8"))


def read_registry_file(path: Path, *, origin: RegistryOrigin) -> tuple[RegistrySource, list[EvidenceEntry]]:
    """读一份注册表文件,按 Toast 的规则校验,带上来历(路径、sha256、条目数;快照另带日期与出处)。"""
    raw = Path(path).read_bytes()
    entries = parse_entries(yaml.safe_load(raw.decode("utf-8")), origin=str(path))
    digest = content_sha256(raw)
    extra: dict[str, str] = {}
    if origin == "toast_snapshot":
        meta = read_snapshot_meta()
        if meta.get("sha256") != digest or meta.get("entries") != len(entries):
            raise RegistryError(f"{path}:快照内容与 {SNAPSHOT_META_PATH.name} 记录的 sha256 或条目数不符")
        extra = {"snapshot_taken_on": str(meta["taken_on"]),
                 "snapshot_of": f"{meta['source']} @ {str(meta['toast_commit'])[:7]}({meta['toast_commit_date']})"}
    source = RegistrySource(origin=origin, path=str(path), sha256=digest, entries=len(entries), **extra)
    return source, entries


def load_registry(toast_path: Path | None = None, *, extension_path: Path = EXTENSION_PATH) -> Registry:
    """Toast 的 20 条 + Butter 扩展 13 条,合成一张按 ID 查的表。ID 撞车直接报错。"""
    path = Path(toast_path) if toast_path is not None else default_toast_registry_path()
    if path.is_file():
        toast = read_registry_file(path, origin="toast")
    else:
        toast = read_registry_file(SNAPSHOT_PATH, origin="toast_snapshot")
    extension = read_registry_file(Path(extension_path), origin="butter_extension")
    return Registry([toast, extension])
