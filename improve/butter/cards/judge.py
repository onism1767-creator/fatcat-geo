"""卡片结局与验证状态(纯层)。设计 §8.5 与 §9.4 总表的最后两列。

比对模块(compare/)每条腿每个指标各判一次;这里把一次再测的几条腿合成一个卡片结局,再推出验证状态。
设计没写几条腿(一条腿 = 一种语言 + 一家 AI)怎么合成一个结局。2026-11-18 用户定:卡片上只写一个结论,
每条腿的数照样列在下面;不把几条腿平均成一个数再判(设计 §9.4"各腿各指标分别报,不合成总分"不变)。做法如下:
- 只看主指标(本品牌提及率)的腿;被引率是辅助,照写,不决定结局。
- 有腿超出波动上升、没有腿超出波动下降 → won;反过来 → lost;两种都有 → inconclusive(方向不一致)。
- 没有腿超出:有腿判了"在波动内 / 漂移 / 无有效对照" → inconclusive;全都不可比 → dnf。

验证状态(只有计分的那次再测说了算):
- 出包时"假设";标记部署后"实验中";
- won / lost 且是单条目批次 → "单次观测·变多 / 变少";多条目批次 → 仍为"假设",注明"同批 N 条";
- 在波动内、不可比、无有效对照、漂移 → 回到"假设",原因照实写;
- 没勾选部署的卡 → "假设",注"未部署"。
"""

from __future__ import annotations

from typing import NamedTuple

from butter.cards.machine import Rec
from butter.cards.text import DIRECTION_OF, leg_name
from butter.compare import label_text
from butter.contracts import CardValidation, Direction, Outcome, Reading

MAIN_METRIC = "mention"
BUTTER_PANEL = "butter_generated"


class Judged(NamedTuple):
    outcome: Outcome | None
    reason: str
    won: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()


class Status(NamedTuple):
    validation: CardValidation
    direction: Direction | None
    note: str


def scoring_reading(rec: Rec) -> Reading | None:
    return next((r for r in rec.readings if r.scoring), None)


def deployed_entries(rec: Rec) -> list[str]:
    if rec.deploy is None:
        return []
    return sorted({rec.specs[c].entry_id for c in rec.deploy.card_ids})


def sitewide_deployed(rec: Rec) -> bool:
    """部署的改动里有没有全站类的(还没部署时看全部卡)。有的话,同站没改的页面也不再干净(设计 §8.1)。"""
    cards = rec.deploy.card_ids if rec.deploy else list(rec.specs)
    return any(rec.specs[c].sitewide for c in cards if c in rec.specs)


def butter_panel(rec: Rec) -> bool:
    return any(p.source == BUTTER_PANEL for p in rec.batch.panel)


def judge_reading(reading: Reading) -> Judged:
    """一次再测的几条腿 → 一个结局。"""
    if not reading.legs:
        return Judged(None, "没有改之前的测量,只有当时的情况,没有对比")
    main = [leg for leg in reading.legs if leg.metric == MAIN_METRIC]
    if not main:
        return Judged("dnf", "不可比:没有提及率(主指标)的结果")
    won = tuple(leg_name(leg) for leg in main if leg.outcome == "won" and leg.label == "up" and not leg.no_control)
    lost = tuple(leg_name(leg) for leg in main if leg.outcome == "lost" and leg.label == "down" and not leg.no_control)
    if won and lost:
        return Judged("inconclusive", f"各条腿方向不一致:{len(won)} 条变多,{len(lost)} 条变少", won, lost)
    if won:
        return Judged("won", f"{len(main)} 条腿里有 {len(won)} 条超出波动上升", won, lost)
    if lost:
        return Judged("lost", f"{len(main)} 条腿里有 {len(lost)} 条超出波动下降", won, lost)
    unsure = [leg for leg in main if leg.outcome == "inconclusive"]
    if unsure:
        if all(leg.label == "within" and not leg.no_control for leg in unsure):
            return Judged("inconclusive", "无可辨变化")
        seen: list[str] = []
        for leg in unsure:
            text = label_text(leg)
            if text not in seen:
                seen.append(text)
        return Judged("inconclusive", "、".join(seen))
    stopped = [leg for leg in main if leg.outcome == "dnf"]
    if stopped:
        first = stopped[0]
        return Judged("dnf", label_text(first) + (f"({first.reason})" if first.reason else ""))
    return Judged(None, label_text(main[0]))  # 不可再测 / 未部署:没有结局


def judge_batch(rec: Rec) -> Judged:
    """批次的结局 = 计分那次再测的结局,外加两道保险(总表第 5、7 行排在 9、10 行前面)。"""
    reading = scoring_reading(rec)
    if reading is None or rec.deploy is None:
        return Judged(None, "")
    judged = judge_reading(reading)
    if judged.outcome in ("won", "lost"):
        if not rec.deploy.baseline_valid:
            return Judged("dnf", rec.deploy.baseline_note or "基线无效:只看时序,不作比对")
        if sitewide_deployed(rec):
            return Judged("inconclusive", "这一批含全站类改动,同站没改的页面也受影响,没有有效对照", judged.won,
                          judged.lost)
    return judged


def card_status(rec: Rec, card_id: str) -> tuple[Status, Judged]:
    """一张卡的验证状态。结局照实带回(哪怕状态回到了"假设")。"""
    judged = judge_batch(rec)
    if rec.phase == "已放弃":
        return Status("假设", None, "这一批已放弃"), judged
    if rec.deploy is None:
        return Status("假设", None, ""), Judged(None, "")
    if card_id not in rec.deploy.card_ids:
        return Status("假设", None, "未部署:不参与比对"), Judged(None, "未部署")
    if scoring_reading(rec) is None:
        note = f"已再测 {len(rec.readings)} 次,只作记录" if rec.readings else ""
        return Status("实验中", None, note), judged
    n_cards = len(rec.deploy.card_ids)
    entries = deployed_entries(rec)
    if judged.outcome in ("won", "lost"):
        direction: Direction = DIRECTION_OF[judged.outcome]  # type: ignore[assignment]
        if len(entries) == 1:
            note = f"同批 {n_cards} 条改动都挂这一个条目,合起来算一次" if n_cards > 1 else ""
            return Status("单次观测", direction, note), judged
        return Status("假设", None, f"同批 {n_cards} 条一起改(涉及 {len(entries)} 个条目),不能单独归因;"
                                    f"这一批测到的是{direction}"), judged
    return Status("假设", None, judged.reason), judged
