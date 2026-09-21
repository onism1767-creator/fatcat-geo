"""每家 AI 一句话(v0.4.14):把一家 surface 跨需求的观测压成品牌方能读懂的一句结论。

规则表由用户拍板(2026-09-08),本模块只做查表,不做新观测:
- 「经常 / 有时 / 几乎不」是现有区间三态(PASS / WEAK / FAIL)的直接映射,不引入新判断;
- 「官网有没有被用上」= 检索或引用任一 PASS;两项皆不可观测时记本次看不到;
- 某一项本次没测(没有该场景的需求 / 无可观测项)就在句尾说明,不猜、不补。

本模块不含任何 provider/surface 名字符串;AI 的显示名由报告层代入结论码参数。
"""

from __future__ import annotations

from enum import StrEnum

from diagnosis.findings import StageStatus
from engine.models import FrozenModel, NeedIntent


class Band(StrEnum):
    OFTEN = "OFTEN"
    SOMETIMES = "SOMETIMES"
    RARELY = "RARELY"


_BAND_OF: dict[StageStatus, Band] = {
    StageStatus.PASS: Band.OFTEN,
    StageStatus.WEAK: Band.SOMETIMES,
    StageStatus.FAIL: Band.RARELY,
}
_RANK: dict[Band, int] = {Band.RARELY: 0, Band.SOMETIMES: 1, Band.OFTEN: 2}
PROBE_INTENTS = frozenset({NeedIntent.BRAND_PROBE})


def band_of(status: StageStatus) -> Band | None:
    """区间三态 → 档位;NOT_OBSERVABLE → None。"""
    return _BAND_OF.get(status)


def combine(bands: list[Band]) -> Band | None:
    """同一场景多个需求的合并:平均档位,半档向上;没有观测 → None(本次没测)。"""
    if not bands:
        return None
    rank = int(sum(_RANK[b] for b in bands) / len(bands) + 0.5)
    return next(b for b, r in _RANK.items() if r == rank)


class VerdictInput(FrozenModel):
    """一家 surface 在一个需求上的观测(由执行器按需求汇总)。"""

    intent: NeedIntent
    mention: Band | None  # 回答里提到品牌的档位;无有效样本 → None
    site_used: bool | None  # 官网被用上(检索或引用任一 PASS);两项皆不可观测 → None


class SurfaceVerdict(FrozenModel):
    surface: str
    model: str  # 型号是口径的一部分(v0.4.19):同一 surface 的每个型号各有一句话
    code: str  # verdict.* 结论码,参数 ai 由报告层代入显示名
    missing: list[str]  # verdict.missing.* 结论码


def _lookup(probe: Band | None, organic: Band | None, site: bool | None) -> str:
    if probe is not None and probe is not Band.OFTEN:
        return "verdict.barely_known"  # 连直接问起都介绍不稳,其余无从谈起
    used = bool(site)
    if probe is Band.OFTEN:
        table = {
            Band.OFTEN: ("verdict.known_recommends_cites", "verdict.known_recommends_site_rare"),
            Band.SOMETIMES: ("verdict.known_sometimes_site_used", "verdict.known_sometimes_site_rare"),
            Band.RARELY: ("verdict.only_when_named_site_used", "verdict.only_when_named"),
            None: ("verdict.known_site_used", "verdict.known_site_rare"),
        }
    else:  # 没有「直接问起」的需求:不谈知不知道你
        table = {
            Band.OFTEN: ("verdict.unnamed_often_site_used", "verdict.unnamed_often_site_rare"),
            Band.SOMETIMES: ("verdict.unnamed_sometimes_site_used", "verdict.unnamed_sometimes_site_rare"),
            Band.RARELY: ("verdict.unnamed_rare_site_used", "verdict.unnamed_rare_site_rare"),
            None: ("verdict.no_data", "verdict.no_data"),
        }
    return table[organic][0 if used else 1]


def surface_verdict(surface: str, model: str, inputs: list[VerdictInput]) -> SurfaceVerdict:
    probe = combine([i.mention for i in inputs if i.intent in PROBE_INTENTS and i.mention is not None])
    organic = combine([
        i.mention for i in inputs if i.intent not in PROBE_INTENTS and i.mention is not None
    ])
    site_obs = [i.site_used for i in inputs if i.site_used is not None]
    site: bool | None = any(site_obs) if site_obs else None
    missing = [
        code
        for code, absent in (
            ("verdict.missing.probe", probe is None),
            ("verdict.missing.organic", organic is None),
            ("verdict.missing.site", site is None),
        )
        if absent
    ]
    return SurfaceVerdict(surface=surface, model=model, code=_lookup(probe, organic, site), missing=missing)
