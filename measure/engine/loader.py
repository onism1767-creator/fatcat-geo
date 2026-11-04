"""配置驱动装载(6.6):扫描 surfaces.yaml,实例化启用的适配器。

新 surface = 一个适配器文件 + 配置一行,本模块与 L2/L3 其余代码零改动
(硬边界 10 的执行机构)。本模块源码无任何 provider 名——适配器类的
点路径与 key 环境变量名全部来自配置,运行时动态装载。

一致性防线:配置里的能力声明必须与适配器 meta() 完全一致,
不一致直接抛错——两处真相不允许漂移。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path

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
    # 这家厂商 / 仪器的别名(产品名、型号系列的常用叫法):只给禁厂商名测试当禁用词(用户 2026-10-28 定扩名单);
    # 名字本身只许写在 config/ 与 adapters/,这里连例子都不能举(硬边界 2)
    aliases: list[str] = []
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
        if declared.supports_search_disabled and meta.est_cost_per_call_search_disabled_usd is None:
            raise ValueError(
                f"{entry.name} 声明了 supports_search_disabled 但未报闭卷单价"
                "(est_cost_per_call_search_disabled_usd)——预估会静默漏计,禁止带病运行"
            )
        # 型号是口径的一部分(v0.4.19):每个型号各测各的,报价必须逐型号全覆盖
        for label, prices in (("est_cost_per_call_usd", meta.est_cost_per_call_usd),
                              ("est_cost_per_call_search_disabled_usd",
                               meta.est_cost_per_call_search_disabled_usd)):
            if prices is not None and set(prices) != set(meta.models):
                raise ValueError(
                    f"{entry.name} 的 {label} 与型号清单不一致:报价 {sorted(prices)} vs "
                    f"型号 {sorted(meta.models)}——漏报的型号预估会静默漏计,禁止带病运行"
                )
        # 牌价表(成本调研②):确认页给没实测过的型号估价靠它,漏一个型号就静默退回声明价
        missing_prices = [m for m in (*meta.models, *meta.helper_models) if m not in meta.price_table]
        if missing_prices:
            raise ValueError(f"{entry.name} 的牌价表(price_table)漏了型号 {missing_prices}"
                             "——预估会静默漏计,禁止带病运行")
        if meta.default_model not in meta.models:
            raise ValueError(f"{entry.name} 的默认型号 {meta.default_model!r} 不在型号清单里")
        for label, tagged in (("消费端默认", meta.consumer_default_model), ("最强", meta.strongest_model)):
            if tagged not in meta.models:  # 档位按标记选型号(S9):标记指向清单外的型号 = 配置资产坏了,禁止带病运行
                raise ValueError(f"{entry.name} 的「{label}」型号 {tagged!r} 不在型号清单里")
        adapters[entry.name] = adapter
        log.info("surface_loaded", surface=entry.name, provider=entry.provider)
    return adapters
