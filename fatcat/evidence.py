"""证据表只有一份吗:核对改进工具自带的那份复制件,和测量工具现在的那份是不是同一份内容。

来历:测量工具的 `config/evidence_registry.yaml` 是原件(20 条)。改进工具在本机找得到它时直接读原件;
找不到时用自带的一份复制件(`butter/registry/toast_snapshot/`),复制件旁边记着抄的日子和内容指纹。
改进工具另有自己的 13 条扩展,编号不许和原件撞。

三个仓库并成一个之前,复制件删不掉(改进工具单独运行时要靠它)。所以这里先做能自动做的那一半:
原件一改、复制件没跟上,就在这里报出来;编号撞了也报。只读。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fatcat.modules import Module, ModuleError

ORIGINAL = Path("config") / "evidence_registry.yaml"
COPY_DIR = Path("butter") / "registry" / "toast_snapshot"
EXTENSION = Path("butter") / "registry" / "registry_extension.yaml"


def content_sha256(raw: bytes) -> str:
    """换行统一成 LF 以后的内容指纹(和改进工具记指纹的算法一样:原件是 CRLF,复制件入库是 LF)。"""
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _ids(path: Path) -> list[str]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        return [str(entry["id"]) for entry in doc["entries"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModuleError(f"读不了证据表 {path}:{type(exc).__name__}") from exc


@dataclass(frozen=True)
class EvidenceState:
    original_ids: list[str]
    original_sha: str
    copy_ids: list[str]
    copy_sha: str            # 复制件文件现在的内容指纹
    recorded_sha: str        # 复制件旁边记下的指纹(抄的那一刻原件的)
    copied_on: str
    extension_ids: list[str]


def read(toast: Module, butter: Module) -> EvidenceState:
    original = toast.require_path() / ORIGINAL
    copy = butter.require_path() / COPY_DIR / "evidence_registry.yaml"
    meta_path = butter.path / COPY_DIR / "SNAPSHOT.yaml"
    try:
        meta: dict[str, Any] = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        return EvidenceState(
            original_ids=_ids(original), original_sha=content_sha256(original.read_bytes()),
            copy_ids=_ids(copy), copy_sha=content_sha256(copy.read_bytes()), recorded_sha=str(meta["sha256"]),
            copied_on=str(meta["taken_on"]), extension_ids=_ids(butter.path / EXTENSION))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModuleError(f"读不了改进工具那份证据表的记录 {meta_path}:{type(exc).__name__}") from exc


def problems(state: EvidenceState) -> list[str]:
    """对不上的地方,一条一句。空 = 全对得上。"""
    out: list[str] = []
    if state.copy_sha != state.recorded_sha:
        out.append("改进工具自带的那份证据表,文件内容和它旁边记的指纹不一样:文件被改过,或者抄完没更新记录。")
    if state.copy_sha != state.original_sha:
        added = sorted(set(state.original_ids) - set(state.copy_ids))
        gone = sorted(set(state.copy_ids) - set(state.original_ids))
        detail = "".join(f";原件多了 {'、'.join(added)}" if added else "") + (
            f";原件没有了 {'、'.join(gone)}" if gone else "")
        out.append(f"改进工具自带的那份证据表是 {state.copied_on} 抄的,测量工具的原件后来改过,两份内容不一样了{detail}。"
                   "本机两个文件夹都在时改进工具读的是原件,不受影响;单独拿走改进工具时会用到这份旧的。")
    clash = sorted(set(state.original_ids) & set(state.extension_ids))
    if clash:
        out.append(f"改进工具的扩展条目和原件用了同一个编号:{'、'.join(clash)}。")
    repeated = sorted({i for i in state.original_ids + state.extension_ids
                       if (state.original_ids + state.extension_ids).count(i) > 1} - set(clash))
    if repeated:
        out.append(f"同一份表里编号重复:{'、'.join(repeated)}。")
    return out


def render(state: EvidenceState, found: list[str]) -> str:
    lines = [f"证据表:原件 {len(state.original_ids)} 条(测量工具),扩展 {len(state.extension_ids)} 条(改进工具),"
             f"合起来 {len(set(state.original_ids) | set(state.extension_ids))} 条。",
             f"改进工具自带的复制件:{len(state.copy_ids)} 条,{state.copied_on} 抄的。"]
    if not found:
        return "\n".join(lines + ["复制件和原件是同一份内容,编号没有撞。"])
    return "\n".join(lines + [f"- {text}" for text in found] + [f"合计 {len(found)} 处。"])
