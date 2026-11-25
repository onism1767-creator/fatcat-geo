"""证据表核对、型号核对的结论怎么算。全用造的文件,不读真项目。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fatcat import evidence, models
from fatcat.modules import Module, ModuleError


def _table(ids: list[str]) -> str:
    return yaml.safe_dump({"entries": [{"id": i, "stage": "x"} for i in ids]}, allow_unicode=True)


def _projects(tmp_path: Path, *, original: list[str], copy: list[str] | None = None, extension: list[str],
              recorded: str | None = None, crlf: bool = False) -> tuple[Module, Module]:
    toast, butter = tmp_path / "toast", tmp_path / "butter"
    (toast / "config").mkdir(parents=True)
    (butter / evidence.COPY_DIR).mkdir(parents=True)
    text = _table(original)
    (toast / evidence.ORIGINAL).write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    copy_text = _table(copy if copy is not None else original)
    (butter / evidence.COPY_DIR / "evidence_registry.yaml").write_bytes(copy_text.encode("utf-8"))
    (butter / evidence.EXTENSION).write_text(_table(extension), encoding="utf-8")
    (butter / evidence.COPY_DIR / "SNAPSHOT.yaml").write_text(yaml.safe_dump({
        "sha256": recorded or evidence.content_sha256(copy_text.encode("utf-8")), "taken_on": "2026-11-23",
        "entries": len(copy if copy is not None else original)}), encoding="utf-8")
    make = lambda name, path: Module(name=name, role="x", path=path, python=None)  # noqa: E731
    return make("toast", toast), make("butter", butter)


def test_copy_that_matches_the_original_is_fine_even_across_line_endings(tmp_path: Path) -> None:
    state = evidence.read(*_projects(tmp_path, original=["A", "B"], extension=["X"], crlf=True))
    assert evidence.problems(state) == []
    text = evidence.render(state, [])
    assert "原件 2 条" in text and "扩展 1 条" in text and "合起来 3 条" in text and "同一份内容" in text


def test_a_changed_original_is_reported_with_what_changed(tmp_path: Path) -> None:
    state = evidence.read(*_projects(tmp_path, original=["A", "B", "C"], copy=["A", "OLD"], extension=["X"]))
    found = evidence.problems(state)
    assert len(found) == 1 and "2026-11-23 抄的" in found[0]
    assert "原件多了 B、C" in found[0] and "原件没有了 OLD" in found[0]
    assert "合计 1 处" in evidence.render(state, found)


def test_edited_copy_and_clashing_ids_are_reported(tmp_path: Path) -> None:
    state = evidence.read(*_projects(tmp_path, original=["A", "B"], extension=["B", "X", "X"], recorded="0" * 64))
    found = evidence.problems(state)
    assert any("记的指纹不一样" in text for text in found)
    assert any("同一个编号:B" in text for text in found) and any("编号重复:X" in text for text in found)


def test_missing_files_are_said_plainly(tmp_path: Path) -> None:
    toast, butter = _projects(tmp_path, original=["A"], extension=[])
    (butter.path / evidence.EXTENSION).unlink()
    with pytest.raises(ModuleError, match="读不了"):
        evidence.read(toast, butter)


def test_model_tables_outside_the_flow_are_listed_but_do_not_fail_the_check() -> None:
    outside = [models.Diff("potato", "extra", "openai", "Potato 目录里的 x 不在清单里"),
               models.Diff("butter", "extra", "xai", "Butter 再测用的 y 不在清单里")]
    assert models.in_flow(outside) == []
    text = models.render_diffs(outside)
    assert "统一流程用到的型号:全部对得上" in text and "Potato(不在统一流程里):和清单有 1 处不一样" in text
    assert "统一流程 0 处;不在统一流程里的 2 处" in text
    stale = models.Diff("catalog", "stale", "openai", "Toast 的被测型号变了,统一清单还没重读")
    assert models.in_flow([*outside, stale]) == [stale]
    assert "统一流程用到的型号:1 处对不上" in models.render_diffs([stale])
    assert "Butter(不在统一流程里):和清单一致" in models.render_diffs([])
    unread = models.render_diffs([], {"potato": "找不到它的解释器"})  # 没读到的不说「一致」
    assert "Potato(不在统一流程里):这次没有核。找不到它的解释器" in unread and "Potato(不在统一流程里):和清单一致" not in unread
