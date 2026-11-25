"""读 Web Lite 的证据包(用户从网站上下载的 zip)。只读,不解压到硬盘。

证据包是外面来的文件:只按名字取那几个 JSON,每个设大小上限,别的成员一概不碰。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from fatcat.modules import ModuleError

_WANTED = ("manifest.json", "findings.json", "signals.json", "methodology.json")
_REQUIRED = ("manifest.json", "findings.json")
_MAX_BYTES = 20 * 1024 * 1024  # 单个 JSON 的上限;真实的证据包里这几个文件都在几十 KB
SUPPORTED_RULESETS = ("1.0.0",)


def load_bundle(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ModuleError(f"找不到证据包:{path}")
    try:
        with zipfile.ZipFile(path) as archive:
            members = {info.filename: info for info in archive.infolist() if not info.is_dir()}
            loaded: dict[str, Any] = {"path": str(path), "files": sorted(Path(name).name for name in members)}
            for wanted in _WANTED:
                info = next((i for name, i in members.items() if name == wanted or name.endswith("/" + wanted)), None)
                if info is None:
                    if wanted in _REQUIRED:
                        raise ModuleError(f"这个 zip 里没有 {wanted},不是 Quick Check 的证据包:{path.name}")
                    loaded[wanted.removesuffix(".json")] = None
                    continue
                if info.file_size > _MAX_BYTES:
                    raise ModuleError(f"证据包里的 {wanted} 有 {info.file_size} 字节,超过上限,没有读")
                loaded[wanted.removesuffix(".json")] = json.loads(archive.read(info).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModuleError(f"证据包读不了:{exc}") from exc
    ruleset = loaded["manifest"].get("ruleset_version")
    loaded["ruleset_known"] = ruleset in SUPPORTED_RULESETS
    return loaded
