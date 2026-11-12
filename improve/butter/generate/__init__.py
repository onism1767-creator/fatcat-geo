"""确定性生成器:事实库 + 站点快照 → 改动包(清单 + 文件内容)。纯函数,不联网、不写盘、不调用任何模型。

不产出 llms.txt(证据注册表里是 E 级,永不输出)。
Butter 不改任何人的站:改动包里的每一条都要站长自己放上服务器。
"""

from __future__ import annotations

from datetime import datetime

from butter.contracts import (
    ChangeItem, ChangePackage, ExcludedVendor, FactBase, NotGenerated, PackageBundle, PageExtract, Platform,
    QuestionBoard, SiteSnapshot, UaEntry,
)
from butter.generate.headings import build_headings
from butter.generate.jsonld import build_jsonld
from butter.generate.pages import build_pages
from butter.generate.robots import build_robots
from butter.util import md_cell, stop_label

FORBIDDEN_OUTPUTS = ("llms.txt", "llms-full.txt")
KIND_LABEL = {
    "robots": "robots.txt 三段式", "jsonld_organization": "Organization JSON-LD", "jsonld_product": "Product JSON-LD",
    "fact_page": "事实页", "fact_block": "价格 / 规格事实块", "heading_suggestion": "标题 / H1 / H2 / meta 建议",
}
ACTION_LABEL = {"add": "新增", "replace": "修改现有", "none": "无需改", "review": "供你参考"}
PURPOSE_LABEL = {"search_index": "搜索 / 引用", "training": "训练", "user_fetch": "用户触发取页",
                 "ai_uses_token": "训练用途令牌(不抓取)"}
_MAX_FACTS_SHOWN = 12
_NO_PAGES_REASON = ("本次没有抓到任何可分析的页面,不知道站上现在有什么;为免和站上已有的内容重复或冲突,没有生成。"
                    "换一个能读到的网址,或拿一份能读到的快照重跑。")


def _cell(text: str) -> str:
    return md_cell(text, 400)  # 外来文字进 Markdown 前先中和


def ua_table_md(ua_table: tuple[UaEntry, ...] | list[UaEntry]) -> str:
    lines = ["# UA 表(只收有官方爬虫文档的厂商)", "",
             "进表的唯一条件:厂商有官方的爬虫说明文档。每条带文档地址与核对日期。", "",
             "| 厂商 | robots 令牌 | 用途 | 官方对 robots.txt 的说法(转述) | 官方文档 | 核对日期 | 怎么核的 |",
             "|---|---|---|---|---|---|---|"]
    for e in ua_table:
        lines.append(f"| {_cell(e.vendor)} | `{e.robots_token}` | {PURPOSE_LABEL[e.purpose]} | {_cell(e.robots_stance)} | "
                     f"{e.docs_url} | {e.checked_on} | {_cell(e.checked_how)} |")
    notes = [e for e in ua_table if e.note]
    if notes:
        lines += ["", "## 备注", ""] + [f"- `{e.robots_token}`:{e.note}" for e in notes]
    return "\n".join(lines) + "\n"


def excluded_md(vendors: tuple[ExcludedVendor, ...] | list[ExcludedVendor]) -> str:
    lines = ["# 未纳入厂商与原因", "",
             "这些厂商没有可以打开的官方爬虫说明文档,所以不进 UA 表;Butter 不采用第三方名单里用途不明的 UA。",
             "厂商发布官方文档后即可纳入。", "",
             "| 厂商 | 相关产品 | 原因 | 查过哪里 | 核对日期 | 依据 |", "|---|---|---|---|---|---|"]
    for v in vendors:
        lines.append(f"| {_cell(v.vendor)} | {_cell(v.products)} | {_cell(v.reason)} | {_cell(v.looked_at)} | "
                     f"{v.checked_on} | {_cell(v.source)} |")
    return "\n".join(lines) + "\n"


def readme_md(package: ChangePackage, fb: FactBase, snapshot: SiteSnapshot) -> str:
    lines = [
        f"# Butter 改动包 · {fb.host}", "",
        f"- 网址:{package.site_url}", f"- 运行号:{package.run_id}",
        f"- 生成时间:{package.generated_at.isoformat(timespec='seconds')}",
        f"- 依据:同一次运行的 `factbase.json`(事实库)与 `audit.json`(审计报告)", "",
        "> Butter 帮你把有证据该做的事一次做对;有没有效果,由再测说了算。",
        "> 这里没有任何效果承诺。看到变化不等于证明了原因(Observed change ≠ proven cause)。",
        "> 条目的验证态最高为「假设」;技术修复类只写「官方文档要求,必要非充分」。",
        "> Butter 不会改你的站:下面每一条都要你自己放上服务器,放之前请逐条看过。", "",
        "## 改动清单", "",
        "| # | 改什么 | 放哪里 | 动作 | 条目 ID | 验证态 |", "|---|---|---|---|---|---|",
    ]
    for i, item in enumerate(package.items, start=1):
        lines.append(f"| {i} | {KIND_LABEL[item.kind]} | {_cell(item.target)} | {ACTION_LABEL[item.action]} | "
                     f"`{item.entry_id}` | {item.validation_state} |")
    if not package.items:
        lines.append("| — | 本次没有产出任何改动 | | | | |")
    lines.append("")
    for i, item in enumerate(package.items, start=1):
        lines += [f"### {i}. {KIND_LABEL[item.kind]} — {ACTION_LABEL[item.action]}", "",
                  f"- 放哪里:{item.target}"]
        if item.files:
            lines.append("- 文件:" + "、".join(f"`{f}`" for f in item.files))
        lines += [f"- 为什么:{item.rationale}", f"- 条目 ID:`{item.entry_id}` · 验证态:{item.validation_state}"]
        if item.needs_human:
            lines.append("- 需要你判断:")
            lines += [f"  - {note}" for note in item.needs_human]
        if item.facts_used:
            lines.append(f"- 用到的事实({len(item.facts_used)} 条,每条带来源):")
            lines += [f"  - `{ref.field}` = {md_cell(ref.value, 120)}(来源:{ref.source_url})"
                      for ref in item.facts_used[:_MAX_FACTS_SHOWN]]
            if len(item.facts_used) > _MAX_FACTS_SHOWN:
                lines.append(f"  - ……其余 {len(item.facts_used) - _MAX_FACTS_SHOWN} 条见 `manifest.json`")
        lines.append("")
    lines += ["## 没有产出的", ""]
    lines += [f"- **{n.what}**:{n.reason}" for n in package.not_generated] or ["- 无。"]
    lines += ["", "## 附:UA 表", "",
              "- `reference/ua-table.md`:robots 三段式用到的 UA,每条带官方文档地址与核对日期。",
              "- `reference/excluded-vendors.md`:未纳入的厂商与原因。", "",
              ]
    if snapshot.source == "local_files":
        lines += ["## 本次读取(本地导入)", "",
                  "- Butter 这次没有向你的站发任何请求。",
                  f"- 导入了 {len(snapshot.pages)} 个文件,其中可分析的 {len(fb.pages)} 个。"]
    else:
        lines += ["## 本次抓取", "",
                  f"- 用的是固定版本的 Chrome 浏览器 UA,没有冒充任何爬虫:`{snapshot.user_agent}`",
                  f"- 收到 {len(snapshot.pages)} 个页面响应,其中可分析的 {len(fb.pages)} 个;"
                  f"结束原因:{stop_label(snapshot.stop_reason)}"]
    lines += [f"- {md_cell(note, 700)}" for note in snapshot.notes]
    lines += ["", "## 已知限制", "",
              "- Butter 不执行页面脚本:原始 HTML 里没有的内容看不到。",
              "- 价格、规格只取结构化数据和 meta 里已有的,不从正文里抽;事实库里没有的字段,生成的文件里就没有。",
              "- 不产出 llms.txt。",
              "- 只看了本次读到的页面,不代表全站。", ""]
    return "\n".join(lines)


def build_package(
    snapshot: SiteSnapshot,
    extracts: list[PageExtract],
    fb: FactBase,
    *,
    ua_table: tuple[UaEntry, ...] | list[UaEntry],
    excluded: tuple[ExcludedVendor, ...] | list[ExcludedVendor],
    platforms: tuple[Platform, ...] | list[Platform] = (),
    board: QuestionBoard | None,
    training: str,
    now: datetime,
) -> PackageBundle:
    today = now.date().isoformat()
    ordered = sorted(extracts, key=lambda p: 0 if p.role == "home" else (1 if p.role == "given" else 2))
    reference_hosts = frozenset(h for p in platforms if p.kind == "reference" for h in p.hosts)
    files: dict[str, str] = {}
    items: list[ChangeItem] = []
    missing: list[NotGenerated] = []
    read_date = snapshot.fetched_at.date().isoformat()
    read_note = (f"页面来自你存在本机的文件:页脚的读取日期 {read_date} 取的是文件的修改时间,"
                 "和你实际保存页面的日子不一样的话请改。" if snapshot.source == "local_files" else "")
    if ordered:
        content_parts = (build_jsonld(fb, ordered, snapshot.origin),
                         build_pages(fb, snapshot.origin, read_date, reference_hosts, read_note))
    else:  # 一页都没看到:不知道站上现有什么,不能说「站上没有」,也不凭本机文件里的一个名字造页面
        content_parts = (({}, [], [
            NotGenerated(what="Organization / Product JSON-LD", reason=_NO_PAGES_REASON),
            NotGenerated(what="事实页与价格 / 规格 / 对比表事实块", reason=_NO_PAGES_REASON)]),)
    for part_files, part_items, part_missing in (
        build_robots(snapshot.robots, ua_table, training=training, sitemap_urls=snapshot.sitemap_urls, today=today),
        *content_parts,
        build_headings(fb, board),
    ):
        files.update(part_files)
        items.extend(part_items)
        missing.extend(part_missing)

    package = ChangePackage(site_url=snapshot.site_url, run_id=fb.run_id, generated_at=now, items=items,
                            not_generated=missing)
    files["reference/ua-table.md"] = ua_table_md(ua_table)
    files["reference/excluded-vendors.md"] = excluded_md(excluded)
    files["README.md"] = readme_md(package, fb, snapshot)
    files["manifest.json"] = package.model_dump_json(indent=2) + "\n"
    for name in files:
        if name.rsplit("/", 1)[-1].lower() in FORBIDDEN_OUTPUTS:
            raise AssertionError(f"改动包不得包含 {name}")
    return PackageBundle(package=package, files=files)
