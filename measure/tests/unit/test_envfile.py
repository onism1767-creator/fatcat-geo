"""进程环境(engine/envfile.py):.env 读入不覆盖已有变量;标准输出被管道接走时改写 UTF-8、接在终端上时不动
(2026-10-27 预览日志窗口中文乱码:Windows 下 Python 对管道默认用 GBK)。零网络零数据库。"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from engine.envfile import load_env_file, utf8_output


class _Tty(io.TextIOWrapper):
    def isatty(self) -> bool:
        return True


def _pipe(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, write_through=True)


def test_piped_output_is_switched_to_utf8_but_a_terminal_is_left_alone(monkeypatch):
    out, err, tty = _pipe("gbk"), _pipe("gbk"), _Tty(io.BytesIO(), encoding="gbk", write_through=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    utf8_output()
    assert out.encoding == "utf-8" and err.encoding == "utf-8"
    out.write("品类核对")
    assert out.buffer.getvalue() == "品类核对".encode()  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stdout", tty)
    utf8_output()
    assert tty.encoding == "gbk"  # 终端由 Python 直接写 Unicode,不改


def test_non_wrapper_streams_are_ignored(monkeypatch):
    """pytest 捕获输出时 sys.stdout 不是 TextIOWrapper:什么都不做,也不报错。"""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    utf8_output()


def test_env_file_fills_missing_variables_only(monkeypatch, tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# 注释\nGEO_TEST_A=1\nGEO_TEST_B = two \nbroken line\n", encoding="utf-8")
    monkeypatch.setenv("GEO_TEST_B", "kept")
    monkeypatch.delenv("GEO_TEST_A", raising=False)
    load_env_file(env)
    import os

    assert os.environ["GEO_TEST_A"] == "1" and os.environ["GEO_TEST_B"] == "kept"
    load_env_file(tmp_path / "missing.env")  # 没有 .env:静默
