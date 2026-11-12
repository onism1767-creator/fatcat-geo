"""命令行入口。薄:只做参数解析和转发,规则都在库里。

    python -m butter run https://example.com
    python -m butter run https://example.com --brands brands.yml --questions questions.yml
    python -m butter run --snapshot out/example.com/<运行号>      # 拿旧快照不联网重跑
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from butter.pipeline import RunOptions, process_snapshot

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "out"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butter", description="Butter(FatCat GEO Improve):一个网址进,事实库 / 审计报告 / 改动包出。")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="抓一个站并产出事实库、审计报告、改动包")
    run.add_argument("url", nargs="?", help="公开网址(只抓这一个主机)")
    run.add_argument("--snapshot", type=Path, help="不联网:用一次旧运行的文件夹(或其中的 snapshot/)重跑")
    run.add_argument("--brands", type=Path, help="Potato 的 brands.yml(只读)")
    run.add_argument("--brand-domains", type=Path, help="Toast 导出的 brand-domains.json(只读)")
    run.add_argument("--questions", type=Path, help="Potato 的 questions.yml(只读;给了才出按题板的标题建议)")
    run.add_argument("--training", choices=("keep", "optout"), default="keep",
                     help="训练类爬虫:keep 不改动(默认),optout 写 Disallow")
    run.add_argument("--max-pages", type=int, default=10, help="最多抓几页(含给定页和首页,默认 10)")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"产出目录(默认 {DEFAULT_OUT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认编码写不了中文
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    if bool(args.url) == bool(args.snapshot):
        print("要么给一个网址,要么给 --snapshot,二选一。", file=sys.stderr)
        return 2
    options = RunOptions(out_root=args.out, brands_path=args.brands, brand_domains_path=args.brand_domains,
                         questions_path=args.questions, training=args.training)
    try:
        if args.snapshot:
            from butter.store import load_snapshot

            snapshot = load_snapshot(args.snapshot)
            source = f"snapshot:{args.snapshot}"
        else:
            from butter.fetch import FetchLimits, fetch_site

            if not 1 <= args.max_pages <= 30:
                print("--max-pages 要在 1 到 30 之间。", file=sys.stderr)
                return 2
            print(f"开始抓取 {args.url}(最多 {args.max_pages} 页,每秒 1 个请求,遇验证码即停)…")
            snapshot = fetch_site(args.url, limits=FetchLimits(max_pages=args.max_pages))
            source = "fetched"
        result = process_snapshot(snapshot, options, snapshot_source=source)
    except (ValueError, OSError) as exc:
        print(f"没跑成:{exc}", file=sys.stderr)
        return 1

    counts = {lv: sum(1 for f in result.audit.findings if f.level == lv) for lv in ("problem", "notice", "pass", "not_checkable")}
    got = len([p for p in result.snapshot.pages if p.html])
    print(f"抓到 {got} 个页面(结束原因:{result.snapshot.stop_reason})。")
    for note in result.snapshot.notes:
        print(f"  · {note}")
    print(f"事实库:{len(result.factbase.products)} 个商品,{len(result.factbase.gaps)} 条空缺。")
    print(f"审计:问题 {counts['problem']} · 提示 {counts['notice']} · 通过 {counts['pass']} · 查不了 {counts['not_checkable']}。")
    print(f"改动包:{len(result.bundle.package.items)} 条改动,{len(result.bundle.package.not_generated)} 项没有产出。")
    for warning in result.warnings:
        print(f"注意:{warning}")
    print(f"产出在:{result.run_dir}")
    print("  factbase.md / factbase.json   事实库")
    print("  audit.md / audit.json         审计报告")
    print("  change-package/README.md      改动包(从这里看起)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
