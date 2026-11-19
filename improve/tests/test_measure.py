"""测量:写 Potato 配置、分组、估费、驱动(一条腿一条腿跑、失败太多就停、断了接着跑、同一批只许一个驱动)、读回结果。

全部用假的 Potato(tests/stubs/potato),不联网、不花钱。假 Potato 学的是真 Potato 的命令行、文件和退出码。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from butter import cards, sources, store
from butter.contracts import DeployMark, PotatoSettings
from butter.jobs import JobManager, create_batch
from butter.measure import driver
from butter.measure import plan as P
from butter.pipeline import RunOptions, process_snapshot
from butter.questions import draft_questions, to_board_questions
from butter.web.app import WebSettings, create_app

from conftest import NOW

STUB = Path(__file__).parent / "stubs" / "potato"
ENV = {"ANTHROPIC_API_KEY": "fake-a", "OPENAI_API_KEY": "fake-o"}


@pytest.fixture()
def site(tmp_path, acme_snapshot, monkeypatch):
    for name in ("STUB_MENTION_RATE", "STUB_MENTION_BOOST", "STUB_FAIL", "STUB_CRASH_AFTER", "STUB_SEED", "STUB_SLEEP"):
        monkeypatch.delenv(name, raising=False)
    out, workspace = tmp_path / "out", tmp_path / "workspace"
    result = process_snapshot(acme_snapshot, RunOptions(out_root=out), now=lambda: NOW, run_id="run-m")
    batch_id, problems = create_batch(workspace, result.bundle.package, site_host=result.factbase.host, industry="",
                                      lang="en")
    assert batch_id and not problems
    questions = draft_questions(sources.load_question_templates(), lang="en", categories=["kettles"], brand="Acme Kettles")
    panel = {"schema_version": "panel/1", "run_id": "run-m", "saved_at": "2026-11-19T00:00:00+00:00",
             "source": "butter_generated", "template_version": "qt-1", "lang": "en", "categories": ["kettles"],
             "brand": "Acme Kettles", "questions": to_board_questions(questions)}
    store.save_text_files(workspace / "panels" / "run-m" / "p1", {"panel.json": json.dumps(panel, ensure_ascii=False)})
    potato = PotatoSettings(potato_dir=str(tmp_path), python=sys.executable, pythonpath=str(STUB))
    return workspace, out, batch_id, potato


def phase(workspace, batch_id) -> str:
    view, _ = cards.load(workspace)
    return next(b for b in view.batches if b.batch.batch_id == batch_id).phase


def state_of(workspace, batch_id):
    view, _ = cards.load(workspace)
    return next(b for b in view.batches if b.batch.batch_id == batch_id)


# ── 纯层 ──────────────────────────────────────────────────────────────

def test_potato_config_is_valid_and_versions_follow_content(acme_factbase):
    brand = P.brand_block(acme_factbase)
    assert brand["name"] == "Acme Kettles" and brand["is_focal"] and brand["domains"]
    qs = [{"id": "g1-d01", "intent": "discovery", "text": "What are the best kettles?", "edited": False}]
    files = P.potato_config(qs, brand, lang="en", runs=3, budget_cap_usd=15)
    q, b = yaml.safe_load(files["questions.yml"]), yaml.safe_load(files["brands.yml"])
    assert (q["locale"], q["region"], q["n_runs"], q["budget_cap_usd"]) == ("en-US", "US", 3, 15)
    assert q["questions"] == [{"id": "g1-d01", "intent": "discovery", "text": "What are the best kettles?"}]  # 多余的键不写
    assert b["peers"] == [brand] and b["focal_domain"] == brand["domains"][0] and b["competitors_on"] is False
    again = P.potato_config(qs, brand, lang="en", runs=3, budget_cap_usd=15)
    changed = P.potato_config([{**qs[0], "text": "Which kettles are good?"}], brand, lang="en", runs=3, budget_cap_usd=15)
    assert again == files and yaml.safe_load(changed["questions.yml"])["prompt_version"] != q["prompt_version"]
    assert yaml.safe_load(P.potato_config(qs, brand, lang="zh", runs=3, budget_cap_usd=15)["questions.yml"])["locale"] == "zh-CN"
    with pytest.raises(ValueError):
        P.potato_config(qs, brand, lang="fr", runs=3, budget_cap_usd=15)


def test_questions_are_split_into_treated_control_and_background():
    pages = {"q1": ["https://x.test/a", "https://x.test/b"], "q2": ["https://x.test/b/"], "q3": [], "b1": ["https://x.test/a"]}
    treated, control, where = P.split_questions(pages, {"b1"}, {"https://x.test/a/"})
    assert treated == ["q1"] and control == ["q2"] and where == {"q1": "https://x.test/a", "q2": "https://x.test/b/"}
    assert P.page_of_target("https://x.test/a 的 <title> / H1") == "https://x.test/a"
    assert P.page_of_target("https://x.test/a:<head> 里的 <meta>") == "https://x.test/a"
    assert P.page_of_target("站点根目录的 robots.txt") is None


def test_potato_estimate_text_is_parsed():
    text = ("[预算估计] x\n  调用数: 24 题 × 3 轮 = 72 次(每个表面)\n"
            "  - anthropic:claude-sonnet-5  预估(保守上界) ≈ $9.07  [OK]\n"
            "  - openai:gpt-6-luna  预估(保守上界) ≈ $2.58  [OK]\n  合计(保守上界): $11.65\n")
    assert P.parse_potato_estimate(text) == {("anthropic", "claude-sonnet-5"): 9.07, ("openai", "gpt-6-luna"): 2.58}
    assert P.parse_potato_estimate("看不懂的输出") == {}


def test_reading_counts_only_clear_mentions_of_the_focal_brand():
    data = {"run_id": "r", "has_extraction": True,
            "manifest": {"model": "m", "surface_id": "s", "tool_version": "t", "config_fingerprint": "f", "cost_usd_actual": 0.5},
            "lock": {},
            "responses": [{"question_id": "q1", "run_index": i, "finished_ok": True, "error_code": None, "search_count": 1} for i in range(3)]
            + [{"question_id": "q2", "run_index": 0, "finished_ok": False, "error_code": "rate_limit", "search_count": 0},
               {"question_id": "q2", "run_index": 1, "finished_ok": True, "error_code": None, "search_count": 0}],
            "mentions": [{"question_id": "q1", "run_index": 0, "brand": "Acme", "mentioned": True, "counted": True},
                         {"question_id": "q1", "run_index": 1, "brand": "Acme", "mentioned": True, "counted": False},  # 歧义别名:不算
                         {"question_id": "q1", "run_index": 2, "brand": "Rival", "mentioned": True, "counted": True},
                         {"question_id": "q2", "run_index": 1, "brand": "Acme", "mentioned": False, "counted": True}],
            "owned_citations": [{"question_id": "q1", "run_index": 2, "brand": "Acme"}]}
    r = P.reading_from_bridge(data, lang="en", provider="openai", model="m", focal="Acme", question_ids=["q1", "q2"],
                              runs=3, min_success=0.6)
    assert (r.ok, r.expected, r.complete, r.cost_usd, r.searched_share) == (4, 6, True, 0.5, 0.75)
    assert [(q.question_id, q.ok, q.mention_hits, q.citation_hits) for q in r.questions] == [("q1", 3, 1, 1), ("q2", 1, 0, 0)]
    assert not P.reading_from_bridge(data, lang="en", provider="openai", model="m", focal="Acme",
                                     question_ids=["q1", "q2"], runs=3, min_success=0.9).complete      # 成功率不够
    assert not P.reading_from_bridge({**data, "manifest": None}, lang="en", provider="openai", model="m", focal="Acme",
                                     question_ids=["q1", "q2"], runs=3, min_success=0.6).complete      # 没跑完
    sample = P.leg_sample(r, ["q1"], 3)
    assert sample.mention_rate == pytest.approx(1 / 3, abs=1e-3) and sample.n_questions == 1
    pair = P.leg_data(r, r, "mention")
    assert [(o.question_id, o.pre_hits, o.pre_n, o.post_hits, o.post_n) for o in pair.questions] == [("q1", 1, 3, 1, 3), ("q2", 0, 1, 0, 1)]
    other = r.model_copy(update={"config_fingerprint": "g", "model": "m2"})
    assert P.config_mismatch(r, r) == [] and P.config_mismatch(r, other) == ["题板、品牌表或采样设置", "引擎型号"]


# ── 假 Potato 上走通 ──────────────────────────────────────────────────

def test_prepare_shows_what_will_be_measured_and_what_it_costs(site):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env=ENV, potato=potato)
    assert [(leg.lang, leg.provider, leg.model, leg.calls) for leg in plan.legs] == [
        ("en", "anthropic", "claude-sonnet-5", 72), ("en", "openai", "gpt-6-luna", 72)]
    assert plan.typical_usd == pytest.approx(72 * 0.05 + 72 * 0.022, abs=0.01) and plan.upper_usd is not None
    assert plan.minutes == pytest.approx(72 * 30 / 60 + 72 * 18 / 60, abs=1)
    assert plan.n_treated == 20 and plan.n_control == 0          # 只卖一种东西的站:题都落在被改的页面上
    assert plan.band_pp and 5 < plan.band_pp < 15 and len(plan.freeze.rules.legs) == 4
    cfg = Path(plan.config_dirs["en"])
    assert (cfg / "questions.yml").is_file() and (cfg / "brands.yml").is_file()
    assert driver.prepare(workspace, out, batch_id, env=ENV, potato=potato).config_dirs == plan.config_dirs  # 内容不变不重写
    broken = PotatoSettings(potato_dir=potato.potato_dir, python=str(Path(potato.potato_dir) / "no-such-python"))
    again = driver.prepare(workspace, out, batch_id, env=ENV, potato=broken)       # 估过一次就记下了,不用再叫 Potato
    assert again.upper_usd == plan.upper_usd and again.notes == plan.notes
    assert phase(workspace, batch_id) == "已出包"                  # 只是算一算,什么都没开始
    only_one = driver.prepare(workspace, out, batch_id, env={"OPENAI_API_KEY": "k"}, potato=potato)
    assert [leg.provider for leg in only_one.legs] == ["openai"]
    with pytest.raises(ValueError, match="key"):
        driver.prepare(workspace, out, batch_id, env={}, potato=potato)
    with pytest.raises(ValueError, match="没有这一批"):
        driver.prepare(workspace, out, "b-nope", env=ENV, potato=potato)


def test_no_panel_means_no_measurement(tmp_path, acme_snapshot):
    out, workspace = tmp_path / "out", tmp_path / "workspace"
    result = process_snapshot(acme_snapshot, RunOptions(out_root=out), now=lambda: NOW, run_id="run-n")
    batch_id, _ = create_batch(workspace, result.bundle.package, site_host=result.factbase.host, industry="", lang="en")
    with pytest.raises(ValueError, match="出题和勾选"):
        driver.prepare(workspace, out, batch_id, env=ENV,
                       potato=PotatoSettings(potato_dir=str(tmp_path), python=sys.executable, pythonpath=str(STUB)))


def test_baseline_then_remeasure_end_to_end(site, monkeypatch):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env=ENV, potato=potato)
    assert driver.start(workspace, plan) is None and phase(workspace, batch_id) == "测基线中"
    assert "2/2 条腿测完整" in driver.run(workspace, batch_id, "baseline", env=ENV, potato=potato)
    state = state_of(workspace, batch_id)
    assert state.phase == "基线就绪" and state.batch.frozen and len(state.baseline.legs) == 2
    assert all(leg.ok == 72 and leg.expected == 72 and leg.mention_rate is not None for leg in state.baseline.legs)
    bdir = driver.batch_dir(workspace, batch_id)
    reading = json.loads((bdir / "readings" / "baseline-1.json").read_text(encoding="utf-8"))
    assert len(reading["legs"]) == 2 and all(leg["complete"] and len(leg["questions"]) == 24 for leg in reading["legs"])
    log = (bdir / "logs" / "measure-baseline-1.log").read_text(encoding="utf-8")
    assert "PROGRESS probe 72/72" in log and "fake-a" not in log                         # key 不进日志
    assert "fake-a" not in (bdir / "measure" / "baseline-1.jsonl").read_text(encoding="utf-8")
    assert driver.current_progress(workspace, batch_id) == ("", [], False)                # 测完了,没有进行中的

    view, _ = cards.load(workspace)
    card_ids = [c.card_id for c in view.cards if c.batch_id == batch_id]
    day = datetime.now().date().isoformat()
    assert cards.submit(workspace, at=datetime.now(UTC), actor="owner", batch_id=batch_id, type="deployed",
                        payload=DeployMark(day=day, card_ids=card_ids)).ok
    monkeypatch.setenv("STUB_SEED", "after")
    monkeypatch.setenv("STUB_MENTION_BOOST", ",".join(plan.freeze.treated_q) + "=0.95")    # 改完之后处理组提得多了
    assert driver.start(workspace, plan, "remeasure") is None and phase(workspace, batch_id) == "再测中"
    assert "2/2 条腿测完整" in driver.run(workspace, batch_id, "remeasure", env=ENV, potato=potato)
    state = state_of(workspace, batch_id)
    assert state.phase == "已比对" and len(state.readings) == 1
    legs = state.readings[0].legs
    mention = [leg for leg in legs if leg.metric == "mention"]
    assert len(legs) == 4 and len(mention) == 2
    assert all(leg.label == "up" and leg.no_control and leg.d_T > 0.3 for leg in mention)   # 明显变多,但没有对照
    assert not state.readings[0].scoring                                                     # 当天再测:不到建议间隔,只作记录
    view, _ = cards.load(workspace)
    assert all(c.validation != "单次观测" for c in view.cards if c.batch_id == batch_id)


def test_a_leg_with_too_many_failures_is_stopped_and_the_next_still_runs(site, monkeypatch):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env=ENV, potato=potato)
    driver.start(workspace, plan)
    monkeypatch.setenv("STUB_FAIL", "2")                                # 每两条失败一条
    monkeypatch.setenv("STUB_SLEEP", "0.03")                            # 真的 Potato 一次要十几秒;这里让假的也慢一点
    result = driver.run(workspace, batch_id, "baseline", env=ENV, potato=potato)
    assert "没有一条腿测完整" in result
    bdir = driver.batch_dir(workspace, batch_id)
    last = driver.leg_progress(bdir, "baseline-1")
    assert {k: v.state for k, v in last.items()} == {("en", "anthropic"): "中止", ("en", "openai"): "中止"}  # 两条腿都跑了、都停了
    assert all(20 <= v.done < 72 and "失败" in v.note for v in last.values())           # 问满 20 次后才停,没有问完
    state = state_of(workspace, batch_id)
    assert state.phase == "已出包" and state.baseline is None and state.measure_failures


def test_a_crashed_leg_is_resumed_with_the_same_run(site, monkeypatch):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env={"OPENAI_API_KEY": "k"}, potato=potato)
    driver.start(workspace, plan)
    monkeypatch.setenv("STUB_CRASH_AFTER", "10")
    assert "没有一条腿测完整" in driver.run(workspace, batch_id, "baseline", env={"OPENAI_API_KEY": "k"}, potato=potato)
    bdir = driver.batch_dir(workspace, batch_id)
    crashed = driver.leg_progress(bdir, "baseline-1")[("en", "openai")]
    assert crashed.state == "不完整" and crashed.done == 10 and crashed.run_id
    monkeypatch.delenv("STUB_CRASH_AFTER")
    assert driver.start(workspace, plan) is None                         # 站长再点一次
    assert "1/1 条腿测完整" in driver.run(workspace, batch_id, "baseline", env={"OPENAI_API_KEY": "k"}, potato=potato)
    done = driver.leg_progress(bdir, "baseline-1")[("en", "openai")]
    assert done.state == "完成" and done.run_id == crashed.run_id and done.done == 72   # 同一个 run_id 接着问,没有重问
    rows = store.read_jsonl(bdir / "potato" / "en" / "raw" / "raw_responses.jsonl")
    assert len(rows) == 72 and len({(r["question_id"], r["run_index"]) for r in rows}) == 72
    assert phase(workspace, batch_id) == "基线就绪"


def test_only_one_driver_per_batch_and_wrong_phase_does_nothing(site):
    workspace, out, batch_id, potato = site
    assert "没有开始测" in driver.run(workspace, batch_id, "baseline", env=ENV, potato=potato)     # 还没点"测一次"
    plan = driver.prepare(workspace, out, batch_id, env=ENV, potato=potato)
    driver.start(workspace, plan)
    with store.hold_lock(driver.batch_dir(workspace, batch_id) / "measure.lock"):
        assert store.lock_is_held(driver.batch_dir(workspace, batch_id) / "measure.lock")
        assert "已经有一个测量在跑" in driver.run(workspace, batch_id, "baseline", env=ENV, potato=potato)
    assert not store.lock_is_held(driver.batch_dir(workspace, batch_id) / "measure.lock")
    assert phase(workspace, batch_id) == "测基线中"
    with pytest.raises(ValueError):
        driver.batch_dir(workspace, "../escape")


def test_missing_key_at_run_time_is_reported_not_hidden(site):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env=ENV, potato=potato)
    driver.start(workspace, plan)
    driver.run(workspace, batch_id, "baseline", env={"OPENAI_API_KEY": "k"}, potato=potato)        # Claude 的 key 没了
    last = driver.leg_progress(driver.batch_dir(workspace, batch_id), "baseline-1")
    assert last[("en", "anthropic")].state == "失败" and "key" in last[("en", "anthropic")].note
    assert last[("en", "openai")].state == "完成" and phase(workspace, batch_id) == "基线就绪"       # 另一条腿照样测完


def test_detached_driver_finishes_on_its_own(site, monkeypatch):
    """网页点了按钮之后起的是一个独立进程:这里真起一个,等它自己跑完。"""
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env={"OPENAI_API_KEY": "k"}, potato=potato)
    driver.start(workspace, plan)
    driver.spawn(workspace, out, batch_id, "baseline", env={"OPENAI_API_KEY": "k"}, potato=potato)
    for _ in range(120):
        if phase(workspace, batch_id) != "测基线中":
            break
        time.sleep(0.5)
    assert phase(workspace, batch_id) == "基线就绪"
    for _ in range(20):
        if not store.lock_is_held(driver.batch_dir(workspace, batch_id) / "measure.lock"):
            break
        time.sleep(0.25)
    assert not store.lock_is_held(driver.batch_dir(workspace, batch_id) / "measure.lock")


def test_read_bridge_runs_in_the_other_interpreter(site):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env={"OPENAI_API_KEY": "k"}, potato=potato)
    driver.start(workspace, plan)
    driver.run(workspace, batch_id, "baseline", env={"OPENAI_API_KEY": "k"}, potato=potato)
    bdir = driver.batch_dir(workspace, batch_id)
    run_id = driver.leg_progress(bdir, "baseline-1")[("en", "openai")].run_id
    env = driver.potato_env({"OPENAI_API_KEY": "k"}, potato)
    done = subprocess.run([sys.executable, "-B", str(driver.BRIDGE), str(bdir / "potato" / "en"), run_id],
                          capture_output=True, env=env, timeout=60)
    data = json.loads(done.stdout.decode("utf-8"))
    assert done.returncode == 0 and len(data["responses"]) == 72 and data["has_extraction"] and data["manifest"]["model"] == "gpt-6-luna"
    missing = subprocess.run([sys.executable, "-B", str(driver.BRIDGE), str(bdir / "nope"), run_id], capture_output=True,
                             env=env, timeout=60)
    assert missing.returncode == 2 and not (bdir / "nope").exists()                 # 不存在的目录:报错,不顺手建出来


# ── 网页:「测一次」、进度、接着测 ────────────────────────────────────

BASE = "http://127.0.0.1:8700"
FORM = {"data": {"token": "t"}, "headers": {"origin": BASE}, "follow_redirects": False}


def run_now(workspace, out_root, batch_id, kind, *, env, potato):
    """测试里代替"起一个独立进程":当场跑完。"""
    driver.run(workspace, batch_id, kind, env=env, potato=potato)


def web(site, *, env=ENV, spawn=run_now) -> TestClient:
    workspace, out, _, potato = site
    jobs = JobManager(out, workspace, env=env, fetch=lambda url, max_pages: None, start_worker=False)
    settings = WebSettings(out_root=out, workspace=workspace, token="t", env=env, jobs=jobs, potato=potato, spawn=spawn)
    return TestClient(create_app(settings), base_url=BASE)


def test_batch_page_shows_cost_and_time_before_anything_is_spent(site):
    workspace, _, batch_id, _ = site
    client = web(site)
    data = client.get(f"/batches/{batch_id}.json").json()
    plan = data["plan"]
    assert data["can_measure"] and not data["measuring"] and not data["measured_before"]
    assert (plan["questions"], plan["runs"], plan["n_treated"]) == (24, 3, 20)
    assert [(leg["name"], leg["calls"]) for leg in plan["legs"]] == [("Anthropic(Claude） · 英文", 72), ("OpenAI · 英文", 72)]
    assert plan["typical_usd"] == pytest.approx(5.18, abs=0.01) and plan["upper_usd"] is not None and plan["band_pp"]
    html = client.get(f"/batches/{batch_id}").text
    assert "测一次：预计 $5.18" in html and "个百分点以内" in html and f'action="/batches/{batch_id}/baseline"' in html
    assert "<script" not in html.lower() and "fake-a" not in html and 'http-equiv="refresh"' not in html
    assert phase(workspace, batch_id) == "已出包"                              # 打开页面不会开始测

    no_key = web(site, env={})
    data = no_key.get(f"/batches/{batch_id}.json").json()
    assert data["plan"] is None and "key" in data["plan_error"]
    assert "现在还测不了" in no_key.get(f"/batches/{batch_id}").text
    assert no_key.post(f"/batches/{batch_id}/baseline", **FORM).status_code == 400
    assert phase(workspace, batch_id) == "已出包"


def test_measure_button_runs_the_baseline_and_shows_what_was_found(site):
    workspace, _, batch_id, _ = site
    client = web(site)
    assert client.post(f"/batches/{batch_id}/baseline", data={"token": "wrong"}, headers={"origin": BASE}).status_code == 403
    assert client.post(f"/batches/{batch_id}/baseline", data={"token": "t"}).status_code == 403   # 别的网站发来的
    assert phase(workspace, batch_id) == "已出包"
    done = client.post(f"/batches/{batch_id}/baseline", **FORM)
    assert done.status_code == 303 and done.headers["location"] == f"/batches/{batch_id}"
    data = client.get(f"/batches/{batch_id}.json").json()
    assert data["phase_text"] == "改之前测过了" and data["measured_before"] and data["can_measure"]
    assert [(leg["name"], leg["ok"], leg["expected"]) for leg in data["baseline_legs"]] == [
        ("Anthropic(Claude） · 英文", 72, 72), ("OpenAI · 英文", 72, 72)]
    assert all(0 <= leg["mention_rate"] <= 1 for leg in data["baseline_legs"])
    html = client.get(f"/batches/{batch_id}").text
    assert "改之前测到的" in html and "再测一次改之前的情况" in html


def test_page_follows_a_running_measurement_and_offers_to_resume_a_dead_one(site, monkeypatch):
    workspace, _, batch_id, _ = site
    started = []
    monkeypatch.setattr(driver, "wait_started", lambda *a, **k: False)
    client = web(site, spawn=lambda workspace, out_root, batch_id, kind, *, env, potato: started.append(kind))
    assert client.post(f"/batches/{batch_id}/resume", **FORM).status_code == 400       # 没在测,没什么可接的
    assert client.post(f"/batches/{batch_id}/baseline", **FORM).status_code == 303 and started == ["baseline"]
    assert client.post(f"/batches/{batch_id}/baseline", **FORM).status_code == 400     # 正在测,不能再开一次
    lock = driver.batch_dir(workspace, batch_id) / "measure.lock"
    with store.hold_lock(lock):                                                          # 驱动在跑
        data = client.get(f"/batches/{batch_id}.json").json()
        assert data["measuring"] and data["driver_alive"] and not data["can_measure"] and data["plan"] is None
        html = client.get(f"/batches/{batch_id}").text
        assert 'http-equiv="refresh" content="5"' in html and "/resume" not in html
        assert client.post(f"/batches/{batch_id}/resume", **FORM).status_code == 303 and started == ["baseline"]  # 不会起第二个
    html = client.get(f"/batches/{batch_id}").text                                        # 驱动没了,但还没测完
    assert "测到一半停了" in html and f'action="/batches/{batch_id}/resume"' in html and 'http-equiv="refresh"' not in html
    assert client.post(f"/batches/{batch_id}/resume", **FORM).status_code == 303 and started == ["baseline", "baseline"]


def test_progress_rows_show_on_the_page_while_measuring(site, monkeypatch):
    workspace, out, batch_id, potato = site
    monkeypatch.setattr(driver, "wait_started", lambda *a, **k: False)
    client = web(site, env={"OPENAI_API_KEY": "k"}, spawn=lambda *a, **k: None)
    client.post(f"/batches/{batch_id}/baseline", **FORM)
    monkeypatch.setenv("STUB_CRASH_AFTER", "10")
    bdir = driver.batch_dir(workspace, batch_id)
    penv = driver.potato_env({"OPENAI_API_KEY": "k"}, potato)
    state = state_of(workspace, batch_id)
    cfg = str(Path(state.batch.panel[0].path).parent)
    driver._run_leg(bdir, "baseline-1", state.batch.surfaces[0], cfg, potato=potato, env=penv, resume_run_id="",
                    abort_rate=0.1, abort_min=20, total=72)
    legs = client.get(f"/batches/{batch_id}.json").json()["legs"]
    assert [(leg["name"], leg["state"], leg["done"], leg["total"]) for leg in legs] == [("OpenAI · 英文", "不完整", 10, 72)]
    assert "10 / 72" in client.get(f"/batches/{batch_id}").text


# ── 没有波动数据的那家(Grok):照测、照列数字,不判变没变 ────────────────

GROK_ENV = {"OPENAI_API_KEY": "fake-o", "XAI_API_KEY": "fake-x"}


def test_an_engine_without_noise_data_is_measured_and_listed_but_not_judged(site, monkeypatch):
    workspace, out, batch_id, potato = site
    plan = driver.prepare(workspace, out, batch_id, env=GROK_ENV, potato=potato)
    assert [(leg.provider, leg.model, leg.judged) for leg in plan.legs] == [("openai", "gpt-6-luna", True), ("grok", "grok-4.3", False)]
    assert any("Grok" in n and "说不了变没变" in n for n in plan.notes)
    cfg = yaml.safe_load((Path(plan.config_dirs["en"]) / "questions.yml").read_text(encoding="utf-8"))
    assert cfg["budget_cap_usd"] == 18.0                      # 每问一次最多 $0.25 × 72 次;真 Potato 给 Grok 估 $15.12,放得过
    rules = {(r.provider, r.metric): r for r in plan.freeze.rules.legs}
    assert rules[("grok", "mention")].band_pp is None and rules[("openai", "mention")].band_pp is not None
    driver.start(workspace, plan)
    assert "2/2 条腿测完整" in driver.run(workspace, batch_id, "baseline", env=GROK_ENV, potato=potato)
    log = (driver.batch_dir(workspace, batch_id) / "logs" / "measure-baseline-1.log").read_text(encoding="utf-8")
    assert "fake-x" not in log
    state = state_of(workspace, batch_id)
    assert [leg.provider for leg in state.baseline.legs] == ["openai", "grok"] and state.baseline.legs[1].mention_rate is not None

    view, _ = cards.load(workspace)
    card_ids = [c.card_id for c in view.cards if c.batch_id == batch_id]
    cards.submit(workspace, at=datetime.now(UTC), actor="owner", batch_id=batch_id, type="deployed",
                 payload=DeployMark(day=datetime.now().date().isoformat(), card_ids=card_ids))
    monkeypatch.setenv("STUB_SEED", "after")
    monkeypatch.setenv("STUB_MENTION_BOOST", ",".join(plan.freeze.treated_q) + "=0.95")
    driver.start(workspace, plan, "remeasure")
    driver.run(workspace, batch_id, "remeasure", env=GROK_ENV, potato=potato)
    reading = state_of(workspace, batch_id).readings[0]
    grok = next(leg for leg in reading.legs if leg.provider == "grok" and leg.metric == "mention")
    assert grok.label == "not_comparable" and grok.d_T > 0.3 and grok.band_T is None   # 数有,判没有
    line = next(text for text in reading.lines if "grok" in text and "提及" in text)
    assert "改前" in line and "改后" in line and "没有这条腿的波动数据" in line
    assert reading.judged == 2                                                         # 只有 OpenAI 的两项算判过


def test_page_marks_the_engine_that_is_only_listed(site):
    _, _, batch_id, _ = site
    client = web(site, env=GROK_ENV)
    data = client.get(f"/batches/{batch_id}.json").json()
    assert [(leg["name"], leg["judged"]) for leg in data["plan"]["legs"]] == [("OpenAI · 英文", True), ("xAI(Grok） · 英文", False)]
    html = client.get(f"/batches/{batch_id}").text
    assert "只列数字" in html and "fake-x" not in html
    keys = client.get("/settings.json").json()["keys"]
    assert [(k["name"], k["ready"]) for k in keys][-1] == ("xAI(Grok）", True)


def test_plan_says_so_when_potato_would_refuse_an_engine_for_cost(site, monkeypatch):
    workspace, out, batch_id, potato = site
    monkeypatch.setenv("STUB_COST", "0.1")     # 假 Potato 的估价是每次花费的 3 倍:72 × 0.3 = $21.6,超过上限 $18
    plan = driver.prepare(workspace, out, batch_id, env={"OPENAI_API_KEY": "k"}, potato=potato)
    assert any("OpenAI" in n and "$21.60" in n and "$18.00" in n and "拦下" in n for n in plan.notes)
