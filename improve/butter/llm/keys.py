"""密钥只从环境变量来(决定 6)。本模块是全仓库唯一知道环境变量名的地方。

可以从用户指定的 .env 文件把值读进一个字典(只读,不写文件,不打印,不进日志、缓存或报错信息)。
变量名与 Potato、Toast、E0 用的一致,站长不用再配一遍。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_NAMES: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY",),  # 只用来测量;起草不用(价格表里没有它)
}


def read_env_file(path: Path) -> dict[str, str]:
    """读 .env 里的 KEY=VALUE 行。只返回本模块认识的那几个变量,其他一概不碰。"""
    wanted = {name for names in ENV_NAMES.values() for name in names}
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        if name in wanted:
            out[name] = value.strip().strip("'\"")
    return out


def secret_for(provider: str, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    for name in ENV_NAMES.get(provider, ()):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def available_providers(env: Mapping[str, str] | None = None) -> list[str]:
    return [p for p in ENV_NAMES if secret_for(p, env)]


def child_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """给子进程(Potato、测量驱动)用的环境:系统环境 + 各家的 key。

    Potato 开跑前的检查只认 GEMINI 那个变量名,所以只有 GOOGLE 那个时补一份(E0 的跑批脚本也是这样做的)。
    """
    out = dict(os.environ)
    for provider, names in ENV_NAMES.items():
        secret = secret_for(provider, env)
        if secret:
            out[names[0]] = secret
    return out
