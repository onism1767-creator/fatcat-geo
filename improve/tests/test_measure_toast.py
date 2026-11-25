"""再测改叫 Toast 的那一套(FatCat 整合,阶段 3):读 Toast 前后两单的下载包,交给比对核心。

下载包的格式照 Toast 的 report/export.py(2026-11-24 对着一单真结果核过字段)。这里用的包是照那个格式造的,不联网、不花钱。
"""

from __future__ import annotations

import json

import pytest

from butter.cli import main
from butter.measure import plan as P
from butter.measure import toast
from butter.sources import SourceFileError

from test_toast_export import make_zip

AIS = (("alpha_search", "alpha-1", "Alpha API · alpha-1"), ("beta_search", "beta-2", "Beta API · beta-2"))
INTENTS = ["category_discovery"] * 6 + ["brand_fact"] * 2          # 1–6 不点名,7–8 点名问品牌
DETECTORS = {"brand_mention_rules": 3, "citation_domain_match": 4, "source_attribution": 1}


def prompts_csv(texts: dict[int, str] | None = None) -> str:
    rows = ["﻿no,prompt_id,intent,origin,locale,text"]
    for no, intent in enumerate(INTENTS, 1):
        rows.append(f"{no},id-{no},{intent},assisted,en-US,{(texts or {}).get(no, f'question {no}')}")
    return "\n".join(rows) + "\n"


def responses(day: str, mention=None, cited=None, *, failed=(), runs: int = 3, ais=AIS, detectors=None,
              searched=True) -> str:
    """mention / cited:{(接口, 题号): [每次的判定]};没列的题三次都是 False。failed:{(接口, 题号, 第几次)}。"""
    lines = []
    for surface, model, ai in ais:
        for no in range(1, len(INTENTS) + 1):
            for index in range(1, runs + 1):
                bad = (surface, no, index) in failed
                verdict = lambda table: (table or {}).get((surface, no), [False] * runs)[index - 1]  # noqa: E731
                lines.append(json.dumps({
                    "run_id": f"{surface}-{no}-{index}", "prompt_id": f"id-{no}-{day}", "prompt_no": no,
                    "run_index": index, "ai": ai, "surface": surface, "model": model, "search_mode": "default",
                    "locale": "en-US", "outcome": "TRANSIENT_FAILURE" if bad else "SUCCESS",
                    "timestamp": f"{day}T12:00:0{index}+00:00", "cost_usd": 0.01, "search_triggered": searched,
                    "judgements": {"mentioned": None if bad else verdict(mention),
                                   "brand_site_cited": None if bad else verdict(cited),
                                   "detectors": detectors or DETECTORS}}))
    return "\n".join(lines) + "\n"


BRAND = json.dumps({"brand": {"name": "Acme", "domains": ["acme.test"]}, "list": None})


def export(tmp_path, name: str, body: str, *, texts=None, brand: str = BRAND):
    return make_zip(tmp_path / name, {"prompts.csv": prompts_csv(texts), "responses.jsonl": body,
                                      "brand-domains.json": brand, "citations.csv": "", "summary.md": ""})


def test_counts_use_the_verdicts_toast_stored(tmp_path):
    body = responses("2026-11-20",
                     mention={("alpha_search", 1): [True, True, False], ("alpha_search", 2): [True, None, None],
                              ("alpha_search", 7): [True, True, True]},
                     cited={("alpha_search", 1): [True, False, None]},
                     failed={("alpha_search", 3, 2)})
    run = toast.load_run(export(tmp_path, "a.zip", body))
    assert run.day == "2026-11-20" and len(run.prompts) == 8 and run.unprompted == ["1", "2", "3", "4", "5", "6"]
    assert run.domains == ["acme.test"]
    leg = run.legs[("alpha_search", "alpha-1")]
    assert (leg.ai, leg.lang, leg.ok, len(leg.rows)) == ("Alpha API · alpha-1", "en", 23, 24)
    mention = toast._per_question(leg, "mention")
    assert mention["1"] == (2, 3)          # 3 次里 2 次提到
    assert mention["2"] == (1, 1)          # 「说不准」的两次不进分母,和 Toast 的报告一个算法
    assert mention["3"] == (0, 2)          # 失败的那一次不算
    cited = toast._per_question(leg, "citation")
    assert cited["1"] == (1, 2)            # 看不到引用的那一次不进分母
    assert toast._searched_share(leg) == 1.0


def test_same_data_gives_the_same_counts_as_the_potato_reading(tmp_path):
    """钉住:没有「说不准」、没有看不到引用的情况时,新读法和原来读 Potato 的读法给出同样的数。"""
    hits = {1: [True, False, True], 2: [False, False, False], 3: [True, True, True], 4: [False, True, False]}
    cites = {1: [False, False, True], 3: [True, True, False]}
    body = responses("2026-11-20", mention={("alpha_search", n): v for n, v in hits.items()},
                     cited={("alpha_search", n): v for n, v in cites.items()}, failed={("alpha_search", 5, 3)})
    leg = toast.load_run(export(tmp_path, "a.zip", body)).legs[("alpha_search", "alpha-1")]
    ids = [str(n) for n in range(1, 9)]
    bridge = {  # 同一批回答,写成 Potato 读取桥给的形状
        "run_id": "r1", "manifest": {"model": "alpha-1"}, "has_extraction": True,
        "responses": [{"question_id": q, "run_index": i, "finished_ok": (q, i) != ("5", 3), "search_count": 1}
                      for q in ids for i in (1, 2, 3)],
        "mentions": [{"question_id": str(n), "run_index": i + 1, "brand": "Acme", "mentioned": v, "counted": v}
                     for n, runs in hits.items() for i, v in enumerate(runs)],
        "owned_citations": [{"question_id": str(n), "run_index": i + 1, "brand": "Acme"}
                            for n, runs in cites.items() for i, v in enumerate(runs) if v],
    }
    old = P.reading_from_bridge(bridge, lang="en", provider="alpha", model="alpha-1", focal="Acme",
                                question_ids=ids, runs=3, min_success=0.9)
    mention, cited = toast._per_question(leg, "mention"), toast._per_question(leg, "citation")
    for q in old.questions:
        assert (q.mention_hits, q.ok) == mention[q.question_id], q.question_id
        assert (q.citation_hits, q.ok) == cited[q.question_id], q.question_id
    assert (old.ok, old.expected, old.searched_share) == (leg.ok, len(leg.rows), toast._searched_share(leg))


def test_before_and_after_are_listed_but_not_judged(tmp_path):
    before = export(tmp_path, "a.zip", responses("2026-11-20", mention={("alpha_search", 1): [True, False, False]}))
    after = export(tmp_path, "b.zip", responses(
        "2026-12-10", mention={("alpha_search", n): [True, True, True] for n in (1, 2, 3)},
        cited={("alpha_search", 1): [True, True, True]}))
    result = toast.compare_runs(toast.load_run(before), toast.load_run(after), deploy_day="2026-11-22")
    assert result["groups"] == {"basis": "all_unprompted", "treated": ["1", "2", "3", "4", "5", "6"], "control": [],
                                "sitewide": True}
    assert result["noise"] == "none" and result["errors"] == [] and result["notes"] == []
    assert [(x["ai"], x["metric"]) for x in result["legs"]] == [
        ("Alpha API · alpha-1", "mention"), ("Alpha API · alpha-1", "citation"),
        ("Beta API · beta-2", "mention"), ("Beta API · beta-2", "citation")]
    leg = result["legs"][0]
    assert leg["counts"] == {"pre": [1, 18], "post": [9, 18]} and leg["n_T"] == 6
    assert leg["pre_T"] == pytest.approx(1 / 18) and leg["post_T"] == pytest.approx(0.5)
    # 变化再大也不判:这套测法上还没有量过平时的波动
    assert (leg["label"], leg["reason"], leg["outcome"]) == ("not_comparable", "没有这条腿的波动数据", "dnf")
    assert result["legs"][1]["counts"] == {"pre": [0, 18], "post": [3, 18]}
    lines = toast.summary_lines(result)
    assert "只列数字,不判变没变" in lines[1] and "1/18 → 9/18" in lines[2]


def test_brand_questions_never_enter_the_groups(tmp_path):
    body = responses("2026-11-20", mention={("alpha_search", 7): [True, True, True], ("alpha_search", 8): [True] * 3})
    run = toast.load_run(export(tmp_path, "a.zip", body))
    result = toast.compare_runs(run, run, deploy_day="2026-11-20")
    assert "7" not in result["groups"]["treated"] and result["legs"][0]["counts"]["pre"] == [0, 18]


def test_the_rules_that_guard_a_comparison_still_apply(tmp_path):
    base = toast.load_run(export(tmp_path, "a.zip", responses("2026-11-20")))
    later = toast.load_run(export(tmp_path, "b.zip", responses("2026-12-10")))
    label = lambda result, i=0: (result["legs"][i]["label"], result["legs"][i]["reason"])  # noqa: E731

    assert label(toast.compare_runs(base, later))[0] == "not_deployed"                      # 没记部署的日子
    assert label(toast.compare_runs(base, later, deploy_day="2026-11-10"))[0] == "baseline_invalid"   # 先部署后测的基线
    assert label(toast.compare_runs(base, later, deploy_day="2026-12-05"))[0] == "baseline_invalid"   # 基线太旧

    reworded = toast.load_run(export(tmp_path, "c.zip", responses("2026-12-10"), texts={2: "another question"}))
    assert label(toast.compare_runs(base, reworded, deploy_day="2026-11-22")) == ("config_changed", "前后不一致:题目")
    newer = toast.load_run(export(tmp_path, "d.zip", responses(
        "2026-12-10", detectors={"brand_mention_rules": 4, "citation_domain_match": 4})))
    result = toast.compare_runs(base, newer, deploy_day="2026-11-22")
    assert label(result, 0) == ("config_changed", "前后不一致:判定方法的版本")       # 提及的判定器换了版本
    assert label(result, 1)[0] == "not_comparable"                                   # 被引的没换,照常列数
    other_list = toast.load_run(export(tmp_path, "e.zip", responses("2026-12-10"),
                                       brand=json.dumps({"brand": {"domains": ["acme.test", "acme.shop"]}})))
    result = toast.compare_runs(base, other_list, deploy_day="2026-11-22")
    assert label(result, 0)[0] == "not_comparable" and label(result, 1) == ("config_changed", "前后不一致:官网名单")

    broken = toast.load_run(export(tmp_path, "f.zip", responses(
        "2026-12-10", failed={("alpha_search", n, i) for n in range(1, 5) for i in (1, 2, 3)})))
    assert label(toast.compare_runs(base, broken, deploy_day="2026-11-22"))[0] == "incomplete"

    unseen = toast.load_run(export(tmp_path, "g.zip", responses("2026-11-20", searched=None)))
    result = toast.compare_runs(unseen, later, deploy_day="2026-11-22")
    assert label(result, 1) == ("not_comparable", "基线里搜索过的回答只占 未知,这条腿不比被引率")


def test_a_model_missing_on_one_side_is_reported_not_guessed(tmp_path):
    base = toast.load_run(export(tmp_path, "a.zip", responses("2026-11-20")))
    swapped = (AIS[0], ("beta_search", "beta-3", "Beta API · beta-3"))
    later = toast.load_run(export(tmp_path, "b.zip", responses("2026-12-10", ais=swapped)))
    result = toast.compare_runs(base, later, deploy_day="2026-11-22")
    assert {x["ai"] for x in result["legs"]} == {"Alpha API · alpha-1"}
    assert any("beta-2" in note and "再测里没有" in note for note in result["notes"])
    assert any("beta-3" in note and "改之前没测过" in note for note in result["notes"])


def test_given_groups_are_used_as_given(tmp_path):
    base = toast.load_run(export(tmp_path, "a.zip", responses("2026-11-20")))
    later = toast.load_run(export(tmp_path, "b.zip", responses("2026-12-10")))
    result = toast.compare_runs(base, later, deploy_day="2026-11-22", treated=["1", "2"], control=["3", "4"])
    assert result["groups"] == {"basis": "given", "treated": ["1", "2"], "control": ["3", "4"], "sitewide": False}
    assert result["legs"][0]["label"] == "not_comparable" and "少于" in result["legs"][0]["reason"]  # 处理组不够 5 题


def test_unreadable_exports_are_refused(tmp_path):
    with pytest.raises(SourceFileError, match="文件不存在"):
        toast.load_run(tmp_path / "nope.zip")
    with pytest.raises(SourceFileError, match="没有 responses.jsonl"):
        toast.load_run(make_zip(tmp_path / "half.zip", {"prompts.csv": prompts_csv()}))
    with pytest.raises(SourceFileError, match="第 1 行读不懂"):
        toast.load_run(make_zip(tmp_path / "bad.zip", {"prompts.csv": prompts_csv(), "responses.jsonl": "{not json\n"}))
    with pytest.raises(SourceFileError, match="没有题目或没有回答"):
        toast.load_run(make_zip(tmp_path / "empty.zip", {"prompts.csv": prompts_csv(), "responses.jsonl": "\n"}))


def test_command_line_writes_the_result_once(tmp_path, capsys):
    before = export(tmp_path, "a.zip", responses("2026-11-20"))
    after = export(tmp_path, "b.zip", responses("2026-12-10", mention={("alpha_search", 1): [True, True, True]}))
    out = tmp_path / "compare.json"
    args = ["compare-toast", "--before", str(before), "--after", str(after), "--deployed", "2026-11-22", "--json", str(out)]
    assert main(args) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["schema"] == "butter.compare-toast/1" and saved["legs"][0]["counts"]["post"] == [3, 18]
    assert "0/18 → 3/18" in capsys.readouterr().out
    assert main(args) == 1                                             # 已经有了就不覆盖
    assert main(["compare-toast", "--before", str(before), "--after", str(after), "--deployed", "not-a-day"]) == 1
    assert main(["compare-toast", "--before", str(tmp_path / "x.zip"), "--after", str(after)]) == 1
