"""展示页导出(外壳:读一次运行的产出、读模板;自己不写盘,把要写的文件交回去)。设计 §12、ADR-B08。

    files, report = build(run_dir, workspace, manual=对照表里人工补的部分)

做的事:
  1. 读一次运行的事实库、审计、改动包,连同它的批次(有的话),用本地服务同一套视图和模板,以静态模式渲染(没有按钮);
  2. 出页面之前,先把视图对象里的真名换成代号(对照表 = 人工补的 + 从事实库自动列的 + 出现过的本站地址);
  3. 每个页面连同它的 .json 孪生、改动包里的文件一起导出,文件名里的真名也换;
  4. 最后全文查一遍:任何一个文件的名字或内容里还搜得到对照表上的真名,就整份不导出(抛 ShowcaseLeak,说明在哪)。

返回的 files = {相对路径: 文本},由调用方交给 store 存进一个新文件夹。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import butter
from butter import cards, sources
from butter.config import load_defaults
from butter.contracts import AuditReport, ChangePackage, FactBase
from butter.registry import load_registry
from butter.showcase import names as N
from butter.showcase.static import relink
from butter.web import views
from butter.web.app import _env as template_env

NO_CHANGE_FILE = Path(__file__).resolve().parent.parent / "data" / "showcase_no_change.json"
PACKAGE_DIR = "example/change-package"
TEXT_SUFFIXES = (".md", ".json", ".html", ".txt", ".csv", ".yaml", ".yml", ".xml")


class ShowcaseLeak(ValueError):
    """导出里还搜得到真名。found = [(文件, 真名), ...]。"""

    def __init__(self, found: list[tuple[str, str]]):
        self.found = found
        where = ";".join(f"{path} 里有「{name}」" for path, name in found[:8])
        more = f" 等 {len(found)} 处" if len(found) > 8 else ""
        super().__init__(f"导出的文件里还搜得到真名,没有导出:{where}{more}。把漏掉的词补进对照表再来一次。")


@dataclass
class ShowcaseReport:
    names: N.Names = field(default_factory=list)       # 实际用的对照表(真名 → 代号),只给做展示页的人看,不导出
    too_short: list[str] = field(default_factory=list)  # 太短没有自动换的词,要人工看一眼
    pages: list[str] = field(default_factory=list)
    package_files: int = 0
    skipped_files: list[str] = field(default_factory=list)
    hints: list[tuple[str, int]] = field(default_factory=list)  # 商品名里没换掉、导出里还出现的词:看看是不是系列名


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in ([k] if isinstance(k, str) else []) + _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def _load_run(run_dir: Path) -> dict[str, Any]:
    try:
        return {
            "dir": run_dir,
            "meta": json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
            "factbase": FactBase.model_validate_json((run_dir / "factbase.json").read_text(encoding="utf-8")),
            "audit": AuditReport.model_validate_json((run_dir / "audit.json").read_text(encoding="utf-8")),
            "package": ChangePackage.model_validate_json(
                (run_dir / "change-package" / "manifest.json").read_text(encoding="utf-8")),
        }
    except (OSError, ValueError) as exc:
        raise ValueError(f"这次运行的文件读不了({run_dir}):{type(exc).__name__}") from exc


def _package_texts(run_dir: Path) -> tuple[dict[str, str], list[str]]:
    """改动包里的文本文件 {相对路径: 内容};不是文本的记下来不导出。"""
    root = run_dir / "change-package"
    out: dict[str, str] = {}
    skipped: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append(rel)
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped.append(rel)
    return out, skipped


def _rename_file(rel: str, text: str, names: N.Names) -> str:
    """改动包里的一个文件。JSON 先读成对象再换:带引号、带转义的名字,在原始文字里是对不上的。"""
    if rel.lower().endswith(".json"):
        try:
            return json.dumps(N.recode_tree(json.loads(text), names), ensure_ascii=False, indent=2) + "\n"
        except ValueError:
            pass
    return N.recode(text, names)


def build(run_dir: Path, workspace: Path, *, manual: N.Names | None = None, today: str | None = None,
          ) -> tuple[dict[str, str], ShowcaseReport]:
    run = _load_run(run_dir)
    fb: FactBase = run["factbase"]
    registry = load_registry()
    defaults = load_defaults()
    cards_view, _ = cards.load(workspace, as_of=date.fromisoformat(today) if today else None)
    state = next((b for b in cards_view.batches if b.batch.run_id == run["meta"]["run_id"]), None)

    # 1. 视图对象(和本地服务用的是同一套)
    run_view = views.run_detail(run, registry, cards_view)
    batch_view = views.batch_detail(state, cards_view, defaults, today=today or "") if state else None
    registry_view = views.registry(registry, cards_view)
    package_files, skipped_files = _package_texts(run_dir)

    # 2. 对照表:人工补的在最前(同一个词以它为准),再是自动列的,最后是出现过的本站地址
    auto, too_short = N.auto_names(fb)
    known = [*(manual or []), *auto]
    texts = _strings(run_view) + _strings(batch_view) + list(package_files) + list(package_files.values())
    names = [*known, *N.url_names(N.site_urls(texts, N.hosts_of(known)), known)]

    # 3. 先换名字,再出页面
    run_view = N.recode_tree(run_view, names)
    batch_view = N.recode_tree(batch_view, names) if batch_view else None
    pages = {"/": "index.html", "/index.json": "index.json", "/registry": "registry.html",
             "/registry.json": "registry.json",
             f"/runs/{run_view['run_id']}": "example/run.html", f"/runs/{run_view['run_id']}.json": "example/run.json"}
    if batch_view:
        pages |= {f"/batches/{batch_view['batch_id']}": "example/batch.html",
                  f"/batches/{batch_view['batch_id']}.json": "example/batch.json"}
    example = {"pages": run_view["pages"], "changes": len(run_view["changes"]),
               "problems": run_view["audit_counts"]["problem"], "notices": run_view["audit_counts"]["notice"],
               "passed": run_view["audit_counts"]["pass"], "run_path": f"/runs/{run_view['run_id']}",
               "batch_path": f"/batches/{batch_view['batch_id']}" if batch_view else ""}
    no_change = json.loads(NO_CHANGE_FILE.read_text(encoding="utf-8"))
    home_view = views.showcase_home(example, no_change, defaults, list(sources.load_excluded_vendors()))

    env = template_env()
    files: dict[str, str] = {}

    def page(template: str, view: dict[str, Any], logical: str) -> None:
        here = pages[logical]
        html = env.get_template(template).render(v=view, token="", version=butter.__version__, path=logical,
                                                 static=True)
        files[here] = relink(html, here, pages)
        files[here.removesuffix(".html") + ".json"] = json.dumps(view, ensure_ascii=False, indent=2) + "\n"

    page("showcase.html", home_view, "/")
    page("run.html", run_view, f"/runs/{run_view['run_id']}")
    if batch_view:
        page("batch.html", batch_view, f"/batches/{batch_view['batch_id']}")
    page("registry.html", registry_view, "/registry")
    for rel, text in package_files.items():
        files[f"{PACKAGE_DIR}/{N.recode(rel, names)}"] = _rename_file(rel, text, names)

    # 4. 全文查:文件名和内容都查。搜到一个就整份不导出
    found = [(path, name) for path, text in files.items() for name in N.leaks(f"{path}\n{text}", names)]
    if found:
        raise ShowcaseLeak(found)
    return files, ShowcaseReport(names=names, too_short=too_short, pages=sorted(p for p in files if p.endswith(".html")
                                                                               and not p.startswith(PACKAGE_DIR)),
                                 package_files=len(package_files), skipped_files=skipped_files,
                                 hints=N.leftover_hints(files.values(), fb, names))
