"""进程环境:读取项目根目录的 .env 到进程环境(不覆盖已存在的变量);标准输出被管道接走时改写 UTF-8。

key 只进内存环境变量,不落日志(13.4)。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def utf8_output() -> None:
    """标准输出 / 标准错误被管道接走时(预览窗口、别的程序读日志)一律写 UTF-8。
    Windows 下 Python 对管道默认用系统代码页(中文系统是 GBK),读的一方按 UTF-8 解就成乱码(2026-10-27 预览日志窗口);
    接在终端上时不动——Python 在 Windows 终端直接写 Unicode,本来就对。服务入口一启动就调,赶在第一行日志之前。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def load_env_file(path: Path | None = None) -> None:
    """用 python-dotenv 读:成对的引号去掉,行尾注释、export 前缀按通行写法(用户 2026-10-29 定)。原来自己写的读法
    把 KEY="sk-…" 的引号也当成 key 的一部分,平台拒绝后工具叫人换 key,换了照样失败。
    已有的环境变量不覆盖;值原样取、不展开 ${…}(key 里碰巧有这几个字符也不被改);记事本存的开头 BOM 去掉。"""
    env_path = path or ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False, interpolate=False, encoding="utf-8-sig")
