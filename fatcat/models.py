"""统一型号清单。

唯一的来源是负责「问 AI」的那一套(Toast)适配器里的声明:被测型号、消费端默认、主力、只当裁判的辅助型号。
sync 把它读出来存成 catalog/models.json;check 拿这份清单去对 Potato 和 Butter 各自的清单,列出对不上的地方。

读各项目的办法:用它自己的解释器跑一小段只读的代码,或者直接读它的配置文件。不往任何一个项目里写东西,
也不发任何网络请求(建适配器时给的是占位字符串,不是 key)。
"""

from __future__ import annotations

import ast
import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from fatcat.modules import ROOT, Module, ModuleError, run_readonly

CATALOG_FILE = ROOT / "catalog" / "models.json"
ID_MAP_FILE = ROOT / "catalog" / "id-map.toml"
RULE = "产品文档 v1.20 D41:被测型号每家只留两个(产品界面默认的那款 + 它在最新一代里的同款)"

# 在 Toast 自己的解释器里跑:把每个启用的适配器的声明读出来。给的不是 key,只是让加载器别跳过它。
_TOAST_DUMP = """
import json
from engine.loader import build_adapters, load_surface_registry
entries = [e for e in load_surface_registry() if e.enabled]
adapters = build_adapters({e.api_key_env: "placeholder-not-a-key" for e in entries})
rows = []
for e in entries:
    meta = adapters[e.name].meta()
    rows.append({
        "surface": meta.surface, "provider": meta.provider, "key_env": e.api_key_env,
        "display_name": meta.display_name, "api_name": meta.api_name,
        "models": list(meta.models), "helper_models": list(meta.helper_models),
        "consumer_default": meta.consumer_default_model, "main": meta.main_model,
        "variant_of": meta.variant_of, "models_checked": meta.models_checked,
    })
print("FATCAT_JSON " + json.dumps(rows))
"""

# 在 Potato 自己的解释器里跑:读它的型号目录(纯数据,不碰任何 SDK)。
_POTATO_DUMP = """
import json
from aivis.providers import catalog
print("FATCAT_JSON " + json.dumps({
    "key_env": {name: info.env_key for name, info in catalog.PROVIDERS.items()},
    "models": [{"provider": m.provider, "model": m.model, "status": m.status,
                "pinned": m.pinned, "surface_kind": m.surface_kind} for m in catalog.MODELS],
}))
"""


# ---------------------------------------------------------------- 读三个项目


def read_toast(module: Module) -> list[dict[str, Any]]:
    return list(run_readonly(module, ["-c", _TOAST_DUMP]))


def read_potato(module: Module) -> dict[str, Any]:
    return dict(run_readonly(module, ["-c", _POTATO_DUMP], pythonpath=module.path / "src"))


def read_butter(module: Module) -> dict[str, Any]:
    """Butter 的清单在配置文件里:再测用的型号(defaults.yaml)、起草用的型号(pricing.yaml)、认哪些 key 变量名(keys.py)。"""
    config = module.require_path() / "butter" / "config"
    try:
        defaults = yaml.safe_load((config / "defaults.yaml").read_text(encoding="utf-8"))
        pricing = yaml.safe_load((config / "pricing.yaml").read_text(encoding="utf-8"))
        key_env = _literal_assignment(module.path / "butter" / "llm" / "keys.py", "ENV_NAMES")
    except (OSError, yaml.YAMLError, SyntaxError, ValueError) as exc:
        raise ModuleError(f"读 Butter 的型号清单失败:{exc}") from exc
    rows = defaults["defaults"] if isinstance(defaults, dict) else defaults
    prefix = "measure.model."
    return {
        "measure": {row["key"][len(prefix):]: row["value"] for row in rows if row["key"].startswith(prefix)},
        "drafting": dict(pricing.get("default_models", {})),
        "key_env": {provider: list(names) for provider, names in key_env.items()},
    }


def _literal_assignment(path: Path, name: str) -> Any:
    """读一个 .py 文件里顶层的 `name = <字面量>`,不导入、不执行它。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target == name and value is not None:
            return ast.literal_eval(value)
    raise ValueError(f"{path.name} 里没有找到 {name}")


# ---------------------------------------------------------------- 统一清单


def build_catalog(rows: list[dict[str, Any]], commit: str | None, read_at: str | None = None) -> dict[str, Any]:
    """把 Toast 各适配器的声明收成按服务商排的一份清单。

    一家服务商可以有不止一台仪器(例:多开一个开关的版本,variant_of 指向原来那台):它不带来新的被测型号,
    只记在 surfaces 里。
    """
    providers: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (r["variant_of"] is not None, r["provider"], r["surface"])):
        entry = providers.setdefault(row["provider"], {
            "id": row["provider"], "key_env": row["key_env"], "measured": [], "helpers": [],
            "surfaces": [], "models_checked": "",
        })
        entry["surfaces"].append({
            "surface": row["surface"], "api_name": row["api_name"], "display_name": row["display_name"],
            "variant_of": row["variant_of"],
        })
        if row["variant_of"] is None:
            entry["models_checked"] = row["models_checked"]
        known = {m["model"] for m in entry["measured"]}
        for model in row["models"]:
            if model in known:
                continue
            roles = [role for role, marked in (("consumer_default", row["consumer_default"]), ("main", row["main"]))
                     if marked == model]
            entry["measured"].append({"model": model, "roles": roles})
        for helper in row["helper_models"]:
            if helper not in entry["helpers"]:
                entry["helpers"].append(helper)
    return {
        "schema": 1,
        "source": {
            "module": "toast", "commit": commit, "rule": RULE,
            "read_at": read_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "providers": [providers[name] for name in sorted(providers)],
    }


def save_catalog(catalog: dict[str, Any], path: Path = CATALOG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_catalog(path: Path = CATALOG_FILE) -> dict[str, Any]:
    if not path.is_file():
        raise ModuleError(f"还没有统一清单({path.name}):先跑 `python -m fatcat models sync`")
    return dict(json.loads(path.read_text(encoding="utf-8")))


def load_id_map(path: Path = ID_MAP_FILE) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {"providers": data.get("providers", {}), "models": data.get("models", {})}


def measured_keys(catalog: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p["id"], m["model"]) for p in catalog["providers"] for m in p["measured"]]


# ---------------------------------------------------------------- 核对


@dataclass(frozen=True)
class Diff:
    module: str   # 哪个项目对不上
    kind: str     # stale / missing / extra / unpinned / key_env / no_provider
    provider: str  # 统一清单里的服务商名
    text: str     # 一句话说清楚


def catalog_drift(stored: dict[str, Any], fresh: dict[str, Any]) -> list[Diff]:
    """存下来的清单和 Toast 现在的声明比:Toast 换了型号而清单没跟着重读,就是过期。"""
    old = {p["id"]: p for p in stored["providers"]}
    new = {p["id"]: p for p in fresh["providers"]}
    diffs: list[Diff] = []
    for provider in sorted(old.keys() | new.keys()):
        before, after = old.get(provider), new.get(provider)
        if before is None or after is None:
            what = "多了这一家" if before is None else "少了这一家"
            diffs.append(Diff("catalog", "stale", provider, f"Toast 现在{what},统一清单还没重读"))
            continue
        for field, label in (("measured", "被测型号"), ("helpers", "辅助型号"), ("key_env", "key 的变量名"),
                             ("surfaces", "仪器")):
            if before[field] != after[field]:
                diffs.append(Diff("catalog", "stale", provider, f"Toast 的{label}变了,统一清单还没重读"))
    return diffs


def compare(catalog: dict[str, Any], potato: dict[str, Any], butter: dict[str, Any],
            id_map: dict[str, Any]) -> list[Diff]:
    """Potato:用户能选的型号应当正好是统一清单(少的、多的都列出来)。
    Butter:再测时每家只测一个型号,这个型号应当在统一清单里;清单里有而它测不了的服务商也列出来。"""
    return _compare_potato(catalog, potato, id_map) + _compare_butter(catalog, butter, id_map)


def _local_provider(id_map: dict[str, Any], module: str, provider: str) -> str:
    return str(id_map["providers"].get(provider, {}).get(module, provider))


def _local_model(id_map: dict[str, Any], module: str, provider: str, model: str) -> tuple[str, str | None]:
    """统一清单的型号在这个项目里怎么写;第二项是登记在对照表里的说明(写法不同才有)。"""
    entry = id_map["models"].get(f"{provider}:{model}", {})
    if module in entry:
        return str(entry[module]), entry.get("note")
    return model, None


def _compare_potato(catalog: dict[str, Any], potato: dict[str, Any], id_map: dict[str, Any]) -> list[Diff]:
    diffs: list[Diff] = []
    offered = {(m["provider"], m["model"]): m for m in potato["models"] if m["provider"] != "mock"}
    matched: set[tuple[str, str]] = set()
    for entry in catalog["providers"]:
        provider = entry["id"]
        local = _local_provider(id_map, "potato", provider)
        if local not in potato["key_env"]:
            diffs.append(Diff("potato", "no_provider", provider, "Potato 没有接这一家"))
            continue
        if potato["key_env"][local] != entry["key_env"]:
            diffs.append(Diff("potato", "key_env", provider,
                              f"key 的变量名不一样:统一清单用 {entry['key_env']},Potato 用 {potato['key_env'][local]}"))
        for measured in entry["measured"]:
            model, note = _local_model(id_map, "potato", provider, measured["model"])
            if (local, model) not in offered:
                diffs.append(Diff("potato", "missing", provider, f"清单里的 {measured['model']} 在 Potato 的目录里没有"))
                continue
            matched.add((local, model))
            if note:
                diffs.append(Diff("potato", "unpinned", provider, f"{measured['model']} 在 Potato 里写作 {model}:{note}"))
    by_local = {_local_provider(id_map, "potato", p["id"]): p["id"] for p in catalog["providers"]}
    for (local, model), info in sorted(offered.items()):
        if (local, model) in matched:
            continue
        tags = [t for t, on in (("已下架", info["status"] == "legacy"), ("预览版", info["status"] == "preview"),
                                ("只搜索不回答", info["surface_kind"] != "answer")) if on]
        suffix = f"({'、'.join(tags)})" if tags else ""
        diffs.append(Diff("potato", "extra", by_local.get(local, local),
                          f"Potato 目录里的 {model}{suffix} 不在清单里"))
    return diffs


def _compare_butter(catalog: dict[str, Any], butter: dict[str, Any], id_map: dict[str, Any]) -> list[Diff]:
    diffs: list[Diff] = []
    for entry in catalog["providers"]:
        provider = entry["id"]
        local = _local_provider(id_map, "butter", provider)
        if local not in butter["measure"]:
            diffs.append(Diff("butter", "no_provider", provider, "Butter 的改前改后测量不测这一家"))
            continue
        names = butter["key_env"].get(local, [])
        if entry["key_env"] not in names:
            diffs.append(Diff("butter", "key_env", provider,
                              f"key 的变量名不一样:统一清单用 {entry['key_env']},Butter 认 {' / '.join(names) or '无'}"))
        allowed = {_local_model(id_map, "butter", provider, m["model"])[0] for m in entry["measured"]}
        current = butter["measure"][local]
        if current not in allowed:
            diffs.append(Diff("butter", "extra", provider,
                              f"Butter 再测用的 {current} 不在清单里(清单里这一家是 {'、'.join(sorted(allowed))})"))
    return diffs


# ---------------------------------------------------------------- 给人看

_ROLE_WORDS = {"consumer_default": "产品界面默认", "main": "主力"}
_KIND_WORDS = {
    "stale": "清单过期", "missing": "少了", "extra": "多了", "unpinned": "写法不同",
    "key_env": "key 变量名", "no_provider": "没有这一家",
}
_MODULE_WORDS = {"catalog": "统一清单", "potato": "Potato", "butter": "Butter"}


def render_catalog(catalog: dict[str, Any]) -> str:
    source = catalog["source"]
    total = len(measured_keys(catalog))
    lines = [
        f"统一型号清单:{len(catalog['providers'])} 家服务商,{total} 个被测型号",
        f"来源:Toast 的适配器声明(提交 {source['commit'] or '未知'},读取于 {source['read_at']})",
        f"规矩:{source['rule']}", "",
    ]
    for entry in catalog["providers"]:
        lines.append(f"{entry['id']}  (key:{entry['key_env']})")
        for measured in entry["measured"]:
            roles = "、".join(_ROLE_WORDS[r] for r in measured["roles"]) or "清单里相邻的一款"
            lines.append(f"  被测  {measured['model']}  [{roles}]")
        for helper in entry["helpers"]:
            lines.append(f"  辅助  {helper}  [只当裁判,不算被测]")
        for surface in entry["surfaces"]:
            if surface["variant_of"]:
                lines.append(f"  可选  {surface['display_name']}  [{surface['variant_of']} 多开一个开关的版本,分开记]")
    return "\n".join(lines)


# 不在统一流程里的两处(2026-11-24 用户定 Z1:问 AI 只留测量工具那一套)。它们的型号表照样核、照样列,
# 但不算统一流程没对上:统一流程不经过它们。哪天它们的提问彻底退出,这两张表也就没有了。
OUTSIDE_FLOW = {
    "potato": "Potato 的提问已经退出统一流程;它的仓库在 2026-12-04 E0 结束前不动",
    "butter": "这是改进工具自己页面上「测一次 / 测改之后」用的型号(经 Potato 提问);统一流程里的再测走测量工具,不用这张表",
}


def in_flow(diffs: list[Diff]) -> list[Diff]:
    """统一流程本身没对上的那些(现在只有一种:统一清单和测量工具的声明不一致)。"""
    return [d for d in diffs if d.module not in OUTSIDE_FLOW]


def render_diffs(diffs: list[Diff], not_read: dict[str, str] | None = None) -> str:
    """not_read:这次没读到的表(名字 → 原因)。没读到的不说「一致」,照实说没核。"""
    not_read = not_read or {}
    flow = in_flow(diffs)
    lines = ["统一流程用到的型号:" + (f"{len(flow)} 处对不上" if flow else "全部对得上(统一清单 = 测量工具现在的声明)。")]
    for diff in flow:
        lines.append(f"  [{_KIND_WORDS[diff.kind]}] {diff.provider}:{diff.text}")
    for module in OUTSIDE_FLOW:
        mine = [d for d in diffs if d.module == module]
        if module in not_read:
            lines += ["", f"{_MODULE_WORDS[module]}(不在统一流程里):这次没有核。{not_read[module]}"]
            continue
        if not mine:
            lines += ["", f"{_MODULE_WORDS[module]}(不在统一流程里):和清单一致。"]
            continue
        lines += ["", f"{_MODULE_WORDS[module]}(不在统一流程里):和清单有 {len(mine)} 处不一样", f"  说明:{OUTSIDE_FLOW[module]}"]
        for diff in mine:
            lines.append(f"  [{_KIND_WORDS[diff.kind]}] {diff.provider}:{diff.text}")
    outside = len(diffs) - len(flow)
    lines += ["", f"合计:统一流程 {len(flow)} 处;不在统一流程里的 {outside} 处(只列出来,不算统一流程没对上;加 --strict 时也算)。"]
    return "\n".join(lines)
