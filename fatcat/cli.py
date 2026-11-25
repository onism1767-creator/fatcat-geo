"""命令行入口:`python -m fatcat <命令>`。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fatcat import models
from fatcat.modules import ModuleError, load_modules
from fatcat.report import Sources, build, render, stamp_now
from fatcat.sources import butter, toast, weblite


def _models_sync() -> int:
    module = load_modules()["toast"]
    catalog = models.build_catalog(models.read_toast(module), module.commit())
    models.save_catalog(catalog)
    print(models.render_catalog(catalog))
    print(f"\n已写入 {models.CATALOG_FILE}")
    return 0


def _models_show() -> int:
    print(models.render_catalog(models.load_catalog()))
    return 0


def _models_check(strict: bool = False) -> int:
    modules = load_modules()
    stored = models.load_catalog()
    fresh = models.build_catalog(models.read_toast(modules["toast"]), modules["toast"].commit())
    diffs = models.catalog_drift(stored, fresh)
    diffs += models.compare(stored, models.read_potato(modules["potato"]), models.read_butter(modules["butter"]),
                            models.load_id_map())
    print(models.render_diffs(diffs))
    return 1 if (diffs if strict else models.in_flow(diffs)) else 0


def _evidence_check() -> int:
    from fatcat import evidence

    modules = load_modules()
    state = evidence.read(modules["toast"], modules["butter"])
    found = evidence.problems(state)
    print(evidence.render(state, found))
    return 1 if found else 0


def _sources() -> int:
    """现在手头有哪些结果能拿来出报告。"""
    modules = load_modules()
    print("Toast 库里的任务:")
    try:
        for job in toast.list_jobs(modules["toast"]):
            print(f"  {job['job_id']}  {job['state']}  {job['brand']}  {job['website']}  "
                  f"报告 {job['report_versions']} 版")
    except ModuleError as exc:
        print(f"  读不到:{exc}")
    print("Butter 的运行(新的在前,最多 12 个):")
    for run in butter.find_runs(modules["butter"].path / "out")[:12]:
        print(f"  {run['host']}  {run['run_id']}  改动 {run['change_items']} 条  {run['dir']}")
    return 0


def _report(args: argparse.Namespace) -> int:
    modules = load_modules()
    sources = Sources()
    if args.toast_job:
        sources.toast = toast.load_view(modules["toast"], args.toast_job)
    if args.butter_run:
        sources.butter = butter.load_run(Path(args.butter_run))
        workspace = Path(args.butter_workspace) if args.butter_workspace else modules["butter"].path / "workspace"
        sources.batches = butter.batches_for(workspace, sources.butter["run"].get("run_id") or "")
    if args.weblite:
        sources.weblite = weblite.load_bundle(Path(args.weblite))
    report = build(sources, args.lang, generated_at=stamp_now(),
                   allow_mixed=args.allow_mixed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    print(f"报告已写入 {out.resolve()}")
    for chapter in report.chapters:
        print(f"  {chapter.no:>2}. {chapter.title}  ({len(chapter.blocks)} 块)")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from fatcat.hub.app import create_app
    from fatcat.hub.backend import Backend
    from fatcat.modules import ROOT

    from fatcat.keys import KeyStore
    from fatcat.models import load_catalog

    runs = Path(args.runs) if args.runs else ROOT / "runs"
    # 各家 key 的变量名以统一型号清单为准;测量工具的进程号和启动日志记在 runs/.tools 里(不进仓库)
    keys = KeyStore({row["id"]: row["key_env"] for row in load_catalog()["providers"]})
    app = create_app(runs, Backend(load_modules(), state_dir=runs / ".tools", keys=keys))
    print(f"FatCat GEO 统一入口:http://127.0.0.1:{args.port}/  (只有这台电脑能打开;Ctrl+C 结束)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认不是 UTF-8,中文会乱
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="fatcat", description="FatCat GEO 统一入口")
    commands = parser.add_subparsers(dest="command", required=True)

    model_parser = commands.add_parser("models", help="统一型号清单")
    model_parser.add_argument(
        "action", choices=["sync", "show", "check"],
        help="sync:从 Toast 读出清单并存下;show:打印清单;check:核对统一清单和测量工具的声明,对不上退出码为 1"
             "(Potato、Butter 自己的型号表不在统一流程里,只列出来)",
    )
    model_parser.add_argument("--strict", action="store_true", help="check 时,不在统一流程里的那两张表对不上也算(退出码 1)")
    evidence_parser = commands.add_parser("evidence", help="证据表")
    evidence_parser.add_argument("action", choices=["check"], help="check:核对改进工具自带的那份复制件和测量工具的原件")
    commands.add_parser("sources", help="列出现在能拿来出报告的结果")
    report_parser = commands.add_parser("report", help="把几步的结果拼成一份长报告(一个 HTML 文件)")
    report_parser.add_argument("--toast-job", help="Toast 的单号(测量与诊断)")
    report_parser.add_argument("--butter-run", help="Butter 一次运行的文件夹(改进)")
    report_parser.add_argument("--butter-workspace", help="Butter 的工作区;不给就用它自己文件夹下的 workspace")
    report_parser.add_argument("--weblite", help="Quick Check 的证据包 zip")
    report_parser.add_argument("--lang", choices=["en", "zh"], default="en", help="报告语言,默认英文")
    report_parser.add_argument("--out", required=True, help="写到哪个 HTML 文件")
    report_parser.add_argument("--allow-mixed", action="store_true",
                               help="几份结果不是同一个网站也照样拼(只为看样子;报告开头会标明)")
    serve_parser = commands.add_parser("serve", help="打开统一入口的本机网页")
    serve_parser.add_argument("--port", type=int, default=8500)
    serve_parser.add_argument("--runs", help="项目存放的文件夹;不给就用本项目下的 runs")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "models":
            if args.action == "check":
                return _models_check(args.strict)
            return {"sync": _models_sync, "show": _models_show}[args.action]()
        if args.command == "evidence":
            return _evidence_check()
        if args.command == "sources":
            return _sources()
        return _report(args)
    except ModuleError as exc:
        print(f"出错:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
