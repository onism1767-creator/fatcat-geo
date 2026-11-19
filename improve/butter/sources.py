"""只读:兄弟模块的文件(Potato 的 brands.yml / questions.yml、Toast 导出的 brand-domains.json)和 Butter 自带的数据表。

不 import aivis,不 import Toast 的任何模块:只把它们的文件当数据读,形状不对就如实报错,不猜。
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import Counter
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from butter.contracts import (
    ExcludedVendor, Platform, PotatoBrand, PotatoBrands, Question, QuestionBoard, TemplateSet, ToastBrandDomains,
    ToastCitedDomain, ToastExport, ToastLight, ToastStageLine, UaEntry,
)


class SourceFileError(ValueError):
    """给的文件读不了或形状不对。"""


def read_yaml(path: Path) -> Any:
    """读一个 YAML 文件(只读);读不了抛 SourceFileError。"""
    return _read_yaml(path)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourceFileError(f"读不了 {path}:{exc}") from exc


def _str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def load_brands_yml(path: str | Path) -> PotatoBrands:
    path = Path(path)
    data = _read_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get("peers"), list):
        raise SourceFileError(f"{path} 不像 Potato 的 brands.yml(缺 peers 列表)")
    brands = [
        PotatoBrand(name=str(p["name"]).strip(), aliases=_str_list(p.get("aliases")),
                    domains=_str_list(p.get("domains")), is_focal=bool(p.get("is_focal")))
        for p in data["peers"] if isinstance(p, dict) and p.get("name")
    ]
    focal = data.get("focal_domain")
    return PotatoBrands(path=str(path), focal_domain=str(focal).strip() if focal else None, brands=brands)


def load_questions_yml(path: str | Path) -> QuestionBoard:
    path = Path(path)
    data = _read_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise SourceFileError(f"{path} 不像 Potato 的 questions.yml(缺 questions 列表)")
    questions = [
        Question(id=str(q["id"]), intent=str(q.get("intent", "")), text=str(q["text"]).strip())
        for q in data["questions"] if isinstance(q, dict) and q.get("id") and q.get("text")
    ]
    version = data.get("prompt_version")
    locale = data.get("locale")
    return QuestionBoard(path=str(path), prompt_version=str(version) if version else None,
                         locale=str(locale) if locale else None, questions=questions)


def load_brand_domains_json(path: str | Path) -> ToastBrandDomains:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFileError(f"读不了 {path}:{exc}") from exc
    return parse_brand_domains(data, str(path))


def parse_brand_domains(data: Any, path: str) -> ToastBrandDomains:
    if not isinstance(data, dict) or "brand" not in data:
        raise SourceFileError(f"{path} 不像 Toast 导出的 brand-domains.json(缺 brand)")
    brand = data.get("brand") if isinstance(data.get("brand"), dict) else {}
    listing = data.get("list") if isinstance(data.get("list"), dict) else {}
    domains = [d for d in listing.get("domains", []) if isinstance(d, dict) and d.get("domain")]
    channels = [str(c["match"]) for c in listing.get("official_channels", []) if isinstance(c, dict) and c.get("match")]
    name = brand.get("name")
    counted = _str_list(brand.get("domains"))  # Toast 的 BrandSpec.domains = 计入的官网,主站在前
    primary = listing.get("primary") or next(iter(counted), None)
    # 主站不在 list.domains 里,list 为空时更是一个都没有:把 brand.domains 里主站之外的也算进确认域名(去重)
    confirmed = [str(d["domain"]) for d in domains if d.get("status") == "confirmed"]
    for extra in counted:
        if extra != primary and extra not in confirmed:
            confirmed.append(extra)
    return ToastBrandDomains(
        path=path, brand_name=str(name).strip() if name else None, brand_aliases=_str_list(brand.get("aliases")),
        primary=str(primary) if primary else None,
        confirmed_domains=confirmed,
        unconfirmed_domains=[str(d["domain"]) for d in domains if d.get("status") == "unconfirmed"],
        official_channels=channels or _str_list(brand.get("official_channels")),
    )


# ── Toast 的报告导出包(只读) ──────────────────────────────────────────

TOAST_FILES = ("prompts.csv", "responses.jsonl", "citations.csv", "brand-domains.json", "summary.md")
_LIGHTS = {"🟢": "green", "🟡": "yellow", "🔴": "red", "⚪": "white"}
_MAX_CITED = 20


def _toast_members(path: Path) -> dict[str, bytes]:
    """导出包里那五个文件的内容。给 zip 就从里面读(不解压到盘上),给文件夹就读文件夹。只认这五个名字。"""
    if path.is_dir():
        return {name: (path / name).read_bytes() for name in TOAST_FILES if (path / name).is_file()}
    try:
        with zipfile.ZipFile(path) as archive:
            names = {n.rsplit("/", 1)[-1]: n for n in archive.namelist() if not n.endswith("/")}
            return {name: archive.read(names[name]) for name in TOAST_FILES if name in names}
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceFileError(f"读不了 {path}:{exc}") from exc


def parse_toast_summary(text: str) -> list[ToastStageLine]:
    """从 summary.md 里把带灯的行抄出来。Toast 的写法:「- 哪家 AI:某一步 🟢 良好; 另一步 🔴 较弱」。只抄,不改判。"""
    out: list[ToastStageLine] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        if not line.startswith("- ") or not any(mark in line for mark in _LIGHTS):
            continue
        body = line[2:]
        first_light = min(body.index(mark) for mark in _LIGHTS if mark in body)
        colon = max(body.rfind(":", 0, first_light), body.rfind("：", 0, first_light))  # 半角、全角冒号都认
        surface = body[:colon].strip() if colon != -1 and raw.startswith("- ") else ""
        rest = body[colon + 1:] if colon != -1 else body
        lights = []
        for part in rest.split(";"):
            part = part.strip()
            hits = [(part.index(mark), mark) for mark in _LIGHTS if mark in part]
            if hits:
                at, mark = min(hits)
                lights.append(ToastLight(label=part[:at].strip(), light=_LIGHTS[mark], text=part[at:].strip()))
        if lights:
            out.append(ToastStageLine(section=section, surface=surface, lights=lights))
    return out


def _csv_cell(value: str) -> str:
    """Toast 的 CSV 里,以 = + - @ 开头的单元格前面补了一个 ',读的时候去掉。"""
    return value[1:] if len(value) > 1 and value[0] == "'" and value[1] in "=+-@\t\r" else value


def load_toast_export(path: str | Path) -> ToastExport:
    """读 Toast 的报告导出包(zip 或解开的文件夹)。少了哪个文件就照实记下,不猜。"""
    path = Path(path)
    if not path.exists():
        raise SourceFileError(f"读不了 {path}:文件不存在")
    members = _toast_members(path)
    if not members:
        raise SourceFileError(f"{path} 不像 Toast 的报告导出包:里面一个认得的文件都没有({', '.join(TOAST_FILES)})")
    notes = [f"导出包里没有 {name}" for name in ("brand-domains.json", "summary.md", "citations.csv") if name not in members]
    brand_domains = None
    if "brand-domains.json" in members:
        try:
            brand_domains = parse_brand_domains(json.loads(members["brand-domains.json"].decode("utf-8-sig")),
                                                f"{path}!brand-domains.json")
        except (ValueError, SourceFileError) as exc:
            notes.append(f"brand-domains.json 读不了:{exc}")
    stage_lines = parse_toast_summary(members["summary.md"].decode("utf-8-sig", errors="replace")) \
        if "summary.md" in members else []
    cited: Counter[str] = Counter()
    if "citations.csv" in members:
        reader = csv.DictReader(io.StringIO(members["citations.csv"].decode("utf-8-sig", errors="replace")))
        for row in reader:
            if _csv_cell(row.get("source_class") or "") == "OTHER":
                domain = _csv_cell(row.get("domain") or "").strip().lower()
                if domain:
                    cited[domain] += 1
    return ToastExport(
        path=str(path), files_found=sorted(members), brand_domains=brand_domains, stage_lines=stage_lines,
        cited_other=[ToastCitedDomain(domain=d, citations=n) for d, n in cited.most_common(_MAX_CITED)], notes=notes)


# ── Butter 自带的数据表 ───────────────────────────────────────────────


def _data(name: str) -> Any:
    return yaml.safe_load(resources.files("butter").joinpath("data", name).read_text(encoding="utf-8"))


@cache
def load_ua_table() -> tuple[UaEntry, ...]:
    return tuple(UaEntry.model_validate(item) for item in _data("ua_table.yaml")["entries"])


@cache
def load_excluded_vendors() -> tuple[ExcludedVendor, ...]:
    return tuple(ExcludedVendor.model_validate(item) for item in _data("excluded_vendors.yaml")["vendors"])


@cache
def load_platforms() -> tuple[Platform, ...]:
    return tuple(Platform.model_validate(item) for item in _data("platforms.yaml")["platforms"])


@cache
def load_question_templates() -> TemplateSet:
    """Butter 出题用的固定句式(data/question_templates.yaml)。"""
    raw = resources.files("butter.data").joinpath("question_templates.yaml").read_text(encoding="utf-8")
    return TemplateSet.model_validate(yaml.safe_load(raw))
