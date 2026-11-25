"""统一型号清单的纯逻辑:怎么从适配器声明收成清单,怎么核对。不读真实项目,不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fatcat import models


def _row(surface: str, provider: str, listed: list[str], *, consumer: str, main: str, key: str,
         helpers: tuple[str, ...] = (), variant_of: str | None = None) -> dict:
    return {
        "surface": surface, "provider": provider, "key_env": key, "display_name": surface.title(),
        "api_name": f"{provider} api", "models": listed, "helper_models": list(helpers),
        "consumer_default": consumer, "main": main, "variant_of": variant_of, "models_checked": "checked",
    }


ROWS = [
    _row("alpha_search", "alpha", ["a-old", "a-new"], consumer="a-old", main="a-new", key="ALPHA_KEY",
         helpers=("a-mini",)),
    _row("beta_search", "beta", ["b-one", "b-next"], consumer="b-one", main="b-one", key="BETA_KEY"),
    _row("beta_extra_search", "beta", ["b-one", "b-next"], consumer="b-one", main="b-one", key="BETA_KEY",
         variant_of="beta_search"),
    _row("gamma_agent", "gamma", ["low@1"], consumer="low@1", main="low@1", key="GAMMA_KEY"),
]
ID_MAP = {
    "providers": {"beta": {"potato": "b", "butter": "b"}},
    "models": {"gamma:low@1": {"potato": "preset:low", "note": "没有钉版本号"}},
}


def _catalog() -> dict:
    return models.build_catalog(ROWS, commit="abc1234", read_at="2026-11-24T00:00:00Z")


def _potato(extra: list[dict] | None = None, drop: tuple[str, str] | None = None) -> dict:
    listed = [("alpha", "a-old"), ("alpha", "a-new"), ("b", "b-one"), ("b", "b-next"), ("gamma", "preset:low")]
    rows = [{"provider": p, "model": m, "status": "current", "pinned": True, "surface_kind": "answer"}
            for p, m in listed if (p, m) != drop]
    rows.append({"provider": "mock", "model": "mock-model", "status": "current", "pinned": True,
                 "surface_kind": "answer"})
    return {"key_env": {"mock": "", "alpha": "ALPHA_KEY", "b": "BETA_KEY", "gamma": "GAMMA_KEY"},
            "models": rows + (extra or [])}


def _butter(**measure: str) -> dict:
    return {
        "measure": {"alpha": "a-old", "b": "b-one", "gamma": "low@1", **measure},
        "drafting": {"alpha": "a-writer"},
        "key_env": {"alpha": ["ALPHA_KEY"], "b": ["BETA_API_KEY", "BETA_KEY"], "gamma": ["GAMMA_KEY"]},
    }


def _kinds(diffs: list[models.Diff], module: str) -> list[tuple[str, str]]:
    return [(d.kind, d.provider) for d in diffs if d.module == module]


def test_catalog_marks_roles_and_keeps_helpers_apart() -> None:
    catalog = _catalog()
    alpha = next(p for p in catalog["providers"] if p["id"] == "alpha")
    assert alpha["measured"] == [
        {"model": "a-old", "roles": ["consumer_default"]},
        {"model": "a-new", "roles": ["main"]},
    ]
    assert alpha["helpers"] == ["a-mini"]  # 辅助型号不算被测
    assert alpha["key_env"] == "ALPHA_KEY"


def test_one_model_can_hold_both_marks_and_the_neighbour_has_none() -> None:
    beta = next(p for p in _catalog()["providers"] if p["id"] == "beta")
    assert beta["measured"] == [
        {"model": "b-one", "roles": ["consumer_default", "main"]},
        {"model": "b-next", "roles": []},
    ]


def test_variant_instrument_adds_no_measured_model() -> None:
    catalog = _catalog()
    beta = next(p for p in catalog["providers"] if p["id"] == "beta")
    assert [s["surface"] for s in beta["surfaces"]] == ["beta_search", "beta_extra_search"]
    assert beta["surfaces"][1]["variant_of"] == "beta_search"
    assert len(models.measured_keys(catalog)) == 5


def test_everything_lines_up_except_the_registered_spelling_note() -> None:
    diffs = models.compare(_catalog(), _potato(), _butter(), ID_MAP)
    assert _kinds(diffs, "butter") == []
    assert _kinds(diffs, "potato") == [("unpinned", "gamma")]  # 写法不同是登记过的,照样提醒


def test_potato_missing_and_extra_models_are_both_listed() -> None:
    extra = [{"provider": "alpha", "model": "a-ancient", "status": "legacy", "pinned": True,
              "surface_kind": "answer"}]
    diffs = models.compare(_catalog(), _potato(extra=extra, drop=("b", "b-next")), _butter(), ID_MAP)
    assert ("missing", "beta") in _kinds(diffs, "potato")
    extra_texts = [d.text for d in diffs if d.kind == "extra"]
    assert extra_texts == ["Potato 目录里的 a-ancient(已下架) 不在清单里"]


def test_mock_engine_is_never_counted_as_extra() -> None:
    diffs = models.compare(_catalog(), _potato(), _butter(), ID_MAP)
    assert not [d for d in diffs if "mock" in d.text]


def test_potato_key_name_difference_and_missing_provider() -> None:
    potato = _potato()
    potato["key_env"]["alpha"] = "ALPHA_API_KEY"
    del potato["key_env"]["gamma"]
    kinds = _kinds(models.compare(_catalog(), potato, _butter(), ID_MAP), "potato")
    assert ("key_env", "alpha") in kinds
    assert ("no_provider", "gamma") in kinds


def test_butter_model_outside_the_catalog_is_flagged() -> None:
    diffs = models.compare(_catalog(), _potato(), _butter(alpha="a-other"), ID_MAP)
    flagged = [d for d in diffs if d.module == "butter"]
    assert [(d.kind, d.provider) for d in flagged] == [("extra", "alpha")]
    assert "a-other" in flagged[0].text and "a-new" in flagged[0].text


def test_butter_provider_it_cannot_measure_and_key_name_it_does_not_accept() -> None:
    butter = _butter()
    del butter["measure"]["gamma"]
    butter["key_env"]["alpha"] = ["OTHER_KEY"]
    kinds = _kinds(models.compare(_catalog(), _potato(), butter, ID_MAP), "butter")
    assert kinds == [("key_env", "alpha"), ("no_provider", "gamma")]


def test_stored_catalog_goes_stale_when_the_source_changes() -> None:
    stored = _catalog()
    changed = [dict(row) for row in ROWS]
    changed[0]["models"] = ["a-old", "a-newer"]
    changed[0]["main"] = "a-newer"
    fresh = models.build_catalog(changed, commit="def5678", read_at="2026-11-25T00:00:00Z")
    drift = models.catalog_drift(stored, fresh)
    assert [(d.kind, d.provider) for d in drift] == [("stale", "alpha")]
    assert models.catalog_drift(stored, _catalog()) == []  # 只是读取时间不同,不算过期


def test_catalog_round_trips_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "catalog" / "models.json"
    models.save_catalog(_catalog(), path)
    assert models.load_catalog(path) == _catalog()


def test_missing_catalog_says_what_to_run(tmp_path: Path) -> None:
    with pytest.raises(models.ModuleError, match="models sync"):
        models.load_catalog(tmp_path / "nope.json")


def test_literal_assignment_reads_without_importing(tmp_path: Path) -> None:
    source = tmp_path / "keys.py"
    source.write_text('import nothing_that_exists\nENV_NAMES: dict = {"alpha": ("A_KEY", "B_KEY")}\n',
                      encoding="utf-8")
    assert models._literal_assignment(source, "ENV_NAMES") == {"alpha": ("A_KEY", "B_KEY")}
    with pytest.raises(ValueError):
        models._literal_assignment(source, "MISSING")
