"""卡片上的句子(纯层):把比对结果的一条腿写成一句话。只照实写数,不写"有效""提升了"(设计 §9.6)。"""

from __future__ import annotations

from butter.compare import label_text
from butter.contracts import LegResult

METRIC_TEXT = {"mention": "本品牌提及率", "citation": "官方域名被引率"}
STAGE_TEXT = {"ACCESS": "访问", "RETRIEVAL": "检索", "CITATION": "引用", "REPRESENTATION": "答案呈现"}
DIRECTION_OF = {"won": "变多", "lost": "变少"}


def leg_name(leg: LegResult) -> str:
    return f"{leg.lang} × {leg.provider} × {METRIC_TEXT.get(leg.metric, leg.metric)}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _pp(value: float) -> str:
    return f"{value * 100:+.1f}"


def effect_line(leg: LegResult) -> str:
    """一条腿一句:前 → 后、差多少、区间、不改动时的波动范围、标签。没比成的写原因。"""
    label = label_text(leg)
    why = f"({leg.reason})" if leg.reason else ""
    if leg.d_T is None or leg.pre_T is None or leg.post_T is None:
        return f"{leg_name(leg)}:{label}{why}"
    text = f"{leg_name(leg)}:改前 {_pct(leg.pre_T)} → 改后 {_pct(leg.post_T)},差 {_pp(leg.d_T)} 个百分点"
    if leg.ci_T is not None:
        text += f"(区间 {_pp(leg.ci_T[0])} ~ {_pp(leg.ci_T[1])})"
    if leg.band_T is not None:
        text += f";不改动时通常在 ±{leg.band_T * 100:.1f} 个百分点内"
    if leg.d_C is not None:
        text += f";没改的题同期差 {_pp(leg.d_C)}"
    if leg.did is not None and leg.primary == "did":
        text += f",两者相减 {_pp(leg.did)}"
        if leg.band_did is not None:
            text += f"(通常在 ±{leg.band_did * 100:.1f} 内)"
    return f"{text}。{label}{why}"
