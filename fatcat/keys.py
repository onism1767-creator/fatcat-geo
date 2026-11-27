"""AI key 只填一次的那一处。

key 从两个地方来,先后是:页面上填的(只放在这个进程的内存里,入口一关就没了) > 本项目文件夹下的 `.env`。
这里只做两件事:说得出哪几家有 key;启动工具时把 key 当环境变量递过去。
key 的值不打印、不进日志、不进任何页面、不写文件;对外只给「有 / 没有、从哪来」。

各家用哪个变量名,以统一型号清单为准(`catalog/models.json`,读自测量工具的适配器声明)。
Gemini 的变量名三个项目写法不一样:递过去时两个名字一起给,读 `.env` 时两个名字都认。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from fatcat.modules import ROOT, ModuleError

ENV_FILE = ROOT / ".env"
# 同一个 key 的另一种变量名:左边是统一清单的写法,右边是别的项目认的写法
ALIASES: dict[str, tuple[str, ...]] = {"GOOGLE_API_KEY": ("GEMINI_API_KEY",)}
MAX_KEY_LENGTH = 400

FROM_PAGE = "page"
FROM_FILE = "file"


class KeyStore:
    """names:服务商 → 环境变量名(统一清单的写法)。env_file:本项目的 `.env`;None 就是不读文件。"""

    def __init__(self, names: Mapping[str, str], env_file: Path | None = ENV_FILE) -> None:
        self._names = dict(names)
        self._file = env_file
        self._typed: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def providers(self) -> list[str]:
        return sorted(self._names)

    @property
    def names(self) -> dict[str, str]:
        """服务商 → 变量名。写 `.env` 时照这个名字写。"""
        return dict(self._names)

    def remember(self, provider: str, value: str) -> None:
        """记下页面上填的一个 key。只放内存。空的不动原来的;样子不对的不收。"""
        if provider not in self._names:
            raise ModuleError(f"不认识 {provider} 这一家")
        value = value.strip()
        if not value:
            return
        if len(value) > MAX_KEY_LENGTH or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in value):
            # 话里不带填的内容:它可能就是半个 key
            raise ModuleError(f"{provider} 填的不像一个 key(里面有空白或别的符号,或者太长),没有记下")
        with self._lock:
            self._typed[provider] = value

    def forget(self, provider: str) -> None:
        with self._lock:
            self._typed.pop(provider, None)

    def _from_file(self) -> dict[str, str]:
        if self._file is None or not self._file.is_file():
            return {}
        # 和测量工具读它自己那份的规矩一样:成对的引号去掉、不展开 ${…}、记事本存的开头 BOM 去掉
        raw = dotenv_values(self._file, interpolate=False, encoding="utf-8-sig")
        found: dict[str, str] = {}
        for provider, name in self._names.items():
            for candidate in (name, *ALIASES.get(name, ())):
                value = (raw.get(candidate) or "").strip()
                if value:
                    found[provider] = value
                    break
        return found

    def where(self) -> dict[str, str | None]:
        """每一家的 key 从哪来:页面上填的、`.env` 里的,或者这里没有(None)。只说来处,不给值。"""
        in_file = self._from_file()
        with self._lock:
            typed = set(self._typed)
        return {provider: FROM_PAGE if provider in typed else FROM_FILE if provider in in_file else None
                for provider in self.providers}

    def child_env(self) -> dict[str, str]:
        """启动工具时加进它环境里的变量。只有这里有的才给;没有的那几家,工具照旧看它自己的配置。"""
        values = self._from_file()
        with self._lock:
            values.update(self._typed)
        env: dict[str, str] = {}
        for provider, value in values.items():
            name = self._names[provider]
            for each in (name, *ALIASES.get(name, ())):
                env[each] = value
        return env

    def __repr__(self) -> str:  # 调试时打印出来也不带值
        return f"KeyStore(providers={self.providers})"
