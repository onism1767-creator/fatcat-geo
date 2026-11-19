"""批次与实验卡(设计 §8、§9.4 总表、ADR-B02、ADR-B06):合法与非法转换、验证状态每一行、方法汇总、日志与索引。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from butter import cards, store
from butter.cards.machine import OWNER_ONLY, Ledger, apply, decide, draft, fold
from butter.cards.project import project
from butter.compare import compare_batch, plan_rules
from butter.config import load_defaults
from butter.contracts import (
    Batch, BatchFacts, BatchFreeze, BatchPlan, CardEvent, CardSpec, CompareResult, DeployMark, FrozenBrands,
    FrozenPanel, LegData, LegSample, MeasureDone, MeasureFailed, MeasureStart, OwnerNote, QuestionObs, Surface,
)
from butter.registry import ids, load_registry

DEFAULTS = load_defaults()
PKG_ROOT = Path(__file__).resolve().parents[1]
ZH = ("zh", "anthropic", "mention")
EN = ("en", "openai", "mention")
ZH_CITE = ("zh", "anthropic", "citation")
TITLE = ids.TITLE_QUERY_MATCH
PRICE = ids.PRICE_SPECS_COMPARISON
FLAT = [(1, 1)] * 12
UP = [(0, 3)] * 12
DOWN = [(3, 0)] * 12
BASE_DAY, DEPLOY_DAY, RE_DAY = "2026-11-01", "2026-11-03", "2026-12-02"  # 再测在部署后 29 天,过了建议间隔 28 天


def dt(day: str, hour: int = 9) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


def freeze(n_t: int = 12, n_c: int = 12, legs=(ZH,), source: str = "potato_file") -> BatchFreeze:
    surfaces = list(dict.fromkeys((lang, provider) for lang, provider, _ in legs))
    return BatchFreeze(
        panel=[FrozenPanel(lang="zh", path="panel/zh/questions.yml", prompt_version="panel-v1", sha256="a" * 64,
                           source=source)],
        brands=[FrozenBrands(lang="zh", path="panel/zh/brands.yml", sha256="b" * 64)],
        surfaces=[Surface(lang=lang, provider=provider, model=f"{provider}-model") for lang, provider in surfaces],
        plan=BatchPlan(runs_per_question=3), treated_q=[f"t{i}" for i in range(n_t)],
        control_q=[f"c{i}" for i in range(n_c)], question_pages={f"c{i}": "https://a.test/other" for i in range(n_c)},
        rules=plan_rules(DEFAULTS, list(legs), n_t, n_c))


def samples(legs=(ZH,), ok: int = 72, expected: int = 72) -> list[LegSample]:
    seen = list(dict.fromkeys((lang, provider) for lang, provider, _ in legs))
    return [LegSample(lang=lang, provider=provider, model=f"{provider}-model", tool_version="aivis 0.9.1",
                      config_fingerprint="fp1", ok=ok, expected=expected, n_questions=12, runs_per_question=3,
                      mention_rate=0.25) for lang, provider in seen]


def spec(card_id: str, batch: str, entry: str = TITLE, **kw) -> CardSpec:
    base = dict(card_id=card_id, batch_id=batch, change_item_id=card_id.split("/")[-1], entry_id=entry,
                kind="heading_suggestion", target="https://a.test/products/classic 的标题", before="Classic",
                after="Classic 电热水壶多少钱", industry="kettles", entry_stage="RETRIEVAL", entry_kind="geo_method",
                entry_level="B", entry_hypothesis="标题复述问题,可能更容易被检索到", entry_sources=["What Gets Cited"])
    return CardSpec(**(base | kw))


def compare(batch: str, rules, pairs=None, *, per_leg: dict | None = None, baseline: str = BASE_DAY,
            deploy: str = DEPLOY_DAY, remeasure: str = RE_DAY, **facts_kw) -> CompareResult:
    """用真的比对模块出结果。pairs = (处理组前后, 对照组前后);per_leg 给每条腿各自的。"""
    per_leg = per_leg or {ZH: pairs or (UP, FLAT)}
    n_t = max(len(t) for t, _ in per_leg.values())
    n_c = max(len(c) for _, c in per_leg.values())
    facts = BatchFacts(batch_id=batch, treated_q=[f"t{i}" for i in range(n_t)], control_q=[f"c{i}" for i in range(n_c)],
                       baseline_day=baseline, deploy_day=deploy, remeasure_day=remeasure, **facts_kw)
    data = []
    for (lang, provider, metric), (t, c) in per_leg.items():
        qs = [QuestionObs(question_id=f"t{i}", pre_hits=a, pre_n=3, post_hits=b, post_n=3) for i, (a, b) in enumerate(t)]
        qs += [QuestionObs(question_id=f"c{i}", pre_hits=a, pre_n=3, post_hits=b, post_n=3) for i, (a, b) in enumerate(c)]
        data.append(LegData(lang=lang, provider=provider, metric=metric, questions=qs, pre_ok=len(qs) * 3,
                            pre_expected=len(qs) * 3, post_ok=len(qs) * 3, post_expected=len(qs) * 3,
                            pre_searched_share=0.9))
    return compare_batch(rules, facts, data)


class Sim:
    """不落盘的小演练:起事件 → 判定 → 合规才记进账本(和 service.submit 走同一条路,只是不写文件)。"""

    def __init__(self) -> None:
        self.ledger = Ledger()
        self.clock = dt("2026-10-31")

    def do(self, kind, payload=None, *, batch="b1", card=None, actor=None, at=None):
        self.clock = at or self.clock + timedelta(minutes=1)
        event = draft(self.ledger, at=self.clock, actor=actor or ("owner" if kind in OWNER_ONLY else "system"),
                      batch_id=batch, type=kind, payload=payload, card_id=card)
        decision = decide(self.ledger, event)
        if decision.ok:
            apply(self.ledger, event)
        return decision

    def ok(self, kind, payload=None, **kw):
        decision = self.do(kind, payload, **kw)
        assert decision.ok, decision.reason
        return decision

    def no(self, kind, payload=None, **kw) -> str:
        before = snapshot(self.ledger)
        decision = self.do(kind, payload, **kw)
        assert not decision.ok and decision.reason and decision.event is None
        assert snapshot(self.ledger) == before, "被拒绝的动作不该改动任何状态"
        return decision.reason

    def view(self, as_of: date | None = None):
        return project(self.ledger, as_of=as_of)

    def card(self, card_id: str):
        return next(c for c in self.view().cards if c.card_id == card_id)

    def batch(self, batch: str = "b1", as_of: date | None = None):
        return next(b for b in self.view(as_of).batches if b.batch.batch_id == batch)

    def rules(self, batch: str = "b1"):
        return self.ledger.batches[batch].batch.rules


def snapshot(ledger: Ledger) -> str:
    return project(ledger).model_dump_json()


def open_batch(sim: Sim, batch="b1", entries=(TITLE,), site="a.test", industry="kettles", sitewide=()) -> list[str]:
    card_ids = [f"{batch}/c{i}" for i in range(len(entries))]
    sim.ok("batch_created", Batch(batch_id=batch, site_host=site, run_id="r1", card_ids=card_ids), batch=batch)
    for card_id, entry in zip(card_ids, entries, strict=True):
        sim.ok("card_created", spec(card_id, batch, entry, industry=industry, sitewide=card_id in sitewide),
               batch=batch, card=card_id)
    return card_ids


def run_baseline(sim: Sim, batch="b1", day=BASE_DAY, fz: BatchFreeze | None = None, **sample_kw) -> None:
    sim.ok("baseline_started", MeasureStart(freeze=fz or freeze()), batch=batch, at=dt(day))
    sim.ok("baseline_finished", MeasureDone(day=day, legs=samples(**sample_kw)), batch=batch, at=dt(day, 12))


def mark_deployed(sim: Sim, card_ids, batch="b1", day=DEPLOY_DAY):
    return sim.ok("deployed", DeployMark(day=day, card_ids=card_ids), batch=batch, at=dt(day))


def remeasure(sim: Sim, result: CompareResult | None, batch="b1", day=RE_DAY, fz: BatchFreeze | None = None) -> None:
    sim.ok("remeasure_started", MeasureStart(freeze=fz), batch=batch, at=dt(day))
    sim.ok("remeasure_finished", MeasureDone(day=day, legs=samples(), result=result), batch=batch, at=dt(day, 12))


def full(sim: Sim, batch="b1", pairs=(UP, FLAT), *, entries=(TITLE,), site="a.test", industry="kettles",
         baseline=BASE_DAY, deploy=DEPLOY_DAY, re_day=RE_DAY, source="potato_file") -> list[str]:
    """一批从出包走到已比对。"""
    card_ids = open_batch(sim, batch, entries, site, industry)
    run_baseline(sim, batch, baseline, freeze(source=source))
    mark_deployed(sim, card_ids, batch, deploy)
    remeasure(sim, compare(batch, sim.rules(batch), pairs, baseline=baseline, deploy=deploy, remeasure=re_day),
              batch, re_day)
    return card_ids


# ── 合法转换:设计 §8.3 的每一条边 ─────────────────────────────────────

def test_package_creates_batch_and_one_card_per_change():
    sim = Sim()
    card_ids = open_batch(sim, entries=(TITLE, PRICE))
    batch = sim.batch()
    assert batch.phase == "已出包" and batch.cards_created == 2 and not batch.batch.frozen
    assert [c.card_id for c in sim.view().cards] == card_ids
    assert all(c.validation == "假设" and not c.deployed for c in sim.view().cards)  # 出包时为"假设"


def test_baseline_start_freezes_parameters_and_finish_makes_it_ready():
    sim = Sim()
    open_batch(sim)
    fz = freeze()
    sim.ok("baseline_started", MeasureStart(freeze=fz), at=dt(BASE_DAY))
    batch = sim.batch()
    assert batch.phase == "测基线中" and batch.batch.frozen
    assert batch.batch.treated_q == fz.treated_q and batch.batch.rules == fz.rules and batch.batch.panel == fz.panel
    sim.ok("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()), at=dt(BASE_DAY, 12))
    batch = sim.batch()
    assert batch.phase == "基线就绪" and batch.baseline.complete and batch.baseline.day == BASE_DAY


def test_failed_baseline_goes_back_and_can_be_retried_with_the_same_frozen_parameters():
    sim = Sim()
    open_batch(sim)
    fz = freeze()
    sim.ok("baseline_started", MeasureStart(freeze=fz), at=dt(BASE_DAY))
    sim.ok("baseline_failed", MeasureFailed(reason="额度用完"), at=dt(BASE_DAY, 10))
    batch = sim.batch()
    assert batch.phase == "已出包" and batch.baseline is None and "额度用完" in batch.measure_failures[0]
    sim.ok("baseline_started", MeasureStart(), at=dt(BASE_DAY, 11))          # 重测可以不再带冻结参数
    sim.ok("baseline_failed", MeasureFailed(reason="断网"), at=dt(BASE_DAY, 12))
    sim.ok("baseline_started", MeasureStart(freeze=fz), at=dt(BASE_DAY, 13))  # 带了,一样的也行
    sim.ok("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()), at=dt(BASE_DAY, 14))
    assert sim.batch().phase == "基线就绪" and len(sim.batch().measure_failures) == 2


def test_owner_may_measure_the_baseline_again_before_deploying():
    sim = Sim()
    open_batch(sim)
    run_baseline(sim, day="2026-11-01")
    run_baseline(sim, day="2026-11-10")  # 隔久了,站长自己决定再测一次
    batch = sim.batch()
    assert batch.baselines_done == 2 and batch.baseline.day == "2026-11-10" and batch.phase == "基线就绪"
    sim.ok("baseline_started", MeasureStart(), at=dt("2026-11-11"))
    sim.ok("baseline_failed", MeasureFailed(reason="断网"), at=dt("2026-11-11", 10))
    assert sim.batch().phase == "基线就绪"  # 失败后回到原来的状态,前一次基线还在


def test_deploy_with_a_valid_baseline_then_becomes_due_by_date():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    decision = mark_deployed(sim, card_ids)
    assert decision.notes == []
    batch = sim.batch()
    assert batch.phase == "已部署" and batch.deploy.baseline_valid and batch.recommended_from == "2026-12-01"
    assert sim.card(card_ids[0]).validation == "实验中"  # 标记部署后为"实验中"
    assert sim.batch(as_of=date(2026, 11, 30)).phase == "已部署"
    assert sim.batch(as_of=date(2026, 12, 1)).phase == "待再测"  # 按日期推出;不存进日志


def test_deploy_without_a_baseline_is_allowed_but_marked():
    sim = Sim()
    card_ids = open_batch(sim)
    decision = mark_deployed(sim, card_ids)
    assert decision.ok and "没有改之前的测量" in decision.notes[0]
    assert sim.batch().phase == "已部署" and not sim.batch().deploy.baseline_valid


@pytest.mark.parametrize(("deploy_day", "why"), [("2026-11-09", "超过 7 天"), ("2026-10-30", "基线晚于部署")])
def test_deploy_outside_the_baseline_window_is_allowed_but_baseline_invalid(deploy_day, why):
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    decision = sim.ok("deployed", DeployMark(day=deploy_day, card_ids=card_ids), at=dt("2026-11-09"))
    assert "基线无效" in decision.notes[0] and why in decision.notes[0]
    assert not sim.batch().deploy.baseline_valid


def test_deploy_after_an_incomplete_baseline_is_marked_invalid():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, ok=60)  # 60/72 = 83%,不到九成
    assert not sim.batch().baseline.complete
    assert "不完整" in mark_deployed(sim, card_ids).notes[0]


def test_revert_deploy_goes_back_and_deploy_can_be_marked_again():
    sim = Sim()
    card_ids = open_batch(sim, entries=(TITLE, PRICE))
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    sim.ok("deploy_reverted", OwnerNote(reason="点错了"), at=dt(DEPLOY_DAY, 10))
    assert sim.batch().phase == "基线就绪" and sim.batch().deploy is None
    assert all(c.validation == "假设" for c in sim.view().cards)
    mark_deployed(sim, card_ids[:1])  # 这次只勾一条
    assert sim.batch().deploy.card_ids == card_ids[:1]
    other = Sim()
    ids_ = open_batch(other)
    mark_deployed(other, ids_)
    other.ok("deploy_reverted", OwnerNote(), at=dt(DEPLOY_DAY, 10))
    assert other.batch().phase == "已出包"  # 没有基线时退回"已出包"


def test_remeasure_start_finish_and_failure():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    sim.ok("remeasure_started", MeasureStart(), at=dt(RE_DAY))
    assert sim.batch().phase == "再测中"
    sim.ok("remeasure_failed", MeasureFailed(reason="失败太多,自动停了"), at=dt(RE_DAY, 10))
    assert sim.batch().phase == "已部署" and sim.batch().readings == []
    remeasure(sim, compare("b1", sim.rules()))
    batch = sim.batch()
    assert batch.phase == "已比对" and batch.scored and len(batch.readings) == 1 and batch.outcome == "won"
    sim.ok("remeasure_started", MeasureStart(), at=dt("2026-12-20"))  # 已比对之后还能再测,结果按时间追加
    sim.ok("remeasure_failed", MeasureFailed(reason="断网"), at=dt("2026-12-20", 10))
    assert sim.batch().phase == "已比对"


def test_remeasure_without_any_baseline_gives_only_the_current_picture():
    sim = Sim()
    card_ids = open_batch(sim)
    mark_deployed(sim, card_ids)
    remeasure(sim, None, fz=freeze())  # 第一次测量在这里,冻结参数在这里带
    card = sim.card(card_ids[0])
    assert sim.batch().phase == "已比对" and card.validation == "假设" and "没有改之前的测量" in card.validation_note
    assert card.post_change and card.observed_effect[0].legs == []


PHASES = ("已出包", "测基线中", "基线就绪", "已部署", "再测中", "已比对", "已放弃")
LEGAL = {  # 事件 → 哪些阶段收(设计 §8.3 的图)
    "batch_created": set(), "card_created": set(),
    "baseline_started": {"已出包", "基线就绪"}, "baseline_finished": {"测基线中"}, "baseline_failed": {"测基线中"},
    "deployed": {"已出包", "基线就绪"}, "deploy_reverted": {"已部署"},
    "remeasure_started": {"已部署", "已比对"}, "remeasure_finished": {"再测中"}, "remeasure_failed": {"再测中"},
    "abandoned": {"已出包", "测基线中", "基线就绪", "已部署", "再测中", "已比对"},
}


def at_phase(phase: str) -> tuple[Sim, list[str]]:
    sim = Sim()
    card_ids = open_batch(sim)
    if phase == "已放弃":
        sim.ok("abandoned", OwnerNote(reason="不做了"))
        return sim, card_ids
    steps = [
        lambda: sim.ok("baseline_started", MeasureStart(freeze=freeze()), at=dt(BASE_DAY)),
        lambda: sim.ok("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()), at=dt(BASE_DAY, 12)),
        lambda: mark_deployed(sim, card_ids),
        lambda: sim.ok("remeasure_started", MeasureStart(), at=dt(RE_DAY)),
        lambda: sim.ok("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(), result=compare("b1", sim.rules())),
                       at=dt(RE_DAY, 12)),
    ]
    for step in steps[:PHASES.index(phase)]:
        step()
    assert sim.batch().phase == phase
    return sim, card_ids


def plausible(kind: str, sim: Sim, card_ids: list[str]):
    """每种事件一份内容没毛病的载荷:这样被拒只能是因为状态不对。"""
    return {
        "batch_created": Batch(batch_id="b1", site_host="a.test", run_id="r1", card_ids=["b1/x"]),
        "card_created": spec(card_ids[0], "b1"),
        "baseline_started": MeasureStart(freeze=freeze()), "remeasure_started": MeasureStart(freeze=freeze()),
        "baseline_finished": MeasureDone(day=BASE_DAY, legs=samples()),
        "remeasure_finished": MeasureDone(day=RE_DAY, legs=samples(),
                                          result=compare("b1", freeze().rules)),
        "baseline_failed": MeasureFailed(reason="x"), "remeasure_failed": MeasureFailed(reason="x"),
        "deployed": DeployMark(day=DEPLOY_DAY, card_ids=card_ids),
        "deploy_reverted": OwnerNote(), "abandoned": OwnerNote(reason="不做了"),
    }[kind]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", sorted(LEGAL))
def test_transition_table_every_event_in_every_phase(phase, kind):
    """7 个阶段 × 11 种事件:该收的收,不该收的拒绝并给出原因;内容是空的也只是拒绝,从不抛异常。"""
    sim, card_ids = at_phase(phase)
    card = card_ids[0] if kind == "card_created" else None
    decision = sim.do(kind, plausible(kind, sim, card_ids), card=card, at=dt(RE_DAY, 13))
    assert decision.ok == (phase in LEGAL[kind]), decision.reason
    assert decision.ok or decision.reason
    sim2, _ = at_phase(phase)
    empty = sim2.do(kind, {}, card=card, at=dt(RE_DAY, 13))  # 空载荷:要么本来就不需要内容,要么拒绝
    assert empty.ok or empty.reason


@pytest.mark.parametrize("phase", [p for p in PHASES if p != "已放弃"])
def test_abandon_from_any_phase_then_nothing_else_is_accepted(phase):
    sim, card_ids = at_phase(phase)
    sim.ok("abandoned", OwnerNote(reason="不做了"), at=dt(RE_DAY, 14))
    assert sim.batch().phase == "已放弃" and sim.batch().abandoned_reason == "不做了"
    assert all(c.validation == "假设" and c.validation_note == "这一批已放弃" for c in sim.view().cards)
    for kind in LEGAL:
        reason = sim.no(kind, plausible(kind, sim, card_ids), card=card_ids[0] if kind == "card_created" else None)
        assert "已经放弃" in reason or kind == "batch_created"


# ── 典型的非法转换:拒绝并说明原因 ─────────────────────────────────────

def test_batch_creation_guards():
    sim = Sim()
    open_batch(sim)
    assert "已经有了" in sim.no("batch_created", Batch(batch_id="b1", site_host="a.test", run_id="r", card_ids=["b1/z"]))
    assert "一张卡都没有" in sim.no("batch_created", Batch(batch_id="b2", site_host="a.test", run_id="r", card_ids=[]),
                               batch="b2")
    assert "重复" in sim.no("batch_created", Batch(batch_id="b2", site_host="a.test", run_id="r", card_ids=["x", "x"]),
                           batch="b2")
    assert "别的批次" in sim.no("batch_created", Batch(batch_id="b2", site_host="a.test", run_id="r", card_ids=["b1/c0"]),
                             batch="b2")
    assert "不一样" in sim.no("batch_created", Batch(batch_id="b9", site_host="a.test", run_id="r", card_ids=["q"]),
                            batch="b2")
    assert "已冻结" in sim.no("batch_created", Batch(batch_id="b2", site_host="a.test", run_id="r", card_ids=["q"],
                                                  frozen=True), batch="b2")
    assert "没有这个批次" in sim.no("abandoned", OwnerNote(), batch="nope")


def test_card_creation_guards():
    sim = Sim()
    sim.ok("batch_created", Batch(batch_id="b1", site_host="a.test", run_id="r1", card_ids=["b1/c0", "b1/c1"]))
    sim.ok("card_created", spec("b1/c0", "b1"), card="b1/c0")
    assert "已经建过" in sim.no("card_created", spec("b1/c0", "b1"), card="b1/c0")
    assert "没有登记" in sim.no("card_created", spec("b1/zz", "b1"), card="b1/zz")
    assert "不一样" in sim.no("card_created", spec("b1/c1", "b1"), card="b1/c0")
    assert "还有 1 张卡没建好" in sim.no("baseline_started", MeasureStart(freeze=freeze()))
    assert "还有 1 张卡没建好" in sim.no("deployed", DeployMark(day=DEPLOY_DAY, card_ids=["b1/c0"]), at=dt(DEPLOY_DAY))
    sim.ok("card_created", spec("b1/c1", "b1"), card="b1/c1")
    sim.ok("baseline_started", MeasureStart(freeze=freeze()), at=dt(BASE_DAY))  # 卡建齐了才收


def test_no_card_can_be_added_once_measuring_or_deploying_started():
    extra = Sim()
    extra.ok("batch_created", Batch(batch_id="b1", site_host="a.test", run_id="r1", card_ids=["b1/c0", "b1/c1"]))
    extra.ok("card_created", spec("b1/c0", "b1"), card="b1/c0")
    extra.ledger.batches["b1"].phase = "已部署"  # 只为走到这条守卫:正常流程里卡没建齐到不了这里
    assert "不能再加卡" in extra.no("card_created", spec("b1/c1", "b1"), card="b1/c1")


def test_measurement_can_only_be_started_by_the_owner():
    """Butter 不自己发起测量(设计 §8.4):system 起的测量一律拒绝。部署、撤销、放弃也只认站长。"""
    sim = Sim()
    card_ids = open_batch(sim)
    for kind, payload in (("baseline_started", MeasureStart(freeze=freeze())),
                          ("deployed", DeployMark(day=DEPLOY_DAY, card_ids=card_ids)),
                          ("abandoned", OwnerNote())):
        assert "只能由站长" in sim.no(kind, payload, actor="system", at=dt(DEPLOY_DAY))
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    assert "只能由站长" in sim.no("remeasure_started", MeasureStart(), actor="system", at=dt(RE_DAY))
    assert "只能由站长" in sim.no("deploy_reverted", OwnerNote(), actor="system")


def test_frozen_parameters_cannot_change_after_the_first_measurement():
    sim = Sim()
    card_ids = open_batch(sim)
    assert "冻结参数" in sim.no("baseline_started", MeasureStart())  # 第一次必须带
    run_baseline(sim)
    assert "不能改" in sim.no("baseline_started", MeasureStart(freeze=freeze(n_t=10)), at=dt(BASE_DAY, 15))
    mark_deployed(sim, card_ids)
    assert "不能改" in sim.no("remeasure_started", MeasureStart(freeze=freeze(legs=(ZH, EN))), at=dt(RE_DAY))
    sim.ok("remeasure_started", MeasureStart(freeze=freeze()), at=dt(RE_DAY))  # 一样的可以


def test_bad_frozen_parameters_are_rejected():
    sim = Sim()
    open_batch(sim)
    good = freeze()
    for change, word in (({"panel": []}, "题板"), ({"surfaces": []}, "引擎"),
                         ({"plan": BatchPlan(runs_per_question=0)}, "至少"),
                         ({"control_q": ["t0", "c1"]}, "既在处理组又在对照组")):
        assert word in sim.no("baseline_started", MeasureStart(freeze=good.model_copy(update=change)))


def test_baseline_guards():
    sim = Sim()
    card_ids = open_batch(sim)
    assert "没有在测基线" in sim.no("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()))
    sim.ok("baseline_started", MeasureStart(freeze=freeze()), at=dt(BASE_DAY))
    assert "正在测" in sim.no("baseline_started", MeasureStart())
    assert "正在测" in sim.no("deployed", DeployMark(day=BASE_DAY, card_ids=card_ids), at=dt(BASE_DAY, 10))
    assert "日期读不懂" in sim.no("baseline_finished", MeasureDone(day="11月1日", legs=samples()))
    assert "一条腿都没有" in sim.no("baseline_finished", MeasureDone(day=BASE_DAY, legs=[]))
    assert "对不上" in sim.no("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples(ok=80, expected=72)))
    assert "不带比对结果" in sim.no("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples(),
                                                              result=compare("b1", freeze().rules)))
    sim.ok("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()), at=dt(BASE_DAY, 12))
    mark_deployed(sim, card_ids)
    assert "改之前的情况测不了" in sim.no("baseline_started", MeasureStart(), at=dt(DEPLOY_DAY, 10))


def test_deploy_guards():
    sim = Sim()
    card_ids = open_batch(sim, entries=(TITLE, PRICE))
    at = dt(DEPLOY_DAY)
    assert "至少要勾一条" in sim.no("deployed", DeployMark(day=DEPLOY_DAY, card_ids=[]), at=at)
    assert "重复" in sim.no("deployed", DeployMark(day=DEPLOY_DAY, card_ids=[card_ids[0]] * 2), at=at)
    assert "没有这些卡" in sim.no("deployed", DeployMark(day=DEPLOY_DAY, card_ids=["b9/c0"]), at=at)
    assert "日期读不懂" in sim.no("deployed", DeployMark(day="明天", card_ids=card_ids), at=at)
    assert "将来的日期" in sim.no("deployed", DeployMark(day="2026-11-20", card_ids=card_ids), at=at)
    assert "没有可撤销的" in sim.no("deploy_reverted", OwnerNote())
    assert "还没标记部署" in sim.no("remeasure_started", MeasureStart(freeze=freeze()), at=at)
    mark_deployed(sim, card_ids)
    assert "已经标记过部署" in sim.no("deployed", DeployMark(day=DEPLOY_DAY, card_ids=card_ids), at=at)


def test_remeasure_guards():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    rules = sim.rules()
    assert "没有在再测" in sim.no("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(), result=compare("b1", rules)))
    sim.ok("remeasure_started", MeasureStart(), at=dt(RE_DAY))
    assert "正在再测" in sim.no("deploy_reverted", OwnerNote())
    assert "早于部署日期" in sim.no("remeasure_finished", MeasureDone(day="2026-11-02", legs=samples(),
                                                                result=compare("b1", rules)))
    assert "要带上比对结果" in sim.no("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples()))
    assert "别的批次" in sim.no("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(), result=compare("b2", rules)))
    sim.ok("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(), result=compare("b1", rules)), at=dt(RE_DAY, 12))
    assert "撤不了" in sim.no("deploy_reverted", OwnerNote())  # 再测过就不能撤销部署标记,只能放弃
    nobase = Sim()
    ids_ = open_batch(nobase)
    mark_deployed(nobase, ids_)
    nobase.ok("remeasure_started", MeasureStart(freeze=freeze()), at=dt(RE_DAY))
    assert "不该带比对结果" in nobase.no("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(),
                                                                   result=compare("b1", rules)))


def test_malformed_payloads_and_duplicate_event_ids_are_rejected_not_raised():
    sim = Sim()
    card_ids = open_batch(sim)
    assert "事件内容不合规" in sim.no("deployed", {"day": DEPLOY_DAY})                      # 缺字段
    assert "事件内容不合规" in sim.no("deployed", {"day": DEPLOY_DAY, "card_ids": card_ids, "x": 1})  # 多字段
    assert "事件内容不合规" in sim.no("baseline_finished", {"day": BASE_DAY, "legs": "很多"})
    assert "事件内容不合规" in sim.no("card_created", {"card_id": "b1/c0"}, card="b1/c0")
    first = next(iter(sim.ledger.accepted.values()))
    again = decide(sim.ledger, first)
    assert not again.ok and "事件号重复" in again.reason


def test_replay_skips_bad_events_instead_of_failing():
    """日志被人手改坏了也能读:不合规的事件跳过,记下是第几条、为什么。"""
    sim = Sim()
    card_ids = open_batch(sim)
    good = list(sim.ledger.accepted.values())
    rogue = CardEvent(event_id="hand-1", at=dt(DEPLOY_DAY), actor="system", batch_id="b1", type="deployed",
                      payload={"day": DEPLOY_DAY, "card_ids": card_ids})  # system 标部署:不合规
    ghost = CardEvent(event_id="hand-2", at=dt(DEPLOY_DAY), actor="owner", batch_id="nope", type="abandoned")
    ledger = fold([good[0], rogue, good[1], ghost, good[0]])
    view = project(ledger)
    assert view.events == 5 and [s.seq for s in view.skipped] == [2, 4, 5]
    assert "只能由站长" in view.skipped[0].reason and "没有这个批次" in view.skipped[1].reason
    assert "事件号重复" in view.skipped[2].reason
    assert view.batches[0].phase == "已出包" and len(view.cards) == 1


# ── 验证状态:设计 §8.5 与 §9.4 总表的每一行 ────────────────────────────

def test_single_entry_batch_up_is_single_observation_more():
    sim = Sim()
    (card_id,) = full(sim, pairs=(UP, FLAT))
    card = sim.card(card_id)
    assert (card.validation, card.direction, card.outcome) == ("单次观测", "变多", "won")
    assert any("只测到一次" in text for text in card.limitations)


def test_single_entry_batch_down_is_single_observation_fewer():
    sim = Sim()
    (card_id,) = full(sim, pairs=(DOWN, FLAT))
    card = sim.card(card_id)
    assert (card.validation, card.direction, card.outcome) == ("单次观测", "变少", "lost")
    assert "超出波动下降" in card.observed_effect[0].lines[0]  # 反方向也照实写


def test_several_changes_on_one_entry_still_count_as_single_entry():
    sim = Sim()
    card_ids = full(sim, entries=(TITLE, TITLE, TITLE))
    assert sim.batch().single_entry and sim.batch().deployed_entries == [TITLE]
    for card_id in card_ids:
        card = sim.card(card_id)
        assert (card.validation, card.direction) == ("单次观测", "变多") and "同批 3 条" in card.validation_note


@pytest.mark.parametrize(("pairs", "word", "outcome"), [((UP, FLAT), "变多", "won"), ((DOWN, FLAT), "变少", "lost")])
def test_multi_entry_batch_stays_hypothesis_with_same_batch_note(pairs, word, outcome):
    sim = Sim()
    card_ids = full(sim, pairs=pairs, entries=(TITLE, PRICE))
    assert not sim.batch().single_entry
    for card_id in card_ids:
        card = sim.card(card_id)
        assert card.validation == "假设" and card.direction is None and card.outcome == outcome
        assert "同批 2 条" in card.validation_note and word in card.validation_note
        assert any("不能单独归因" in text for text in card.limitations)
        assert card.observed_effect[0].lines  # "观测到的效果"照写


def test_only_the_deployed_cards_decide_whether_the_batch_is_single_entry():
    sim = Sim()
    card_ids = open_batch(sim, entries=(TITLE, PRICE))
    run_baseline(sim)
    mark_deployed(sim, card_ids[:1])  # 只部署了标题那一条
    remeasure(sim, compare("b1", sim.rules()))
    deployed, skipped = sim.card(card_ids[0]), sim.card(card_ids[1])
    assert (deployed.validation, deployed.direction) == ("单次观测", "变多")
    assert skipped.validation == "假设" and "未部署" in skipped.validation_note and not skipped.deployed
    assert skipped.outcome is None and skipped.observed_effect == [] and skipped.post_change == []


def test_no_valid_control_keeps_hypothesis():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, fz=freeze(n_c=2))  # 对照只有 2 题,少于下限 5
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), (UP, [(1, 1)] * 2)))
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome == "inconclusive" and "无有效对照" in card.validation_note
    assert not card.control.valid and "只做了前后对比" in card.control.note


def test_sitewide_change_means_no_valid_control_even_if_the_result_says_up():
    sim = Sim()
    card_ids = open_batch(sim, sitewide=("b1/c0",))
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    assert sim.batch().sitewide
    remeasure(sim, compare("b1", sim.rules(), (UP, FLAT)))  # 调用方忘了标全站:比对给了 won
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome == "inconclusive" and "全站" in card.validation_note
    honest = Sim()
    ids_ = open_batch(honest, sitewide=("b1/c0",))
    run_baseline(honest)
    mark_deployed(honest, ids_)
    remeasure(honest, compare("b1", honest.rules(), (UP, FLAT), sitewide=True))
    assert honest.card(ids_[0]).validation == "假设" and "无有效对照" in honest.card(ids_[0]).validation_note


def test_drift_keeps_hypothesis():
    sim = Sim()
    (card_id,) = full(sim, pairs=(UP, UP))  # 没改的题也一起涨了
    card = sim.card(card_id)
    assert card.validation == "假设" and card.outcome == "inconclusive" and "同期漂移" in card.validation_note


def test_within_noise_goes_back_to_hypothesis_with_no_visible_change():
    sim = Sim()
    (card_id,) = full(sim, pairs=(FLAT, FLAT))
    card = sim.card(card_id)
    assert card.validation == "假设" and card.outcome == "inconclusive" and card.validation_note == "无可辨变化"


def test_not_comparable_goes_back_to_hypothesis_with_the_reason():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), (UP, FLAT), config_mismatch=["引擎型号"]))
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome == "dnf"
    assert "不可比:配置变了" in card.validation_note and "引擎型号" in card.validation_note


def test_invalid_baseline_never_scores_even_if_the_result_says_up():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    sim.ok("deployed", DeployMark(day="2026-11-12", card_ids=card_ids), at=dt("2026-11-12"))  # 基线后 11 天
    result = compare("b1", sim.rules(), (UP, FLAT), deploy="2026-11-12", remeasure="2026-12-15")
    assert result.legs[0].label == "baseline_invalid"
    remeasure(sim, result, day="2026-12-15")
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome == "dnf" and "基线无效" in card.validation_note
    lying = compare("b1", sim.rules(), (UP, FLAT))  # 调用方给了错的日期,比对说 won
    other = Sim()
    ids_ = open_batch(other)
    run_baseline(other)
    other.ok("deployed", DeployMark(day="2026-11-12", card_ids=ids_), at=dt("2026-11-12"))
    remeasure(other, lying, day="2026-12-15")
    assert other.card(ids_[0]).validation == "假设" and other.card(ids_[0]).outcome == "dnf"


def test_remeasure_before_the_recommended_gap_is_recorded_but_does_not_score():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    early = compare("b1", sim.rules(), (UP, FLAT), remeasure="2026-11-13")
    assert early.legs[0].outcome == "won"
    remeasure(sim, early, day="2026-11-13")  # 部署后 10 天
    card = sim.card(card_ids[0])
    assert sim.batch().phase == "已比对" and not sim.batch().scored
    assert card.validation == "实验中" and card.direction is None and "只作记录" in card.validation_note
    assert len(card.observed_effect) == 1 and not card.observed_effect[0].scoring
    remeasure(sim, compare("b1", sim.rules(), (FLAT, FLAT)), day=RE_DAY)  # 过了间隔的第一次:在波动内
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.validation_note == "无可辨变化"
    assert [r.scoring for r in card.observed_effect] == [False, True]


def test_only_the_first_remeasure_after_the_gap_scores():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), (FLAT, FLAT)), day="2026-12-01")  # 正好第 28 天:计分
    remeasure(sim, compare("b1", sim.rules(), (UP, FLAT), remeasure="2026-12-20"), day="2026-12-20")
    card = sim.card(card_ids[0])
    assert [r.scoring for r in card.observed_effect] == [True, False]
    assert card.validation == "假设" and card.outcome == "inconclusive"  # 后来测到变多也不改结局
    assert len(card.observed_effect[1].lines) == 1 and "超出波动上升" in card.observed_effect[1].lines[0]  # 但照实追加


def test_legs_pointing_both_ways_are_inconclusive_and_citation_alone_does_not_decide():
    legs = (ZH, EN, ZH_CITE)
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, fz=freeze(legs=legs))
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), per_leg={ZH: (UP, FLAT), EN: (DOWN, FLAT), ZH_CITE: (FLAT, FLAT)}))
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome == "inconclusive" and "方向不一致" in card.validation_note

    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, fz=freeze(legs=legs))
    mark_deployed(sim, card_ids)
    result = compare("b1", sim.rules(), per_leg={ZH: (FLAT, FLAT), EN: (FLAT, FLAT), ZH_CITE: (UP, FLAT)})
    assert [leg.outcome for leg in result.legs] == ["inconclusive", "inconclusive", "won"]
    remeasure(sim, result)
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.validation_note == "无可辨变化"  # 被引率是辅助指标,不决定结局

    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, fz=freeze(legs=legs))
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), per_leg={ZH: (UP, FLAT), EN: (FLAT, FLAT), ZH_CITE: (FLAT, FLAT)}))
    card = sim.card(card_ids[0])
    assert (card.validation, card.direction) == ("单次观测", "变多") and "2 条腿里有 1 条" in card.outcome_reason


def test_domestic_platform_and_undeployed_rows_stay_hypothesis():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim)
    mark_deployed(sim, card_ids)
    remeasure(sim, compare("b1", sim.rules(), (UP, FLAT), domestic=True))
    card = sim.card(card_ids[0])
    assert card.validation == "假设" and card.outcome is None and "不可再测" in card.validation_note


# ── §20.4 的 11 个字段 ────────────────────────────────────────────────

def test_card_carries_the_eleven_fields_derived_from_events():
    sim = Sim()
    (card_id,) = full(sim)
    card = sim.card(card_id)
    assert card.schema_version == "card/1" and card.batch_id == "b1" and card.change_item_id == "c0" and card.deployed
    assert card.intervention.target.endswith("的标题") and card.intervention.before == "Classic"   # 1 干预措施
    assert card.target_stage == "检索"                                                              # 2 目标阶段
    assert "12 道题" in card.hypothesis and "标题复述问题" in card.hypothesis                        # 3 假设
    assert card.evidence.entry_id == TITLE and card.evidence.level == "B" and card.evidence.sources  # 4 依据
    cond = card.test_conditions                                                                      # 5 测试条件
    assert cond.surfaces[0].model == "anthropic-model" and cond.tool_versions == ["aivis 0.9.1"]
    assert cond.panel[0].prompt_version == "panel-v1" and cond.panel[0].sha256 == "a" * 64
    assert cond.industry == "kettles" and cond.sample_days == [BASE_DAY, RE_DAY] and cond.page
    assert card.baseline[0].ok == 72 and card.baseline[0].n_questions == 12                          # 6 基线
    assert card.post_change[0].expected == 72                                                        # 7 修改后结果
    assert card.control.valid and len(card.control.questions) == 12                                  # 8 对照
    assert card.control.pages == ["https://a.test/other"]
    line = card.observed_effect[0].lines[0]                                                          # 9 观测到的效果
    assert "改前 0.0% → 改后 100.0%" in line and "区间" in line and "±" in line and "超出波动上升" in line
    assert any("API" in text for text in card.limitations)                                           # 10 局限
    assert any("Observed change ≠ proven cause" in text for text in card.limitations)
    assert any("大品牌" in text for text in card.limitations)
    assert (card.validation, card.direction) == ("单次观测", "变多")                                 # 11 验证状态
    assert "有效" not in line and "提升了" not in line  # 设计 §9.6:不写"有效",不写"提升了 X%"


def test_limitations_name_citation_legs_not_compared_but_say_nothing_about_butter_generated_panels():
    sim = Sim()
    card_ids = open_batch(sim)
    run_baseline(sim, fz=freeze(legs=(ZH, ZH_CITE), source="butter_generated"))
    mark_deployed(sim, card_ids)
    result = compare("b1", sim.rules(), per_leg={ZH: (UP, FLAT), ZH_CITE: (UP, FLAT)})
    low = result.legs[1].model_copy(update={"label": "not_comparable", "outcome": "dnf", "reason": "搜索太少"})
    remeasure(sim, result.model_copy(update={"legs": [result.legs[0], low]}))
    texts = "\n".join(sim.card(card_ids[0]).limitations)
    assert "很少联网搜索" in texts
    assert "自己出" not in texts and "偏乐观" not in texts  # 2026-11-18 用户定:卡片上不写这句;题从哪来仍记在测试条件里
    assert sim.card(card_ids[0]).test_conditions.panel[0].source == "butter_generated"


def test_plan_batch_turns_a_real_change_package_into_cards(acme_bundle):
    registry = load_registry()
    batch, specs = cards.plan_batch(acme_bundle.package, registry, batch_id="acme-1", site_host="www.acme-kettles.test",
                                    industry="电热水壶")
    real_changes = [i for i in acme_bundle.package.items if i.action != "none"]
    assert len(specs) == len(real_changes) >= 3 and batch.card_ids == [s.card_id for s in specs]
    by_kind = {s.kind: s for s in specs}
    assert by_kind["robots"].sitewide and by_kind["robots"].entry_stage == "ACCESS"
    assert by_kind["robots"].entry_level == registry.get(ids.ACCESS_ROBOTS).evidence_level
    assert not by_kind["jsonld_product"].sitewide
    assert all(s.entry_hypothesis and s.entry_sources and s.industry == "电热水壶" for s in specs)
    sim = Sim()
    sim.ok("batch_created", batch, batch="acme-1")
    for s in specs:
        sim.ok("card_created", s, batch="acme-1", card=s.card_id)
    assert sim.batch("acme-1").cards_created == len(specs) and sim.batch("acme-1").sitewide


# ── 方法级汇总 ────────────────────────────────────────────────────────

def method(sim: Sim, entry: str = TITLE):
    return next(m for m in sim.view().methods if m.entry_id == entry)


def test_one_observation_gives_single_observation_in_that_direction_only():
    sim = Sim()
    full(sim, "b1", (UP, FLAT))
    m = method(sim)
    assert m.more.state == "单次观测" and len(m.more.independent) == 1 and m.more.cards == 1
    assert m.fewer.state is None and m.fewer.independent == [] and m.cards_total == 1
    assert m.more.independent[0].legs == ["zh × anthropic × 本品牌提及率"]


def test_two_sites_same_direction_is_replicated():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test")
    full(sim, "b2", (UP, FLAT), site="b.test")
    m = method(sim)
    assert m.more.state == "已复现" and [e.batch_id for e in m.more.independent] == ["b1", "b2"]


def test_same_site_overlapping_windows_count_once():
    """最保守的读法:同一个站、测量时间重叠的两批,不算两次独立实验。"""
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test")
    full(sim, "b2", (UP, FLAT), site="a.test", baseline="2026-11-02", deploy="2026-11-04", re_day="2026-12-03")
    m = method(sim)
    assert m.more.state == "单次观测" and len(m.more.independent) == 1 and m.more.cards == 2
    assert [e.batch_id for e in m.more.not_independent] == ["b2"] and "时间重叠" in m.more.not_independent[0].note


def test_same_site_at_different_times_counts_twice():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test")
    full(sim, "b2", (UP, FLAT), site="a.test", baseline="2026-12-05", deploy="2026-12-06", re_day="2027-01-05")
    assert method(sim).more.state == "已复现" and len(method(sim).more.independent) == 2


def test_several_cards_in_one_batch_are_one_experiment():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), entries=(TITLE, TITLE, TITLE))
    m = method(sim)
    assert m.more.cards == 3 and len(m.more.independent) == 1 and m.more.state == "单次观测"


def test_both_directions_are_listed_side_by_side_not_netted():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test")
    full(sim, "b2", (DOWN, FLAT), site="b.test")
    m = method(sim)
    assert m.more.state == "单次观测" and m.fewer.state == "单次观测"
    assert len(m.more.independent) == 1 and len(m.fewer.independent) == 1


def test_two_decreases_is_replicated_fewer():
    sim = Sim()
    full(sim, "b1", (DOWN, FLAT), site="a.test")
    full(sim, "b2", (DOWN, FLAT), site="b.test")
    assert method(sim).fewer.state == "已复现" and method(sim).more.state is None


def test_generalizable_needs_replication_across_industries():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test", industry="kettles")
    full(sim, "b2", (UP, FLAT), site="b.test", industry="kettles")
    assert method(sim).more.state == "已复现"
    full(sim, "b3", (UP, FLAT), site="c.test", industry="headphones")
    assert method(sim).more.state == "可泛化"


def test_multi_entry_unscored_and_inconclusive_batches_do_not_count_and_abandoned_ones_vanish():
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test", entries=(TITLE, PRICE))  # 多条目:照写不计数
    full(sim, "b2", (UP, FLAT), site="b.test")
    sim.ok("abandoned", OwnerNote(reason="不要了"), batch="b2")            # 已放弃:不计,也不写
    full(sim, "b3", (FLAT, FLAT), site="c.test")                          # 没看出变化
    ids4 = open_batch(sim, "b4", site="d.test")
    run_baseline(sim, "b4")
    mark_deployed(sim, ids4, "b4")                                        # 还在试
    m = method(sim)
    assert m.more.state is None and m.fewer.state is None and m.more.independent == []
    assert m.same_batch_more == ["b1/c0"]
    assert (m.cards_total, m.cards_hypothesis, m.cards_running) == (3, 2, 1)
    assert "b2" not in m.model_dump_json()  # 放弃的批次在汇总里完全不出现
    assert sim.batch("b2").phase == "已放弃"   # 但批次自己的记录还在
    assert method(sim, PRICE).same_batch_more == ["b1/c1"]


# ── 落盘:事件日志只追加,索引可整份重建 ─────────────────────────────────

def play(ws) -> list[str]:
    """经 service 走一遍:两批走完,一批在途,中间夹几个被拒的动作。返回被拒的原因。"""
    rejected = []

    def go(kind, payload=None, *, batch="b1", card=None, actor=None, at):
        decision = cards.submit(ws, at=at, actor=actor or ("owner" if kind in OWNER_ONLY else "system"),
                                batch_id=batch, type=kind, payload=payload, card_id=card)
        if not decision.ok:
            rejected.append(decision.reason)
        return decision

    rules = freeze().rules
    for batch, site, pairs in (("b1", "a.test", (UP, FLAT)), ("b2", "b.test", (UP, FLAT))):
        card_ids = [f"{batch}/c0", f"{batch}/c1"]
        go("batch_created", Batch(batch_id=batch, site_host=site, run_id="r1", card_ids=card_ids), batch=batch,
           at=dt("2026-10-31"))
        for card_id in card_ids:
            go("card_created", spec(card_id, batch), batch=batch, card=card_id, at=dt("2026-10-31", 10))
        go("baseline_started", MeasureStart(freeze=freeze()), batch=batch, actor="system", at=dt(BASE_DAY))  # 被拒
        go("baseline_started", MeasureStart(freeze=freeze()), batch=batch, at=dt(BASE_DAY))
        go("baseline_finished", MeasureDone(day=BASE_DAY, legs=samples()), batch=batch, at=dt(BASE_DAY, 12))
        go("deployed", DeployMark(day=DEPLOY_DAY, card_ids=card_ids), batch=batch, at=dt(DEPLOY_DAY))
        go("deployed", DeployMark(day=DEPLOY_DAY, card_ids=card_ids), batch=batch, at=dt(DEPLOY_DAY, 10))  # 被拒
        go("remeasure_started", MeasureStart(), batch=batch, at=dt(RE_DAY))
        go("remeasure_finished", MeasureDone(day=RE_DAY, legs=samples(), result=compare(batch, rules, pairs)),
           batch=batch, at=dt(RE_DAY, 12))
    go("batch_created", Batch(batch_id="b3", site_host="c.test", run_id="r1", card_ids=["b3/c0"]), batch="b3",
       at=dt("2026-12-03"))
    go("card_created", spec("b3/c0", "b3", PRICE), batch="b3", card="b3/c0", at=dt("2026-12-03", 10))
    go("deployed", DeployMark(day="2026-12-03", card_ids=["b3/c0"]), batch="b3", at=dt("2026-12-03", 11))
    go("deploy_reverted", OwnerNote(reason="点错了"), batch="b3", at=dt("2026-12-03", 12))
    return rejected


def index_dump(ws) -> dict:
    return {table: store.read_card_index(ws, table) for table in ("events", "batches", "cards", "methods")}


def test_submit_appends_accepted_events_only_and_never_rewrites(tmp_path):
    ws = tmp_path / "ws"
    log = ws / store.CARD_EVENTS
    decision = cards.submit(ws, at=dt(BASE_DAY), actor="owner", batch_id="nope", type="abandoned")
    assert not decision.ok and not log.exists()  # 被拒的动作什么都不写
    sizes, heads = [], []
    for i in range(3):
        cards.submit(ws, at=dt(BASE_DAY, 9 + i), actor="system", batch_id=f"b{i}", type="batch_created",
                     payload=Batch(batch_id=f"b{i}", site_host="a.test", run_id="r", card_ids=[f"b{i}/c0"]))
        raw = log.read_bytes()
        sizes.append(len(raw))
        heads.append(raw)
    assert sizes == sorted(sizes) and heads[1].startswith(heads[0]) and heads[2].startswith(heads[1])  # 只追加
    assert heads[2].endswith(b"\n") and heads[2].count(b"\n") == 3 and b"\r" not in heads[2]
    events, problems = store.read_card_events(ws)
    assert [e.batch_id for e in events] == ["b0", "b1", "b2"] and problems == []
    assert len({e.event_id for e in events}) == 3 and events[0].schema_version == "cardevent/1"


def test_reader_tolerates_a_half_written_last_line_and_new_events_start_on_a_fresh_line(tmp_path):
    ws = tmp_path / "ws"
    play(ws)
    log = ws / store.CARD_EVENTS
    whole, _ = store.read_card_events(ws)
    with open(log, "ab") as handle:  # 模拟写到一半断电
        handle.write(b'{"schema_version":"cardevent/1","event_id":"e9')
    events, problems = store.read_card_events(ws)
    assert events == whole and len(problems) == 1 and "半行" in problems[0]
    decision = cards.submit(ws, at=dt("2026-12-04"), actor="owner", batch_id="b3", type="abandoned",
                            payload=OwnerNote(reason="不做了"))
    assert decision.ok
    events, problems = store.read_card_events(ws)
    assert len(events) == len(whole) + 1 and events[-1].type == "abandoned"
    assert len(problems) == 1 and "读不了" in problems[0]  # 半行留在原地,成了中间的一行坏行;后面的事件不受影响
    view, _ = cards.load(ws)
    assert next(b for b in view.batches if b.batch.batch_id == "b3").phase == "已放弃"


def test_live_index_equals_index_rebuilt_from_the_event_log(tmp_path):
    """ADR-B02:实时更新出来的索引,和删掉后按事件日志重建的索引,查出来一模一样。"""
    ws = tmp_path / "ws"
    rejected = play(ws)
    assert len(rejected) == 4 and all(rejected)
    live = index_dump(ws)
    assert len(live["batches"]) == 3 and len(live["cards"]) == 5 and len(live["methods"]) == 2
    assert len(live["events"]) == len(store.read_card_events(ws)[0]) == 20

    view, problems = cards.rebuild(ws)
    assert problems == [] and view.skipped == [] and index_dump(ws) == live

    for leftover in ws.glob(store.CARD_INDEX + "*"):  # 把索引整个删掉
        leftover.unlink()
    assert store.read_card_index(ws, "cards") == []
    cards.rebuild(ws)
    assert index_dump(ws) == live

    # 索引里存的,和直接从日志推出来的当前样子是同一份
    from_log, _ = cards.load(ws)
    assert [json.loads(r["state"]) for r in live["batches"]] == [
        b.model_dump(mode="json") for b in sorted(from_log.batches, key=lambda b: b.batch.batch_id)]
    assert {r["card_id"]: json.loads(r["card"]) for r in live["cards"]} == {
        c.card_id: c.model_dump(mode="json") for c in from_log.cards}
    row = next(r for r in live["methods"] if r["entry_id"] == TITLE)
    assert (row["more_state"], row["more_independent"], row["fewer_state"]) == ("已复现", 2, None)
    b3 = next(r for r in live["batches"] if r["batch_id"] == "b3")
    assert (b3["phase"], b3["deploy_day"], b3["scored"]) == ("已出包", None, 0)
    assert {r["validation"] for r in live["cards"] if r["batch_id"] == "b1"} == {"单次观测"}


def test_rebuild_survives_a_log_with_bad_lines_and_bad_events(tmp_path):
    ws = tmp_path / "ws"
    play(ws)
    good, _ = cards.load(ws)
    rogue = CardEvent(event_id="hand-1", at=dt("2026-12-05"), actor="system", batch_id="b1", type="abandoned")
    with open(ws / store.CARD_EVENTS, "ab") as handle:
        handle.write("这一行不是 JSON\n".encode() + rogue.model_dump_json().encode() + b"\n")
    view, problems = cards.rebuild(ws)
    assert len(problems) == 1 and "读不了" in problems[0]
    assert len(view.skipped) == 1 and "只能由站长" in view.skipped[0].reason
    assert view.batches == good.batches and view.cards == good.cards
    assert len(store.read_card_index(ws, "events")) == 20  # 不合规的事件不进索引


def test_rebuild_throws_away_whatever_was_in_the_index(tmp_path):
    """索引里被写进了日志里没有的东西、或者某一行被改坏了:重建后只剩事件日志推得出来的。"""
    ws = tmp_path / "ws"
    play(ws)
    live = index_dump(ws)
    ghost_batch = dict(live["batches"][0], batch_id="ghost", phase="已比对")
    ghost_card = dict(live["cards"][0], card_id="ghost/c0", batch_id="ghost", validation="单次观测")
    ghost_method = dict(live["methods"][0], entry_id="EVIDENCE_GHOST_V1", more_state="已复现")
    wrong = dict(live["cards"][1], validation="可泛化", direction="变多")
    store.upsert_card_index(ws, {"batches": [ghost_batch], "cards": [ghost_card, wrong], "methods": [ghost_method]})
    assert index_dump(ws) != live
    cards.rebuild(ws)
    assert index_dump(ws) == live


def test_two_processes_writing_at_once_never_lose_or_mix_events(tmp_path):
    """同一时刻只有一个写者(设计 §6 规则 2):两个进程抢着写,事件一条不少、一行不乱,索引和重建的一致。"""
    import subprocess
    import sys

    ws = tmp_path / "ws"
    script = (
        "import sys\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "from butter import cards\n"
        "from butter.contracts import Batch\n"
        "ws, who = Path(sys.argv[1]), sys.argv[2]\n"
        "for i in range(12):\n"
        "    b = f'{who}-{i}'\n"
        "    d = cards.submit(ws, at=datetime(2026, 11, 1, 9, i, tzinfo=UTC), actor='system', batch_id=b,\n"
        "                     type='batch_created',\n"
        "                     payload=Batch(batch_id=b, site_host='a.test', run_id='r', card_ids=[b + '/c0']))\n"
        "    assert d.ok and not d.notes, (d.reason, d.notes)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(ws), who],
                              cwd=str(PKG_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for who in ("p", "q", "r")]
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")
    events, problems = store.read_card_events(ws)
    assert problems == [] and len(events) == 36 and len({e.event_id for e in events}) == 36
    assert sorted(e.batch_id for e in events) == sorted(f"{w}-{i}" for w in "pqr" for i in range(12))
    live = index_dump(ws)
    assert len(live["batches"]) == 36 and [r["seq"] for r in live["events"]] == list(range(1, 37))
    cards.rebuild(ws)
    assert index_dump(ws) == live


def test_index_rejects_rows_that_do_not_match_its_tables(tmp_path):
    with pytest.raises(ValueError, match="没有这些表"):
        store.upsert_card_index(tmp_path, {"nope": []})
    with pytest.raises(ValueError, match="对不上"):
        store.upsert_card_index(tmp_path, {"methods": [{"entry_id": "x"}]})
    with pytest.raises(ValueError, match="没有这张表"):
        store.read_card_index(tmp_path, "nope")


def test_rebuild_index_command(tmp_path, capsys):
    from butter.cli import main

    ws = tmp_path / "ws"
    play(ws)
    live = index_dump(ws)
    (ws / store.CARD_INDEX).unlink()
    assert main(["rebuild-index", "--workspace", str(ws)]) == 0
    assert "20 条事件,3 个批次,5 张卡" in capsys.readouterr().out and index_dump(ws) == live
