"""每家 AI 一句话(v0.4.14):把一家 surface 跨题型的观测压成品牌方能读懂的一句结论。

规则表由用户拍板(2026-09-08),本模块只做查表,不做新观测:
- 「经常 / 有时 / 几乎不」是比例三态(PASS / WEAK / FAIL)的直接映射,不引入新判断(S2:看比例,线不变);
- 「官网有没有被用上」= 检索或引用任一 PASS;两项皆不可观测时记本次看不到;
- 某一项本次没测(没有该题型的题 / 无可观测项)就在句尾说明,不猜、不补。

本模块不含任何 provider/surface 名字符串;AI 的显示名由报告层代入结论码参数。
"""

from __future__ import annotations

from enum import StrEnum

from diagnosis.findings import DiagnosisThresholds, StageFinding, StageStatus, rate_status
from engine.aggregate import ClassSurfaceMetric
from engine.models import FrozenModel, PromptIntent


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
PROBE_INTENTS = frozenset({PromptIntent.BRAND_FACT})  # 点名焦点品牌的题型


def band_of(status: StageStatus) -> Band | None:
    """比例三态 → 档位;NOT_OBSERVABLE → None。"""
    return _BAND_OF.get(status)


def combine(bands: list[Band]) -> Band | None:
    """同一类题型多个观测的合并:平均档位,半档向上;没有观测 → None(本次没测)。"""
    if not bands:
        return None
    rank = int(sum(_RANK[b] for b in bands) / len(bands) + 0.5)
    return next(b for b, r in _RANK.items() if r == rank)


class VerdictInput(FrozenModel):
    """一家 surface 在一个题型上的观测(由执行器按题型汇总)。"""

    intent: PromptIntent
    mention: Band | None  # 回答里提到品牌的档位;无有效样本 → None
    site_used: bool | None  # 官网被用上(检索或引用任一 PASS);两项皆不可观测 → None
    third_party: Band | None = None  # 写到你的第三方网页被引用的档位(S6.1);看不到 → None
    # 失败 / 拒答太多(7.3 LOW_CONFIDENCE):提及档位不进一句话,句尾说明——降权后的「有时」不能被说成
    # 「它不够了解你」那种最重的定论(审核 2026-10-27 黄 13)
    low_confidence: bool = False


def verdict_input(
    intent: PromptIntent,
    mention: ClassSurfaceMetric,
    retrieval: StageFinding,
    citation: StageFinding,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
    third_party_cited: StageFinding | None = None,
) -> VerdictInput:
    """一个被测对象在一个题型上的一句话原料:提及档位 = 提及比例三态的直接映射;
    官网被用上 = 检索或引用任一 PASS(两项皆不可观测 → None)。"""
    mention_band = None
    if mention.rate is not None:
        status, _ = rate_status(
            mention.rate,
            low_confidence=mention.accounting.low_confidence,
            fail_below=thresholds.mention_fail_below,
            pass_from=thresholds.mention_pass_from,
        )
        mention_band = band_of(status)
    site = [f for f in (retrieval, citation) if f.status is not StageStatus.NOT_OBSERVABLE]
    return VerdictInput(
        intent=intent,
        mention=mention_band,
        site_used=any(f.status is StageStatus.PASS for f in site) if site else None,
        third_party=band_of(third_party_cited.status) if third_party_cited is not None else None,
        low_confidence=mention.rate is not None and mention.accounting.low_confidence,
    )


class SurfaceVerdict(FrozenModel):
    surface: str
    model: str  # 型号是口径的一部分(v0.4.19):同一 surface 的每个型号各有一句话
    code: str  # verdict.* 结论码,参数 ai 由报告层代入显示名
    missing: list[str]  # verdict.missing.* 结论码
    third_party_code: str | None = None  # 第三方线一句(S6.1,措辞待用户逐行拍板);看不到 → None


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
    else:  # 没有「直接问起」的题:不谈知不知道你
        table = {
            Band.OFTEN: ("verdict.unnamed_often_site_used", "verdict.unnamed_often_site_rare"),
            Band.SOMETIMES: ("verdict.unnamed_sometimes_site_used", "verdict.unnamed_sometimes_site_rare"),
            Band.RARELY: ("verdict.unnamed_rare_site_used", "verdict.unnamed_rare_site_rare"),
            None: ("verdict.no_data", "verdict.no_data"),
        }
    return table[organic][0 if used else 1]


def surface_verdict(surface: str, model: str, inputs: list[VerdictInput]) -> SurfaceVerdict:
    usable = [i for i in inputs if not i.low_confidence]  # 失败 / 拒答太多的题型不进档位,句尾说明
    probe = combine([i.mention for i in usable if i.intent in PROBE_INTENTS and i.mention is not None])
    organic = combine([
        i.mention for i in usable if i.intent not in PROBE_INTENTS and i.mention is not None
    ])
    site_obs = [i.site_used for i in inputs if i.site_used is not None]
    site: bool | None = any(site_obs) if site_obs else None
    third_party = combine([i.third_party for i in inputs if i.third_party is not None])
    missing = [
        code
        for code, absent in (
            ("verdict.missing.probe", probe is None),
            ("verdict.missing.organic", organic is None),
            ("verdict.missing.site", site is None),
            ("verdict.missing.thirdparty", third_party is None),
            ("verdict.missing.low_confidence", any(i.low_confidence and i.mention is not None for i in inputs)),
        )
        if absent
    ]
    return SurfaceVerdict(
        surface=surface, model=model, code=_lookup(probe, organic, site), missing=missing,
        third_party_code=_THIRD_PARTY_CODE[third_party] if third_party is not None else None,
    )


_THIRD_PARTY_CODE: dict[Band, str] = {
    Band.OFTEN: "verdict.thirdparty.often",
    Band.SOMETIMES: "verdict.thirdparty.sometimes",
    Band.RARELY: "verdict.thirdparty.rarely",
}
