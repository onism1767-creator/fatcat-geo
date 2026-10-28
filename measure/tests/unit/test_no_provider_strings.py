"""验收清单 12.2「禁字符串测试」:engine/ 与 diagnosis/ 源码中
不得出现任何已注册的 provider/surface 名(硬边界 2)。

名单从配置动态读取,新增 surface / 爬虫自动纳入,无需改本测试:
- config/surfaces.yaml:provider、name、name 按 _ 拆出的词、aliases(gpt / chatgpt / google / sonar……);
- config/crawlers.yaml:每个爬虫的 name 与 robots_token(GPTBot / Googlebot / ClaudeBot……)。
按词边界匹配(前后不是字母数字):「gpt-4o」「chatgpt」会被抓到,「combining」不会因为含 bing 中枪。
(用户 2026-10-28 定:名单从只看 surfaces.yaml 扩到别名与爬虫名。)
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("engine", "diagnosis")

# surface 名按 _ 拆词后,这些通用词不作为禁用词(否则 SearchMode 等正当命名全中枪)
GENERIC_TOKENS = {"web", "search", "agent", "api", "mode"}


def forbidden_names() -> set[str]:
    surfaces = yaml.safe_load((ROOT / "config" / "surfaces.yaml").read_text(encoding="utf-8"))
    crawlers = yaml.safe_load((ROOT / "config" / "crawlers.yaml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for surface in surfaces["surfaces"]:
        names.add(surface["provider"].lower())
        names.add(surface["name"].lower())
        names.update(token for token in surface["name"].lower().split("_") if token not in GENERIC_TOKENS)
        names.update(alias.lower() for alias in surface.get("aliases", []))
    for crawler in crawlers["crawlers"]:
        names.add(crawler["name"].lower())
        names.add(crawler["robots_token"].lower())
    return names


def offenders_in(text: str, names: set[str]) -> list[str]:
    lowered = text.lower()
    return sorted(name for name in names if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered))


def test_forbidden_name_list_is_not_empty_and_wide():
    names = forbidden_names()
    assert names, "surfaces.yaml 未注册任何 surface,禁字符串测试失去保护对象"
    # 用户定的最低宽度:别名与爬虫名都在(改配置把它们拿掉,这里会报)
    assert {"gpt", "chatgpt", "google", "sonar", "gptbot", "googlebot", "claudebot"} <= names


def test_matching_is_by_word_boundary():
    assert offenders_in("model = \"gpt-4o\"", {"gpt"}) == ["gpt"]
    assert offenders_in("unicodedata.combining(c)", {"bing"}) == []
    assert offenders_in("ChatGPT said", {"chatgpt"}) == ["chatgpt"]


def test_engine_and_diagnosis_contain_no_provider_strings():
    names = forbidden_names()
    offenders = []
    for package in SCANNED_PACKAGES:
        package_dir = ROOT / package
        if not package_dir.exists():
            continue
        for source_file in package_dir.rglob("*.py"):
            found = offenders_in(source_file.read_text(encoding="utf-8"), names)
            offenders.extend((str(source_file.relative_to(ROOT)), name) for name in found)
    assert not offenders, (
        f"engine/ 或 diagnosis/ 源码中出现 provider/surface/爬虫名: {offenders}。"
        "这些名字只允许存在于 adapters/ 与 config/(硬边界 2)。"
    )
