"""方法(注册表条目)级汇总(纯层)。设计 §8.5:方法的验证状态由卡汇总,变多、变少分开数,不互相抵消。

- 某个方向有一张"单次观测"的卡 → 该方向"单次观测";
- 同一方向至少两次独立实验 → 该方向"已复现"(变多 → 升级提案;变少 → 降级提案,都由人工合并);
- 在多个行业的站点上同向复现 → "可泛化"。

"独立实验"怎么数(产品文档 §15.4 原文只有一句"不同时间或不同站点",这里取最保守的读法):
- 一次实验 = 一个单条目批次的那次计分再测。同批几张卡算一次,不重复数。
- 不同站点的批次,算独立。
- 同一个站点的两个批次,测量时间段(基线日 ~ 计分再测日)不重叠才算独立;重叠的照实列出,不另计
  (2026-11-18 用户定:只算一次)。
- 多条目批次不计数,但照实列出。
- 题是 Butter 自己出的实验,照常算一次(2026-11-18 用户定);提案的表里标出题从哪来,不另加提醒。
- 站长放弃的批次不计数,也不列出(2026-11-18 用户定:"放弃了不计,也不写");事件日志里仍有记录。
- "可泛化":独立实验 ≥ 2 次,且来自 ≥ 2 个不同的行业(行业取事实库的类别;没填行业的不算)。
"""

from __future__ import annotations

from butter.cards.judge import butter_panel, card_status, deployed_entries, judge_batch, scoring_reading
from butter.cards.machine import Ledger, Rec
from butter.cards.text import DIRECTION_OF
from butter.contracts import Direction, MethodDirection, MethodExperiment, MethodSummary, MethodValidation


def _experiment(rec: Rec) -> MethodExperiment | None:
    """这一批算不算一次能计数的实验:没放弃、单条目、计分再测判了变多或变少。"""
    if rec.phase == "已放弃" or rec.deploy is None or not rec.baselines:
        return None
    reading = scoring_reading(rec)
    judged = judge_batch(rec)
    if reading is None or judged.outcome not in ("won", "lost") or len(deployed_entries(rec)) != 1:
        return None
    cards = [rec.specs[c] for c in rec.deploy.card_ids]
    industry = next((c.industry for c in cards if c.industry), "")
    return MethodExperiment(
        batch_id=rec.batch.batch_id, site_host=rec.batch.site_host, industry=industry,
        direction=DIRECTION_OF[judged.outcome], baseline_day=rec.baselines[-1].day, remeasure_day=reading.day,
        card_ids=list(rec.deploy.card_ids), butter_panel=butter_panel(rec),
        legs=list(judged.won if judged.outcome == "won" else judged.lost))


def split_independent(experiments: list[MethodExperiment]) -> tuple[list[MethodExperiment], list[MethodExperiment]]:
    """同站且测量时间段重叠的只算一次:每个站按再测日先后取,和已取的不重叠才计入。"""
    kept: list[MethodExperiment] = []
    extra: list[MethodExperiment] = []
    last_by_site: dict[str, MethodExperiment] = {}
    for exp in sorted(experiments, key=lambda e: (e.remeasure_day, e.baseline_day, e.batch_id)):
        last = last_by_site.get(exp.site_host)
        if last is not None and exp.baseline_day <= last.remeasure_day:
            extra.append(exp.model_copy(update={"note": f"和 {last.batch_id} 同站且测量时间重叠,不另计"}))
            continue
        kept.append(exp)
        last_by_site[exp.site_host] = exp
    return kept, extra


def _state(independent: list[MethodExperiment]) -> MethodValidation | None:
    if not independent:
        return None
    if len(independent) == 1:
        return "单次观测"
    industries = {e.industry for e in independent if e.industry}
    return "可泛化" if len(industries) >= 2 else "已复现"


def _direction(direction: Direction, experiments: list[MethodExperiment], cards: int) -> MethodDirection:
    kept, extra = split_independent([e for e in experiments if e.direction == direction])
    return MethodDirection(direction=direction, state=_state(kept), independent=kept, not_independent=extra,
                           cards=cards)


def summarize_methods(ledger: Ledger) -> list[MethodSummary]:
    """按证据条目 ID 汇总各卡。条目按 ID 排序,结果与事件先后无关的部分保持稳定。"""
    entries: dict[str, dict] = {}

    def slot(entry_id: str) -> dict:
        return entries.setdefault(entry_id, {
            "total": 0, "hypothesis": 0, "running": 0, "more": 0, "fewer": 0,
            "same_more": [], "same_fewer": [], "experiments": []})

    for rec in ledger.batches.values():
        if rec.phase == "已放弃":
            continue  # 放弃了不计,也不写
        judged = judge_batch(rec)
        multi = len(deployed_entries(rec)) > 1
        for spec in rec.specs.values():
            tally = slot(spec.entry_id)
            tally["total"] += 1
            status, _ = card_status(rec, spec.card_id)
            deployed = rec.deploy is not None and spec.card_id in rec.deploy.card_ids
            if status.validation == "单次观测":
                tally["more" if status.direction == "变多" else "fewer"] += 1
            elif status.validation == "实验中":
                tally["running"] += 1
            else:
                tally["hypothesis"] += 1
                if deployed and multi and judged.outcome in ("won", "lost"):
                    tally["same_more" if judged.outcome == "won" else "same_fewer"].append(spec.card_id)
        experiment = _experiment(rec)
        if experiment is not None:
            slot(deployed_entries(rec)[0])["experiments"].append(experiment)

    out = []
    for entry_id in sorted(entries):
        t = entries[entry_id]
        out.append(MethodSummary(
            entry_id=entry_id, more=_direction("变多", t["experiments"], t["more"]),
            fewer=_direction("变少", t["experiments"], t["fewer"]), cards_total=t["total"],
            cards_hypothesis=t["hypothesis"], cards_running=t["running"],
            same_batch_more=t["same_more"], same_batch_fewer=t["same_fewer"]))
    return out
