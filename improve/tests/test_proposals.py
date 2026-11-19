"""给 Toast 的提案文档(决定 4、决定 7;设计 §8.5):新增类、修改类、按卡片结果的升级与降级。只出文档,不碰 Toast。"""

from __future__ import annotations

import re

import pytest
import yaml

from butter import store
from butter.contracts import (
    EvidenceEntry, MethodDirection, MethodExperiment, MethodSummary, OwnerNote, RegistrySource,
)
from butter.proposals import (
    AMENDMENT_FILE, AMENDMENTS, EXTENSION_FILE, README_FILE, REPLICATION_FILE, TODO, WHY_ADD, amendment_proposal,
    build_proposals, extension_proposal, replicated, replication_proposal, resolve_toast_id,
)
from butter.proposals.notes import ADDED_1114
from butter.registry import EXTENSION_PATH, SNAPSHOT_PATH, Registry, ids, load_registry, read_registry_file
from test_cards import DOWN, FLAT, UP, Sim, full

DAY = "2026-11-19"
# 用自带快照,不依赖这台机器上有没有 Toast
REGISTRY = Registry([read_registry_file(SNAPSHOT_PATH, origin="toast_snapshot"),
                     read_registry_file(EXTENSION_PATH, origin="butter_extension")])
EXT_IDS = [e["id"] for e in yaml.safe_load(EXTENSION_PATH.read_text(encoding="utf-8"))["entries"]]


def section(doc: str, heading_part: str) -> str:
    """取一节(从含 heading_part 的二级标题到下一个二级标题或分隔线)。"""
    parts = re.split(r"\n(?=## )|\n---\n", doc)
    return next(p for p in parts if p.startswith("## ") and heading_part in p.splitlines()[0])


# ── 一、新增类 ────────────────────────────────────────────────────────

def test_extension_proposal_lists_all_13_entries_with_id_level_both_languages_sources_and_reason():
    doc = extension_proposal(REGISTRY, generated_on=DAY)
    assert len(EXT_IDS) == 13 and set(EXT_IDS) == set(ids.BUTTER_EXTENSION)
    assert "新增下面 13 条" in doc and "现有 20 条" in doc and "Butter 不改 Toast 的文件" in doc
    for i, entry_id in enumerate(EXT_IDS, 1):
        entry = REGISTRY.get(entry_id)
        part = section(doc, f"`{entry_id}`")
        assert part.startswith(f"## {i}. `{entry_id}`")                         # 顺序同扩展文件
        assert f"级别:{entry.evidence_level}(" in part and entry.stage in part and entry.kind in part
        assert entry.hypothesis["zh"] in part and entry.hypothesis["en"] in part  # 中英文字
        assert entry.plain["zh"] in part and entry.plain["en"] in part
        assert all(s in part for s in entry.sources["zh"] + entry.sources["en"])  # 来源
        why = next(line for line in part.splitlines() if line.startswith("- 为什么建议加:"))
        assert TODO not in why and len(why) > 30                                 # 为什么建议加:13 条都有依据
    assert doc.count("| `EVIDENCE_") == 13  # 一页速览:一条一行


def test_every_extension_entry_has_a_documented_reason_and_unknown_ones_say_todo():
    assert set(WHY_ADD) | set(ADDED_1114) == set(EXT_IDS) and not set(WHY_ADD) & set(ADDED_1114)
    extra = REGISTRY.get(ids.TITLE_QUERY_MATCH).model_copy(update={"id": "EVIDENCE_SOMETHING_NEW_V1"})
    source = RegistrySource(origin="butter_extension", path="x.yaml", sha256="0" * 64, entries=1)
    registry = Registry([read_registry_file(SNAPSHOT_PATH, origin="toast_snapshot"), (source, [extra])])
    doc = extension_proposal(registry, generated_on=DAY)
    assert "- 为什么建议加:待补" in doc  # 核不到依据的写"待补",不编


# ── 二、修改类 ────────────────────────────────────────────────────────

def test_amendment_ids_are_checked_against_the_toast_registry():
    assert [a.key for a in AMENDMENTS] == ["STRUCT_V1", "STATS_V1 / QUOTABLE_V1", "FAQ_V1", "LLMSTXT_V1",
                                           "THIRDPARTY_COMMUNITY"]
    for short in ("STRUCT_V1", "STATS_V1", "QUOTABLE_V1", "FAQ_V1", "LLMSTXT_V1"):
        assert resolve_toast_id(REGISTRY, short) == f"EVIDENCE_{short}"
    assert resolve_toast_id(REGISTRY, "THIRDPARTY_COMMUNITY") is None       # Toast 里没有这个 ID:照实指出
    assert resolve_toast_id(REGISTRY, "TITLE_QUERY_MATCH") is None          # Butter 扩展里的不算 Toast 现有条目
    assert resolve_toast_id(REGISTRY, "TITLE_QUERY_MATCH_V1") is None


def test_amendment_proposal_shows_current_state_suggestion_basis_and_todo():
    doc = amendment_proposal(REGISTRY, generated_on=DAY)
    for real in ("EVIDENCE_STRUCT_V1", "EVIDENCE_STATS_V1", "EVIDENCE_QUOTABLE_V1", "EVIDENCE_FAQ_V1",
                 "EVIDENCE_LLMSTXT_V1"):
        assert f"条目 `{real}`" in doc and REGISTRY.get(real).hypothesis["zh"] in doc  # 现状取自注册表,不是手抄
    struct = section(doc, "STRUCT_V1")
    assert "改措辞" in struct and "级别:B" in struct and "结构字段承载事实与查询词" in struct
    assert "0.78–1.68" in struct and "待补" in struct and "逐字写法" in struct
    stats = section(doc, "STATS_V1 / QUOTABLE_V1")
    assert "LIVE-GEO" in stats and "2609.07559" in stats and "数值必须可溯源" in stats
    assert "EVIDENCE_NUMERIC_TRACEABILITY_V1" in stats and "已经有「数字无法溯源」" in stats
    faq = section(doc, "FAQ_V1")
    assert "现在就是 D" in faq and "由人定" in faq and "−5.74%" in faq and "2026-05-07" in faq
    assert "C(矛盾)" in faq  # 研究里两处评级不一致,照实列出
    llms = section(doc, "LLMSTXT_V1")
    assert "级别:E" in llms and "97%" in llms and "0.1%" in llms and "Google" in llms
    community = section(doc, "THIRDPARTY_COMMUNITY")
    assert "条目 ID 对不上" in community and "从来没有进过表" in community and "不需要改任何东西" in community
    assert "EVIDENCE_THIRDPARTY_LISTICLE_V1" in community and "0.5%" in community and "待补:无" in community
    assert "| THIRDPARTY_COMMUNITY | 对不上 | — | 维持不进 | 齐 |" in doc
    assert "| FAQ_V1 | `EVIDENCE_FAQ_V1` | D | 降级 |" in doc
    assert all(d.source.startswith(("research/", "Butter 扩展条目")) for a in AMENDMENTS for d in a.details)


def test_amendment_proposal_follows_the_registry_when_toast_changes():
    """Toast 以后把 FAQ 降到 E,或改了 ID:文档跟着注册表走,不写死。"""
    source, entries = read_registry_file(SNAPSHOT_PATH, origin="toast_snapshot")
    changed = [e.model_copy(update={"evidence_level": "E"}) if e.id == "EVIDENCE_FAQ_V1" else e for e in entries
               if e.id != "EVIDENCE_LLMSTXT_V1"]
    doc = amendment_proposal(Registry([(source, changed)]), generated_on=DAY)
    assert "已经是 E,没有可再降的" in section(doc, "FAQ_V1")
    assert "条目 ID 对不上" in section(doc, "LLMSTXT_V1") and "| LLMSTXT_V1 | 对不上 |" in doc


# ── 三、按卡片结果的升级 / 降级 ─────────────────────────────────────────

def methods_after(*runs) -> list[MethodSummary]:
    sim = Sim()
    for i, kw in enumerate(runs, 1):
        full(sim, f"b{i}", **kw)
    return sim.view().methods


def test_no_proposal_until_two_independent_experiments_agree():
    one = methods_after(dict(pairs=(UP, FLAT), site="a.test"))
    assert replicated(one) == ([], [])
    doc = replication_proposal(REGISTRY, one, generated_on=DAY)
    assert "0 个条目够出升级提案,0 个条目够出降级提案" in doc and "升级提案:" not in doc and "降级提案:" not in doc
    assert "变多 1 次,变少 0 次" in doc and "不出提案" in doc
    empty = replication_proposal(REGISTRY, [], generated_on=DAY)
    assert "还没有卡" in empty


def test_two_increases_make_an_upgrade_proposal_that_leaves_the_level_to_people():
    methods = methods_after(dict(pairs=(UP, FLAT), site="a.test"), dict(pairs=(UP, FLAT), site="b.test"))
    ups, downs = replicated(methods)
    assert [m.entry_id for m in ups] == [ids.TITLE_QUERY_MATCH] and downs == []
    doc = replication_proposal(REGISTRY, methods, generated_on=DAY)
    part = section(doc, "升级提案")
    assert f"`{ids.TITLE_QUERY_MATCH}`" in part and "2 次独立实验都测到明显变多" in part and "已复现·变多" in part
    assert "现在的级别:B" in part and "Butter 扩展文件" in part
    assert "调高" in part and "调几级由人定,这里不替人定" in part
    assert not re.search(r"(升|调)(到|为|至)\s*[A-E]\b", part)  # 提案里不替人定调到哪一级
    assert "| b1 | a.test |" in part and "| b2 | b.test |" in part and "b1/c0" in part and "b2/c0" in part
    assert "反方向(变少)的实验:没有" in part and "只在一个行业" in part
    assert "Observed change ≠ proven cause" in doc and "API" in doc and "降级提案:" not in doc
    assert "| 升级提案 |" in doc


def test_two_decreases_make_a_downgrade_proposal():
    methods = methods_after(dict(pairs=(DOWN, FLAT), site="a.test"), dict(pairs=(DOWN, FLAT), site="b.test"))
    ups, downs = replicated(methods)
    assert ups == [] and [m.entry_id for m in downs] == [ids.TITLE_QUERY_MATCH]
    doc = replication_proposal(REGISTRY, methods, generated_on=DAY)
    part = section(doc, "降级提案")
    assert "2 次独立实验都测到明显变少" in part and "调低" in part and "调几级由人定" in part
    assert "升级提案:" not in doc and "| 降级提案 |" in doc


def test_both_directions_are_shown_side_by_side_never_netted():
    methods = methods_after(dict(pairs=(UP, FLAT), site="a.test"), dict(pairs=(UP, FLAT), site="b.test"),
                            dict(pairs=(DOWN, FLAT), site="c.test"))
    doc = replication_proposal(REGISTRY, methods, generated_on=DAY)
    part = section(doc, "升级提案")
    assert "变多 2 次,变少 1 次" in part and "反方向(变少)的实验,照实并列,不互相抵消" in part and "| b3 | c.test |" in part
    assert "降级提案:" not in doc  # 变少只有 1 次,不够

    methods = methods_after(dict(pairs=(UP, FLAT), site="a.test"), dict(pairs=(UP, FLAT), site="b.test"),
                            dict(pairs=(DOWN, FLAT), site="c.test"), dict(pairs=(DOWN, FLAT), site="d.test"))
    ups, downs = replicated(methods)
    assert len(ups) == 1 and len(downs) == 1  # 两边都够:两份提案都出,各自列出对方
    doc = replication_proposal(REGISTRY, methods, generated_on=DAY)
    assert "变多 2 次,变少 2 次" in section(doc, "升级提案") and "变多 2 次,变少 2 次" in section(doc, "降级提案")
    assert "| 升级提案、降级提案 |" in doc


def test_proposal_names_what_was_not_counted_and_counts_butter_generated_panels_as_usual():
    methods = methods_after(
        dict(pairs=(UP, FLAT), site="a.test", source="butter_generated"),
        dict(pairs=(UP, FLAT), site="b.test"),
        dict(pairs=(UP, FLAT), site="b.test", baseline="2026-11-02", deploy="2026-11-04", re_day="2026-12-03"),
        dict(pairs=(UP, FLAT), site="c.test", entries=(ids.TITLE_QUERY_MATCH, ids.PRICE_SPECS_COMPARISON)))
    part = section(replication_proposal(REGISTRY, methods, generated_on=DAY), "升级提案")
    assert "同站、测量时间重叠,没有另计的" in part and "和 b2 同站且测量时间重叠" in part
    # 2026-11-18 用户定:Butter 出题的实验照常算一次;表里标出题从哪来,不另加"降一档、由人定"的话
    assert "2 次独立实验都测到明显变多" in part and "| b1 | a.test |" in part and "Butter 出的题" in part
    assert "降一档" not in part and "自己出题、自己改、自己考" not in part
    assert "分不开是谁起作用的卡(不计数):变多 1 张(b4/c0)" in part


def test_abandoned_batches_are_neither_counted_nor_mentioned():
    """2026-11-18 用户定:站长放弃的批次,不计数,提案里也不写。"""
    sim = Sim()
    full(sim, "b1", (UP, FLAT), site="a.test")
    full(sim, "b2", (UP, FLAT), site="b.test")
    full(sim, "b3", (DOWN, FLAT), site="c.test")
    full(sim, "b4", (UP, FLAT), site="d.test")
    for batch in ("b3", "b4"):
        sim.ok("abandoned", OwnerNote(reason="不要了"), batch=batch)
    doc = replication_proposal(REGISTRY, sim.view().methods, generated_on=DAY)
    assert "变多 2 次,变少 0 次" in doc and "反方向(变少)的实验:没有" in doc
    assert "b3" not in doc and "b4" not in doc and "放弃" not in doc
    sim.ok("abandoned", OwnerNote(reason="不要了"), batch="b2")  # 再放弃一批:只剩一次,不够出提案
    doc = replication_proposal(REGISTRY, sim.view().methods, generated_on=DAY)
    assert "升级提案:" not in doc and "变多 1 次,变少 0 次" in doc and "b2" not in doc


def test_proposal_for_an_entry_missing_from_the_registry_says_todo():
    exp = [MethodExperiment(batch_id=f"b{i}", site_host=f"s{i}.test", direction="变多", baseline_day="2026-11-01",
                            remeasure_day="2026-12-02", card_ids=[f"b{i}/c0"]) for i in (1, 2)]
    ghost = MethodSummary(entry_id="EVIDENCE_GONE_V1", cards_total=2,
                          more=MethodDirection(direction="变多", state="已复现", independent=exp, cards=2),
                          fewer=MethodDirection(direction="变少"))
    doc = replication_proposal(REGISTRY, [ghost], generated_on=DAY)
    assert "现在的级别:待补(注册表里查不到 `EVIDENCE_GONE_V1`)" in doc


# ── 合在一起、落盘、命令行 ─────────────────────────────────────────────

def test_build_proposals_returns_four_documents_and_saving_never_overwrites(tmp_path):
    methods = methods_after(dict(pairs=(UP, FLAT), site="a.test"), dict(pairs=(UP, FLAT), site="b.test"))
    files = build_proposals(REGISTRY, methods, generated_on=DAY)
    assert list(files) == [README_FILE, EXTENSION_FILE, AMENDMENT_FILE, REPLICATION_FILE]
    assert "13 条" in files[README_FILE] and "5 项" in files[README_FILE] and "升级 1 条,降级 0 条" in files[README_FILE]
    assert all(DAY in text and text.endswith("\n") for text in files.values())
    assert files == build_proposals(REGISTRY, methods, generated_on=DAY)  # 同样的输入,同样的文档
    out = tmp_path / "proposals"
    written = store.save_text_files(out, files)
    assert sorted(p.name for p in written) == sorted(files)
    assert (out / AMENDMENT_FILE).read_text(encoding="utf-8") == files[AMENDMENT_FILE]
    with pytest.raises(store.RunDirExists):
        store.save_text_files(out, files)
    with pytest.raises(ValueError):
        store.save_text_files(tmp_path / "x", {"../escape.md": "x"})
    assert not (tmp_path / "x").exists() and not (tmp_path / "escape.md").exists()


def test_proposals_command_writes_documents_into_a_new_folder(tmp_path, capsys):
    from butter.cli import main
    from test_cards import play

    ws = tmp_path / "ws"
    play(ws)  # b1、b2 两个站各测到一次变多
    out = tmp_path / "给Toast的提案"
    args = ["proposals", "--workspace", str(ws), "--out", str(out), "--toast-registry", str(SNAPSHOT_PATH)]
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "没有改 Toast 的任何文件" in printed
    assert sorted(p.name for p in out.iterdir()) == sorted([README_FILE, EXTENSION_FILE, AMENDMENT_FILE, REPLICATION_FILE])
    assert "升级提案:`EVIDENCE_TITLE_QUERY_MATCH_V1`" in (out / REPLICATION_FILE).read_text(encoding="utf-8")
    assert main(args) == 1 and "拒绝覆盖" in capsys.readouterr().err  # 文件夹已存在:不覆盖
    empty_ws = tmp_path / "empty"
    assert main(["proposals", "--workspace", str(empty_ws), "--toast-registry", str(SNAPSHOT_PATH)]) == 0
    made = list((empty_ws / "proposals").iterdir())  # 没有卡片也能出前两份;默认存进工作目录
    assert len(made) == 1 and (made[0] / EXTENSION_FILE).exists()


def test_extension_proposal_works_against_the_live_registry_too():
    """本机有 Toast 就用 Toast 的文件,没有就用快照:两种情况文档都出得来,条数一样。"""
    registry = load_registry()
    doc = extension_proposal(registry, generated_on=DAY)
    assert doc.count("| `EVIDENCE_") == 13 and isinstance(registry.get(ids.STRUCT), EvidenceEntry)
