"""命令行入口。薄:只做参数解析和转发,规则都在库里。

    python -m butter run https://example.com
    python -m butter run https://example.com --brands brands.yml --questions questions.yml
    python -m butter run --snapshot out/example.com/<运行号>      # 拿旧快照不联网重跑
    python -m butter run https://example.com --html-dir 存下的页面/  # 不联网:读你自己存下的 HTML 文件
    python -m butter proposals                                     # 生成给 Toast 的提案文档(只是文档)
    python -m butter rebuild-index                                 # 按事件日志重建卡片索引
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from butter.pipeline import DraftSettings, RunOptions, process_snapshot
from butter.util import stop_label

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "out"
DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"  # 卡片库、索引、提案(设计 §6);不进 git


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butter", description="Butter(FatCat GEO Improve):一个网址进,事实库 / 审计报告 / 改动包出。")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="抓一个站并产出事实库、审计报告、改动包")
    run.add_argument("url", nargs="?", help="公开网址(只抓这一个主机)")
    run.add_argument("--snapshot", type=Path, help="不联网:用一次旧运行的文件夹(或其中的 snapshot/)重跑")
    run.add_argument("--html-dir", type=Path,
                     help="不联网:读一个文件夹里你自己存下的 HTML 文件(要同时给网址,说明是哪个站)。"
                          "站点对程序请求下发人机验证时用:在浏览器里打开页面,另存为「网页,仅 HTML」放进文件夹;"
                          "可选再放 robots.txt、sitemap.xml 和 urls.txt(每行「文件名 网址」)")
    run.add_argument("--brands", type=Path, help="Potato 的 brands.yml(只读)")
    run.add_argument("--brand-domains", type=Path, help="Toast 导出的 brand-domains.json(只读)")
    run.add_argument("--questions", type=Path, help="Potato 的 questions.yml(只读;给了才出按题板的标题建议)")
    run.add_argument("--training", choices=("keep", "optout"), default="keep",
                     help="训练类爬虫:keep 不改动(默认),optout 写 Disallow")
    run.add_argument("--max-pages", type=int, default=10, help="最多抓几页(含给定页和首页,默认 10)")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"产出目录(默认 {DEFAULT_OUT})")
    run.add_argument("--no-draft", action="store_true", help="不让模型起草(不调模型、不花钱),只出规则生成的部分")
    run.add_argument("--env-file", type=Path, help="从这个 .env 文件读模型的 key(只读);不给就只看环境变量")
    run.add_argument("--draft-provider", choices=("anthropic", "openai", "gemini"),
                     help="起草用哪家;不给就按你有哪家的 key 来定(几家都有时先用 Anthropic)")
    run.add_argument("--draft-model", help="起草用哪个型号(要在价目表里);不给就用那一家的默认型号")
    run.add_argument("--draft-budget", type=float, default=1.0, help="这次运行起草最多花多少美元(默认 1.0)")

    proposals = sub.add_parser("proposals", help="生成给 Toast 的提案文档(只是文档,不改 Toast 的任何文件)")
    proposals.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                           help=f"卡片库所在的工作目录(默认 {DEFAULT_WORKSPACE});里面没有卡片也能出前两份提案")
    proposals.add_argument("--out", type=Path, help="存进哪个新文件夹(默认 <工作目录>/proposals/<日期-时间>);已存在就不写")
    proposals.add_argument("--toast-registry", type=Path,
                           help="Toast 的 config/evidence_registry.yaml(只读);不给就找旁边的 Toast 文件夹,找不到用自带快照")
    index = sub.add_parser("rebuild-index", help="按事件日志整份重建卡片索引(index.sqlite 删了、坏了时用)")
    index.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help=f"工作目录(默认 {DEFAULT_WORKSPACE})")
    web = sub.add_parser("serve", help="在本机打开 Butter 的网页界面(只有这台电脑能访问)")
    web.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"运行产出目录(默认 {DEFAULT_OUT})")
    web.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help=f"工作目录(默认 {DEFAULT_WORKSPACE})")
    web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser


def _proposals(args) -> int:
    from datetime import datetime

    from butter import cards, store
    from butter.proposals import build_proposals
    from butter.registry import load_registry

    now = datetime.now()
    registry = load_registry(args.toast_registry)
    view, problems = cards.load(args.workspace)
    for problem in problems:
        print(f"注意:{problem}", file=sys.stderr)
    out_dir = args.out or args.workspace / "proposals" / now.strftime("%Y%m%d-%H%M%S")
    written = store.save_text_files(out_dir, build_proposals(registry, view.methods, generated_on=now.date().isoformat()))
    print(f"提案文档存在:{out_dir}")
    for path in written:
        print(f"  {path.name}")
    print("这些只是文档,Butter 没有改 Toast 的任何文件;采不采纳由你看过再定。")
    return 0


def _rebuild_index(args) -> int:
    from butter import cards

    view, problems = cards.rebuild(args.workspace)
    for problem in problems:
        print(f"注意:{problem}", file=sys.stderr)
    for skipped in view.skipped:
        print(f"注意:日志第 {skipped.seq} 条事件不合规,没有计入({skipped.reason})", file=sys.stderr)
    print(f"索引已按事件日志重建:{view.events} 条事件,{len(view.batches)} 个批次,{len(view.cards)} 张卡。")
    return 0


def _draft_settings(args, out_root: Path) -> DraftSettings | None:
    """按命令行和手上的 key 决定起不起草、用哪家。没有 key 就不起草,照常出规则生成的部分。"""
    if args.no_draft:
        return None
    import os

    from butter.config import load_pricing
    from butter.draft import PROMPT_VERSION, SYSTEM
    from butter.llm import choose_provider, complete, find_price, upper_bound_usd
    from butter.llm.keys import available_providers, read_env_file

    env = dict(os.environ)
    if args.env_file:
        env.update(read_env_file(args.env_file))
    pricing = load_pricing()
    choice = choose_provider(pricing, available_providers(env), args.draft_provider)
    if choice is None:
        print("没有找到任何一家模型的 key,这次不起草;规则生成的部分照常出。")
        return None
    provider, model = choice[0], args.draft_model or choice[1]
    price = find_price(pricing, provider, model)
    if price is None:
        print(f"价目表里没有 {provider}:{model},这次不起草。", file=sys.stderr)
        return None
    from datetime import date

    bound = min(args.draft_budget, upper_bound_usd(price, args.max_pages, 4000, 4000, date.today().isoformat()))
    print(f"起草用 {provider}:{model},这次最多花约 ${bound:.2f}(上限 ${args.draft_budget:.2f});不想起草加 --no-draft。")
    cache_dir = out_root / ".llm-cache"

    def ask(prompt: str, schema: dict):
        return complete(provider, model, prompt, pricing=pricing, prompt_version=PROMPT_VERSION, system=SYSTEM,
                        schema=schema, cache_dir=cache_dir, env=env)

    return DraftSettings(provider=provider, model=model, ask=ask, budget_usd=args.draft_budget, max_pages=args.max_pages)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认编码写不了中文
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    if args.command == "serve":
        from butter.web.serve import serve

        return serve(args.out, args.workspace, open_browser=not args.no_browser)
    if args.command in ("proposals", "rebuild-index"):
        try:
            return _proposals(args) if args.command == "proposals" else _rebuild_index(args)
        except (ValueError, OSError) as exc:
            print(f"没做成:{exc}", file=sys.stderr)
            return 1
    if bool(args.url) == bool(args.snapshot):
        print("要么给一个网址,要么给 --snapshot,二选一。", file=sys.stderr)
        return 2
    if args.html_dir and args.snapshot:
        print("--html-dir 要和网址一起给,不能和 --snapshot 同用。", file=sys.stderr)
        return 2
    options = RunOptions(out_root=args.out, brands_path=args.brands, brand_domains_path=args.brand_domains,
                         questions_path=args.questions, training=args.training,
                         draft=_draft_settings(args, args.out))
    try:
        if args.snapshot:
            from butter.store import load_snapshot

            snapshot = load_snapshot(args.snapshot)
            source = f"snapshot:{args.snapshot}"
        elif args.html_dir:
            from butter.local_import import import_site

            print(f"不联网:读取文件夹 {args.html_dir} 里的 HTML 文件,当作 {args.url} 的页面…")
            snapshot = import_site(args.url, args.html_dir)
            source = f"html-dir:{args.html_dir.name}"
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
    if result.snapshot.source == "local_files":
        print(f"导入了 {len(result.snapshot.pages)} 个本机 HTML 文件,其中可分析的 {len(result.factbase.pages)} 个(没有联网)。")
    else:
        print(f"收到 {len(result.snapshot.pages)} 个页面响应,其中可分析的 {len(result.factbase.pages)} 个"
              f"(结束原因:{stop_label(result.snapshot.stop_reason)})。")
    for note in result.snapshot.notes:
        print(f"  · {note}")
    print(f"事实库:{len(result.factbase.products)} 个商品,{len(result.factbase.gaps)} 条空缺。")
    print(f"审计:问题 {counts['problem']} · 提示 {counts['notice']} · 通过 {counts['pass']} · 查不了 {counts['not_checkable']}。")
    print(f"改动包:{len(result.bundle.package.items)} 条改动,{len(result.bundle.package.not_generated)} 项没有产出。")
    if result.drafts is not None:
        d = result.drafts
        print(f"起草:通过 {len(d.items)} 条,作废 {len(d.voided)} 条;发了 {d.calls} 次请求,花了 ${d.cost_usd:.4f}。")
        if d.stopped:
            print(f"注意:{d.stopped}")
    for warning in result.warnings:
        print(f"注意:{warning}")
    print(f"产出在:{result.run_dir}")
    print("  factbase.md / factbase.json   事实库")
    print("  audit.md / audit.json         审计报告")
    print("  change-package/README.md      改动包(从这里看起)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
