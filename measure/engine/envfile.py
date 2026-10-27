"""进程环境:读取项目根目录的 .env 到进程环境(不覆盖已存在的变量);标准输出被管道接走时改写 UTF-8。

key 只进内存环境变量,不落日志(13.4)。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def utf8_output() -> None:
    """标准输出 / 标准错误被管道接走时(预览窗口、别的程序读日志)一律写 UTF-8。
    Windows 下 Python 对管道默认用系统代码页(中文系统是 GBK),读的一方按 UTF-8 解就成乱码(2026-10-27 预览日志窗口);
    接在终端上时不动——Python 在 Windows 终端直接写 Unicode,本来就对。服务入口一启动就调,赶在第一行日志之前。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def load_env_file(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
