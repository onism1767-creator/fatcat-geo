"""找到三个项目和 Web Lite 在哪(modules.toml)。只读,不往那些文件夹里写。"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODULES_FILE = ROOT / "modules.toml"


class ModuleError(RuntimeError):
    """项目找不到、解释器找不到,或者读它的时候出了错。"""


@dataclass(frozen=True)
class Module:
    name: str
    role: str
    path: Path
    python: Path | None  # 它自己的解释器;没有就是 None
    url: str = ""  # 它的本机网页地址(有网页的才填)
    # 下面三项只有「自带数据库」的装法(免安装包)才填:数据库的地址、PostgreSQL 的程序文件夹、这个包自己的数据目录
    db_url: str = ""
    pg_bin: Path | None = None
    pg_data: Path | None = None

    def require_path(self) -> Path:
        if not self.path.is_dir():
            raise ModuleError(f"找不到 {self.name} 的文件夹:{self.path}(位置写在 modules.toml)")
        return self.path

    def require_python(self) -> Path:
        self.require_path()
        if self.python is None or not self.python.is_file():
            raise ModuleError(f"找不到 {self.name} 自己的 Python:{self.python}(位置写在 modules.toml)")
        return self.python

    def commit(self) -> str | None:
        """它现在停在哪个提交上;有没提交的改动就在后面加 +。不是 git 仓库返回 None。"""
        try:
            head = subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(self.path), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return head + ("+" if dirty else "")


_JSON_LINE = "FATCAT_JSON "
_ERROR_LINE = "FATCAT_ERROR "


def run_readonly(module: Module, args: list[str], *, pythonpath: Path | None = None,
                 env: dict[str, str] | None = None, timeout: int = 180) -> Any:
    """用这个项目自己的解释器跑一段只读的代码,取回它打印的那一行 JSON。

    args 是解释器后面的参数(`-c 代码` 或 `脚本路径 参数…`)。-B 和 PYTHONDONTWRITEBYTECODE:不在它的文件夹里留 .pyc。
    被跑的代码用 `FATCAT_JSON <json>` 交结果,用 `FATCAT_ERROR <一句话>` 说它做不了。
    """
    full_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", **(env or {}))
    if module.db_url:  # 自带数据库的装法:读的也是那一个库
        full_env["GEO_DATABASE_URL"] = module.db_url
    if pythonpath is not None:
        full_env["PYTHONPATH"] = str(pythonpath)
    try:
        done = subprocess.run(
            [str(module.require_python()), "-B", *args], cwd=module.require_path(), env=full_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleError(f"读 {module.name} 时没跑起来:{exc}") from exc
    for line in done.stdout.splitlines():
        if line.startswith(_JSON_LINE):
            return json.loads(line[len(_JSON_LINE):])
        if line.startswith(_ERROR_LINE):
            raise ModuleError(line[len(_ERROR_LINE):])
    tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
    raise ModuleError(f"读 {module.name} 失败(退出码 {done.returncode}):" + " / ".join(tail))


def load_modules(file: Path = MODULES_FILE) -> dict[str, Module]:
    data = tomllib.loads(file.read_text(encoding="utf-8"))
    modules: dict[str, Module] = {}
    for name, entry in data["modules"].items():
        path = (file.parent / entry["path"]).resolve()
        python = entry.get("python") or ""
        modules[name] = Module(
            name=name,
            role=entry.get("role", ""),
            path=path,
            python=(path / python) if python else None,
            url=str(entry.get("url", "")).rstrip("/"),
            db_url=str(entry.get("db_url", "")),
            pg_bin=(file.parent / entry["pg_bin"]).resolve() if entry.get("pg_bin") else None,
            pg_data=(file.parent / entry["pg_data"]).resolve() if entry.get("pg_data") else None,
        )
    return modules
