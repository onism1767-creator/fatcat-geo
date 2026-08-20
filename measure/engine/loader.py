"""配置驱动装载(6.6):扫描 surfaces.yaml,实例化启用的适配器。

新 surface = 一个适配器文件 + 配置一行,本模块与 L2/L3 其余代码零改动
(硬边界 10 的执行机构)。本模块源码无任何 provider 名——适配器类的
点路径与 key 环境变量名全部来自配置,运行时动态装载。

一致性防线:配置里的能力声明必须与适配器 meta() 完全一致,
不一致直接抛错——两处真相不允许漂移。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Mapping

import structlog
import yaml

from adapters.base import CapabilityRegistry, SurfaceAdapter
from engine.models import FrozenModel

log = structlog.get_logger("engine.loader")

DEFAULT_SURFACES_PATH = Path(__file__).resolve().parents[1] / "config" / "surfaces.yaml"


class SurfaceEntry(FrozenModel):
    name: str
    provider: str
    adapter: str  # 适配器类的点路径
    api_key_env: str
    enabled: bool
    capabilities: dict[str, bool]


def load_surface_registry(config_path: Path = DEFAULT_SURFACES_PATH) -> list[SurfaceEntry]:
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return [SurfaceEntry(**entry) for entry in data["surfaces"]]


def build_adapters(
    env: Mapping[str, str],
    config_path: Path = DEFAULT_SURFACES_PATH,
) -> dict[str, SurfaceAdapter]:
    """实例化全部 enabled 的适配器,返回 {surface 名: 适配器}。

    - key 缺失:跳过该 surface 并告警(其余 surface 照常,6.6);
    - 配置与适配器 meta 不一致(surface/provider/能力):抛 ValueError,
      这是配置或代码错误,必须在启动时炸掉,不允许带病运行。
    """
    adapters: dict[str, SurfaceAdapter] = {}
    for entry in load_surface_registry(config_path):
        if not entry.enabled:
            continue
        api_key = env.get(entry.api_key_env, "")
        if not api_key:
            log.warning("surface_skipped_missing_key",
                        surface=entry.name, env_var=entry.api_key_env)
            continue
        module_path, _, class_name = entry.adapter.rpartition(".")
        adapter_cls = getattr(importlib.import_module(module_path), class_name)
        adapter: SurfaceAdapter = adapter_cls(api_key=api_key)

        meta = adapter.meta()
        if (meta.surface, meta.provider) != (entry.name, entry.provider):
            raise ValueError(
                f"配置与适配器身份不一致:配置 ({entry.provider}/{entry.name}) vs "
                f"meta ({meta.provider}/{meta.surface})"
            )
        declared = CapabilityRegistry(**entry.capabilities)
        if meta.capabilities != declared:
            raise ValueError(
                f"{entry.name} 能力声明漂移:配置 {declared} vs 适配器 {meta.capabilities}。"
                "两处必须一致,改其一须同步另一处并过契约测试"
            )
        adapters[entry.name] = adapter
        log.info("surface_loaded", surface=entry.name, provider=entry.provider)
    return adapters
