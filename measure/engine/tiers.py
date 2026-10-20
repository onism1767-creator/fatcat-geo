"""档位(S9.1;架构书附录 v0.4.27 + 2026-10-14 拍板):一套冻结的测量参数——型号怎么选 × 每题几次 × 「提到你」怎么认。

配置在 config/tiers.yaml;新档位 = 加一段配置,本模块与 L2 / L3 其余代码零改动(硬边界 10 同款验收)。
型号按各适配器 meta 的标记选(消费端默认 / 最强,配置资产,来源与日期在适配器里);本模块不含任何厂商名。
档位不是分数:不改判定线、不改口径写法、不改题目;一单一档,中途不换;档位名与参数写进报告「这次是怎么测的」。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

import yaml

from adapters.base import SurfaceAdapter, SurfaceMeta
from engine.models import FrozenModel, Target

DEFAULT_TIERS_PATH = Path(__file__).resolve().parents[1] / "config" / "tiers.yaml"


class ModelRule(StrEnum):
    """档位怎么从每家的清单里挑型号。"""

    CONSUMER_DEFAULT = "consumer_default"
    STRONGEST_AND_CONSUMER_DEFAULT = "strongest_and_consumer_default"
    ALL = "all"


class MentionMethod(StrEnum):
    """「提到你」怎么认(判定器组合);自定义跟起点档走,不单独选(用户定 2026-10-14)。"""

    RULES = "rules"  # 别名表 + 词边界(判定器 v2)
    RULES_FUZZY = "rules_fuzzy"  # + 模糊匹配(S9.2)
    RULES_FUZZY_JUDGE = "rules_fuzzy_judge"  # + AI 裁判(S9.3)


class TierSpec(FrozenModel):
    name: str
    runs_per_prompt: int
    models: ModelRule
    mention_method: MentionMethod


class CustomRuns(FrozenModel):
    min: int
    max: int


class TierChoice(FrozenModel):
    """一单定下的档位参数(确认时冻结,随任务快照与报告采样说明存):
    name = 档位名(自定义时 = 起点档,「提到你」的认法跟它走);custom = 型号或次数改过,报告写实际参数、不冠档位名。"""

    name: str
    runs_per_prompt: int
    mention_method: MentionMethod
    custom: bool


class TierConfig(FrozenModel):
    default: str
    custom_runs: CustomRuns
    tiers: list[TierSpec]

    def get(self, name: str) -> TierSpec | None:
        return next((t for t in self.tiers if t.name == name), None)

    @property
    def default_tier(self) -> TierSpec:
        spec = self.get(self.default)
        if spec is None:  # load_tiers 已校验;直接构造的配置漏了也要明确报错,不静默
            raise ValueError(f"默认档位 {self.default!r} 不在清单里")
        return spec

    def allows_runs(self, n: int) -> bool:
        return self.custom_runs.min <= n <= self.custom_runs.max


def load_tiers(path: Path = DEFAULT_TIERS_PATH) -> TierConfig:
    """读档位配置;配置自相矛盾(默认档不存在、重名、次数越界)启动时直接炸,不带病运行。"""
    config = TierConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    names = [t.name for t in config.tiers]
    if len(set(names)) != len(names):
        raise ValueError(f"档位重名:{names}")
    if config.get(config.default) is None:
        raise ValueError(f"默认档位 {config.default!r} 不在清单 {names} 里")
    if config.custom_runs.min < 1 or config.custom_runs.min > config.custom_runs.max:
        raise ValueError(f"自定义次数范围不合法:{config.custom_runs}")
    for tier in config.tiers:
        if not config.allows_runs(tier.runs_per_prompt):
            raise ValueError(f"档位 {tier.name} 的每题次数 {tier.runs_per_prompt} 超出自定义范围 {config.custom_runs}")
    return config


def tier_targets(tier: TierSpec, adapters: Mapping[str, SurfaceAdapter]) -> list[Target]:
    """一档要测的对象:按仪器名排序,每家按档位的挑法从清单里取型号。"""
    return [
        Target(surface=name, model=model)
        for name in sorted(adapters)
        for model in pick_models(tier.models, adapters[name].meta())
    ]


def pick_models(rule: ModelRule, meta: SurfaceMeta) -> list[str]:
    if rule is ModelRule.ALL:
        return list(meta.models)
    if rule is ModelRule.CONSUMER_DEFAULT:
        return [meta.consumer_default_model]
    picked = [meta.strongest_model]
    if meta.consumer_default_model != meta.strongest_model:
        picked.append(meta.consumer_default_model)
    else:  # 同一个型号只算一个,再补清单里紧挨着它的下一个(它已是最后一个就补前面那个;只有一个就不补)
        i = meta.models.index(meta.strongest_model)
        neighbour = meta.models[i + 1] if i + 1 < len(meta.models) else (meta.models[i - 1] if i > 0 else None)
        if neighbour is not None:
            picked.append(neighbour)
    return picked
