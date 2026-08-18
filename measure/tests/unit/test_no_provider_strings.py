"""验收清单 12.2「禁字符串测试」:engine/ 与 diagnosis/ 源码中
不得出现任何已注册的 provider/surface 名(硬边界 2)。

名单从 config/surfaces.yaml 动态读取:新增 surface 自动纳入检查,无需改本测试。
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("engine", "diagnosis")

# surface 名按 _ 拆词后,这些通用词不作为禁用词(否则 SearchMode 等正当命名全中枪)
GENERIC_TOKENS = {"web", "search", "agent", "api", "mode"}


def forbidden_names() -> set[str]:
    config = yaml.safe_load((ROOT / "config" / "surfaces.yaml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for surface in config["surfaces"]:
        names.add(surface["provider"].lower())
        names.add(surface["name"].lower())
        names.update(token for token in surface["name"].lower().split("_") if token not in GENERIC_TOKENS)
    return names


def test_forbidden_name_list_is_not_empty():
    assert forbidden_names(), "surfaces.yaml 未注册任何 surface,禁字符串测试失去保护对象"


def test_engine_and_diagnosis_contain_no_provider_strings():
    names = forbidden_names()
    offenders = []
    for package in SCANNED_PACKAGES:
        package_dir = ROOT / package
        if not package_dir.exists():
            continue
        for source_file in package_dir.rglob("*.py"):
            text = source_file.read_text(encoding="utf-8").lower()
            offenders.extend(
                (str(source_file.relative_to(ROOT)), name) for name in names if name in text
            )
    assert not offenders, (
        f"engine/ 或 diagnosis/ 源码中出现 provider/surface 名: {offenders}。"
        "这些名字只允许存在于 adapters/ 与 config/(硬边界 2)。"
    )
