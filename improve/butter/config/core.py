"""默认值表(纯层):按 key 查;波动数据按「语言 × 引擎 × 间隔 × 指标」取档。设计 §9.4、§11。"""

from __future__ import annotations

from typing import Any

from butter.contracts import DefaultValue, Metric, NoiseTier, Thresholds

NOISE_PREFIX = "noise."


class DefaultsError(ValueError):
    pass


def parse_defaults(doc: Any, *, origin: str) -> list[DefaultValue]:
    if not isinstance(doc, dict) or not isinstance(doc.get("defaults"), list):
        raise DefaultsError(f"{origin}:顶层要有 defaults 列表")
    out = []
    for i, item in enumerate(doc["defaults"]):
        try:
            out.append(DefaultValue.model_validate(item))
        except Exception as exc:  # pydantic 的报错原样带上位置
            raise DefaultsError(f"{origin} 第 {i + 1} 条:{exc}") from exc
    return out


def noise_key(metric: str, lang: str, provider: str, days: int) -> str:
    return f"{NOISE_PREFIX}{metric}.{lang}.{provider}.{days}d"


class Defaults:
    """一份冻结的默认值。批次在测基线时把它整份存进规则快照,之后不再读文件。"""

    def __init__(self, values: list[DefaultValue]):
        self._by_key: dict[str, DefaultValue] = {}
        for v in values:
            if v.key in self._by_key:
                raise DefaultsError(f"默认值 key 重复:{v.key}")
            self._by_key[v.key] = v

    def __len__(self) -> int:
        return len(self._by_key)

    def all(self) -> list[DefaultValue]:
        return list(self._by_key.values())

    def entry(self, key: str) -> DefaultValue:
        try:
            return self._by_key[key]
        except KeyError:
            raise DefaultsError(f"没有这个默认值:{key}") from None

    def get(self, key: str) -> Any:
        return self.entry(key).value

    def noise_tiers(self) -> list[NoiseTier]:
        out = []
        for key, v in self._by_key.items():
            if not key.startswith(NOISE_PREFIX):
                continue
            metric, lang, provider, days = key[len(NOISE_PREFIX):].split(".")
            out.append(NoiseTier(metric=metric, lang=lang, provider=provider, days=int(days.rstrip("d")),
                                 sigma=float(v.value["sigma"]), tau=float(v.value["tau"])))
        return out

    def thresholds(self) -> Thresholds:
        g = self.get
        return Thresholds(
            min_treated=g("compare.min_treated_questions"), min_control=g("compare.min_control_questions"),
            min_success_rate=g("compare.min_success_rate"), baseline_valid_days=g("compare.baseline_valid_days"),
            citation_min_searched_share=g("compare.citation_min_searched_share"),
            bootstrap_draws=g("compare.bootstrap_draws"), seed=g("compare.seed"), z=g("compare.z"),
            recommended_gap_days=g("remeasure.interval_days"))


def pick_tier(tiers: list[NoiseTier], metric: Metric, lang: str, provider: str, gap_days: int) -> NoiseTier | None:
    """取不短于实际间隔的最近一档;没有这么长的,取最长的一档(隔 17 天取 21 天档,隔 40 天取 28 天档)。"""
    mine = sorted((t for t in tiers if (t.metric, t.lang, t.provider) == (metric, lang, provider)),
                  key=lambda t: t.days)
    if not mine:
        return None
    for t in mine:
        if t.days >= gap_days:
            return t
    return mine[-1]
