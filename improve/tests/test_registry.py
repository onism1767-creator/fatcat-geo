"""证据注册表:扩展文件按 Toast 的规则加载、合并不撞车、代码里用到的条目 ID 都查得到、快照与记录一致。

本机有 Toast 时,再用 Toast 自己的解释器加载一次扩展文件和合并后的文件,证明「Toast 采纳即可合并」(设计 §11);
同时逐项对照条目模型的字段、默认值和取值范围。子进程带 -B 与 PYTHONDONTWRITEBYTECODE,不在 Toast 的目录里留文件。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import get_args

import pytest
import yaml

from butter.contracts import (
    ACCESS_CHECK_NAMES, DATA_CONDITIONS, EVIDENCE_LEVELS, OUTPUT_LEVELS, REQUIRED_LANGS, EvidenceEntry, PromptIntent,
    Stage,
)
from butter.registry import (
    EXTENSION_PATH, SNAPSHOT_META_PATH, SNAPSHOT_PATH, Registry, RegistryError, content_sha256,
    default_toast_registry_path, ids, load_registry, parse_entries, read_registry_file,
)

PKG = Path(__file__).resolve().parents[1] / "butter"
TOAST_REGISTRY = default_toast_registry_path()
TOAST_DIR = TOAST_REGISTRY.parents[1]
TOAST_PY = next((p for p in (TOAST_DIR / ".venv" / "Scripts" / "python.exe", TOAST_DIR / ".venv" / "bin" / "python")
                 if p.exists()), None)
EXT = yaml.safe_load(EXTENSION_PATH.read_text(encoding="utf-8"))
EXT_ENTRIES = parse_entries(EXT, origin=str(EXTENSION_PATH))

needs_toast = pytest.mark.skipif(not TOAST_REGISTRY.exists(), reason="本机没有 Toast,跳过对照")
needs_toast_python = pytest.mark.skipif(TOAST_PY is None or not TOAST_REGISTRY.exists(),
                                        reason="本机没有 Toast 或它的虚拟环境,跳过对照")


def minimal(**change) -> dict:
    """一条最小的合法条目,测试里逐项改坏它。"""
    entry = {"id": "EVIDENCE_TEST_V1", "stage": "CITATION", "kind": "geo_method",
             "hypothesis": {"zh": "假设", "en": "hypothesis"}, "plain": {"zh": "人话", "en": "plain"},
             "verify": {"zh": "怎么验", "en": "verify"}, "evidence_level": "C",
             "sources": {"zh": ["来源"], "en": ["source"]}}
    entry.update(change)
    return entry


# ── 扩展文件本身 ─────────────────────────────────────────────────────


def test_extension_loads_with_toast_rules_and_matches_the_id_constants():
    assert len(EXT_ENTRIES) == 13
    assert {e.id for e in EXT_ENTRIES} == ids.BUTTER_EXTENSION
    assert all(e.id.startswith("EVIDENCE_") and e.id.endswith("_V1") for e in EXT_ENTRIES)


def test_extension_texts_are_complete_in_both_languages():
    """比 Toast 严一点(只查自己的文件):中英文字都非空;来源、禁忌两种语言条数一样;每条都有护栏。"""
    for e in EXT_ENTRIES:
        for field in ("hypothesis", "plain", "verify"):
            texts = getattr(e, field)
            assert set(texts) == REQUIRED_LANGS and all(t.strip() for t in texts.values()), (e.id, field)
        assert e.sources["zh"] and len(e.sources["zh"]) == len(e.sources["en"]), e.id
        assert len(e.contraindications["zh"]) == len(e.contraindications["en"]), e.id
        assert e.guardrail is not None and all(t.strip() for t in e.guardrail.values()), e.id


def test_geo_methods_read_as_hypotheses_and_e_level_entries_are_archive_only():
    """GEO 方法一律是实验卡:主张用「可能」,人话版写明还没被证明。E 级只存档:不配题型,标禁止。技术修复类都是 A 级。"""
    for e in EXT_ENTRIES:
        if e.evidence_level == "E":
            assert e.kind == "geo_method" and e.supported_intents == [], e.id
            assert "禁止" in e.plain["zh"] and "forbidden" in e.plain["en"].lower(), e.id
        elif e.kind == "geo_method":
            assert "可能" in e.hypothesis["zh"] and " may " in e.hypothesis["en"], e.id
            assert "还没" in e.plain["zh"], e.id
            assert "not proven" in e.plain["en"] or "no intervention" in e.plain["en"], e.id
        else:
            assert e.kind == "technical_fix" and e.evidence_level == "A", e.id


def test_the_two_entries_added_on_11_14():
    by_id = {e.id: e for e in EXT_ENTRIES}
    numeric, jsonld = by_id[ids.NUMERIC_TRACEABILITY], by_id[ids.JSONLD_BODY_CONSISTENCY]
    assert (numeric.stage, numeric.kind, numeric.evidence_level) == ("CITATION", "technical_fix", "A")
    assert (jsonld.stage, jsonld.kind, jsonld.evidence_level) == ("REPRESENTATION", "technical_fix", "A")


# ── 与 Toast 同样的加载规则 ──────────────────────────────────────────


@pytest.mark.parametrize("change, message", [
    ({"extra_field": 1}, "extra_field"),
    ({"hypothesis": {"zh": "只有中文"}}, "zh 与 en"),
    ({"guardrail": {"en": "english only"}}, "zh 与 en"),
    ({"stage": "VISIBILITY"}, "stage"),
    ({"kind": "best_practice"}, "kind"),
    ({"supported_intents": ["discovery"]}, "supported_intents"),
    ({"evidence_level": "F"}, "非法证据等级"),
    ({"access_checks": ["meta_robots"]}, "未知的官网检查项"),
    ({"requires_data": ["has_reviews"]}, "未知的数据条件"),
])
def test_rejects_what_toast_rejects(change, message):
    with pytest.raises(RegistryError, match=message):
        parse_entries({"entries": [minimal(**change)]}, origin="t")


def test_rejects_duplicate_ids_and_files_without_entries():
    with pytest.raises(RegistryError, match="重复 ID"):
        parse_entries({"entries": [minimal(), minimal()]}, origin="t")
    with pytest.raises(RegistryError, match="entries"):
        parse_entries({"items": []}, origin="t")
    assert parse_entries({"entries": [minimal()]}, origin="t")[0].channel == "own"  # 默认值同 Toast


def test_merge_refuses_id_collisions_between_files():
    toast_source, toast_entries = read_registry_file(SNAPSHOT_PATH, origin="toast_snapshot")
    clash = parse_entries({"entries": [minimal(id=ids.STATS)]}, origin="clash")
    clash_source = toast_source.model_copy(update={"origin": "butter_extension", "path": "clash.yaml"})
    with pytest.raises(RegistryError, match=f"撞车.*{ids.STATS}"):
        Registry([(toast_source, toast_entries), (clash_source, clash)])


# ── 合并后的表与快照 ──────────────────────────────────────────────────


def test_snapshot_plus_extension_resolves_every_id_used_in_code(tmp_path):
    reg = load_registry(tmp_path / "no-toast-here" / "evidence_registry.yaml")  # 找不到 Toast → 用快照
    assert len(reg) == 33
    snap = reg.sources[0]
    assert snap.origin == "toast_snapshot" and snap.snapshot_taken_on == "2026-11-14" and "5214e99" in snap.snapshot_of
    assert reg.sources[1].origin == "butter_extension" and reg.sources[1].entries == 13
    assert not reg.missing(ids.ALL_IDS)
    literal: set[str] = set()
    for path in PKG.rglob("*.py"):
        literal |= set(re.findall(r"EVIDENCE_[A-Z0-9_]+_V\d+", path.read_text(encoding="utf-8")))
    assert literal and not reg.missing(literal), reg.missing(literal)
    assert reg.source_of(ids.STATS).origin == "toast_snapshot"
    assert reg.source_of(ids.TITLE_QUERY_MATCH).origin == "butter_extension"
    assert not reg.suggestable(ids.HIDDEN_TEXT_INJECTION) and reg.suggestable(ids.NUMERIC_TRACEABILITY)
    with pytest.raises(KeyError, match="没有条目"):
        reg.get("EVIDENCE_NOPE_V1")


def test_snapshot_matches_its_record_and_hash_ignores_line_endings():
    meta = yaml.safe_load(SNAPSHOT_META_PATH.read_text(encoding="utf-8"))
    raw = SNAPSHOT_PATH.read_bytes()
    assert content_sha256(raw) == meta["sha256"] and meta["entries"] == 20
    assert content_sha256(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")) == meta["sha256"]


def test_a_tampered_snapshot_is_refused(tmp_path):
    bad = tmp_path / "evidence_registry.yaml"
    bad.write_bytes(SNAPSHOT_PATH.read_bytes().replace(b"evidence_level: C", b"evidence_level: B", 1))
    with pytest.raises(RegistryError, match="sha256"):
        read_registry_file(bad, origin="toast_snapshot")


@needs_toast
def test_default_load_uses_local_toast_and_the_snapshot_is_current():
    reg = load_registry()
    toast = reg.sources[0]
    assert toast.origin == "toast" and toast.entries == 20 and len(reg) == 33
    meta = yaml.safe_load(SNAPSHOT_META_PATH.read_text(encoding="utf-8"))
    assert toast.sha256 == meta["sha256"], "Toast 的注册表变了:按 toast_snapshot/SNAPSHOT.yaml 顶部的说明更新快照"


# ── 用 Toast 自己的代码对照 ───────────────────────────────────────────

TOAST_LOAD = ("import sys; sys.path.insert(0, '.'); from pathlib import Path; "
              "from diagnosis.recommender import load_evidence_registry; "
              "print(len(load_evidence_registry(Path(sys.argv[1]))))")
TOAST_SHAPE = """
import sys, json
sys.path.insert(0, '.')
from diagnosis.recommender import EvidenceEntry, _LEVELS, _OUTPUT_LEVELS, _REQUIRED_LANGS, DATA_CONDITIONS
from diagnosis.access_audit import CHECK_NAMES
from diagnosis.findings import Stage
from engine.models import PromptIntent
fields = {n: ("REQUIRED" if f.is_required() else f.default) for n, f in EvidenceEntry.model_fields.items()}
print(json.dumps({"fields": fields, "cfg": {k: EvidenceEntry.model_config.get(k) for k in ("frozen", "extra")},
                  "stages": [s.value for s in Stage], "intents": [i.value for i in PromptIntent],
                  "checks": sorted(CHECK_NAMES), "data": sorted(DATA_CONDITIONS), "levels": list(_LEVELS),
                  "out": list(_OUTPUT_LEVELS), "langs": sorted(_REQUIRED_LANGS)}, ensure_ascii=False))
"""


def run_toast(code: str, *args: str) -> str:
    """用 Toast 的解释器跑一段只读代码;-B 与 PYTHONDONTWRITEBYTECODE 保证不在 Toast 的目录里写字节码。"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([str(TOAST_PY), "-B", "-c", code, *args], cwd=TOAST_DIR, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@needs_toast_python
def test_toast_own_loader_accepts_the_extension_alone_and_merged(tmp_path):
    assert run_toast(TOAST_LOAD, str(EXTENSION_PATH)).strip() == "13"
    merged = {"entries": yaml.safe_load(TOAST_REGISTRY.read_text(encoding="utf-8"))["entries"] + EXT["entries"]}
    path = tmp_path / "merged.yaml"
    path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert run_toast(TOAST_LOAD, str(path)).strip() == "33"


@needs_toast_python
def test_butter_entry_model_matches_toast_field_by_field():
    toast = json.loads(run_toast(TOAST_SHAPE))
    ours = {n: ("REQUIRED" if f.is_required() else f.default) for n, f in EvidenceEntry.model_fields.items()}
    assert toast["fields"] == ours
    assert toast["cfg"] == {k: EvidenceEntry.model_config.get(k) for k in ("frozen", "extra")}
    assert toast["stages"] == list(get_args(Stage)) and toast["intents"] == list(get_args(PromptIntent))
    assert set(toast["checks"]) == ACCESS_CHECK_NAMES and set(toast["data"]) == DATA_CONDITIONS
    assert tuple(toast["levels"]) == EVIDENCE_LEVELS and tuple(toast["out"]) == OUTPUT_LEVELS
    assert set(toast["langs"]) == REQUIRED_LANGS
