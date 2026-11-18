"""手动核对:各适配器的型号清单 vs 平台「列出型号」接口的返回(12.3:真实网络只在手动脚本里)。

v0.4.19 起每家 AI 的全部型号一起测,「清单齐不齐」成了头号口径风险——型号按月更替,
清单靠人记必然过期。本脚本直接问平台这把 key 能用哪些型号,与适配器声明的清单做差集。
列型号接口不计费。平台没有该接口的(见 _LISTERS 之外的适配器)如实标「无接口,须查官方文档」。

**只报告、不自动入清单**:型号进清单的门槛是真实对照实验(架构书附录 v0.4.6 条目 2、
v0.4.19 条目 2),且返回里混有非对话型号(嵌入 / 语音 / 图像)、同型号的日期快照别名、
深度研究类,取舍是口径决定,不是脚本能替人做的。

用法: .venv\\Scripts\\python scripts\\check_model_coverage.py
key 从 .env / 环境变量读取,只放请求头,不进 URL、不打印。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402


def _anthropic(client: httpx.Client, key: str) -> list[str]:
    resp = client.get("https://api.anthropic.com/v1/models", params={"limit": 1000},
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    resp.raise_for_status()
    return [m["id"] for m in resp.json()["data"]]


def _openai(client: httpx.Client, key: str) -> list[str]:
    resp = client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
    resp.raise_for_status()
    return [m["id"] for m in resp.json()["data"]]


def _gemini(client: httpx.Client, key: str) -> list[str]:
    resp = client.get("https://generativelanguage.googleapis.com/v1beta/models",
                      params={"pageSize": 1000}, headers={"x-goog-api-key": key})
    resp.raise_for_status()
    return [m["name"].removeprefix("models/") for m in resp.json()["models"]]


def _xai(client: httpx.Client, key: str) -> list[str]:
    resp = client.get("https://api.x.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
    resp.raise_for_status()
    return [m["id"] for m in resp.json()["data"]]


# provider → (key 的环境变量, 列型号函数);没列在这里的 provider = 平台无此接口
_LISTERS: dict[str, tuple[str, Callable[[httpx.Client, str], list[str]]]] = {
    "anthropic": ("ANTHROPIC_API_KEY", _anthropic),
    "openai": ("OPENAI_API_KEY", _openai),
    "gemini": ("GOOGLE_API_KEY", _gemini),
    "xai": ("XAI_API_KEY", _xai),
}


def main() -> int:
    load_env_file()
    adapters = build_adapters(os.environ)
    declared: dict[str, set[str]] = {}
    for adapter in adapters.values():
        meta = adapter.meta()
        declared.setdefault(meta.provider, set()).update(meta.models, meta.helper_models)
        adapter.close()

    with httpx.Client(timeout=30.0) as client:
        for provider, models in sorted(declared.items()):
            print(f"\n== {provider} ==  适配器清单 {len(models)} 个:{sorted(models)}")
            if provider not in _LISTERS:
                print("  平台无「列出型号」接口:须人工对照官方文档核对")
                continue
            env_var, lister = _LISTERS[provider]
            try:
                available = lister(client, os.environ[env_var])
            except (httpx.HTTPError, KeyError) as exc:
                print(f"  列型号失败:{type(exc).__name__}: {exc}")
                continue
            print(f"  平台返回 {len(available)} 个型号")
            # 平台常只列带日期的全名(如 xxx-20251001),清单里的短名是它的别名:前缀对得上就算在
            gone = sorted(m for m in models
                          if not any(a == m or a.startswith(f"{m}-") for a in available))
            if gone:
                print(f"  !! 清单里有、平台不返回(已下架,或这把 key 没权限):{gone}")
            print("  平台有、清单里没有(含非对话型号与快照别名,须人工取舍):")
            for model_id in sorted(set(available) - models):
                print(f"    {model_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
