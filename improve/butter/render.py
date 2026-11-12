"""把事实库和审计报告渲染成给人看的 Markdown。纯函数。外来文字进表格前先处理竖线和换行。"""

from __future__ import annotations

from butter.contracts import AuditReport, FactBase, FactField, SiteSnapshot
from butter.factbase import KIND_LABEL
from butter.util import collapse_ws, md_cell

LEVEL_LABEL = {"problem": "问题", "notice": "提示", "pass": "通过", "not_checkable": "查不了"}
CHECK_LABEL = {
    "meta_robots": "meta robots(noindex / nosnippet / noarchive / max-snippet)",
    "visible_date": "可见日期真实性",
    "numeric_traceability": "数值可溯源",
    "hidden_text": "隐藏文本与面向 AI 的指令",
    "jsonld_consistency": "JSON-LD 与正文一致",
}
ENTITY_LABEL = {
    "name": "名称", "aliases": "别名", "category": "类别", "official_domain": "官网域名", "same_as": "官方渠道(sameAs)",
    "description": "一句话描述", "logo": "标志图",
}
PRODUCT_LABEL = {
    "name": "名称", "description": "描述", "brand": "品牌", "sku": "SKU", "category": "类别", "price": "价格",
    "currency": "币种", "availability": "库存状态", "comparison_claims": "对比主张", "updated_date": "更新日期",
    "author": "负责人 / 作者",
}


_cell = md_cell  # 外来文字进 Markdown 前先中和


def _field_rows(label: str, field: FactField) -> list[str]:
    if not field.values:
        rows = [f"| {label} | (空缺) | | | |"]
    else:
        rows = []
        for i, v in enumerate(field.values):
            flag = _cell(field.conflict_note, 200) if field.conflict and i == 0 else ""
            rows.append(f"| {label if i == 0 else ''} | {_cell(v.value)} | {KIND_LABEL[v.source_kind]} · {_cell(v.source_path, 60)} | "
                        f"{v.source_url} | {flag} |")
    for c in field.candidates:
        rows.append(f"| | {_cell(c.value)} | {KIND_LABEL[c.source_kind]} · {_cell(c.source_path, 60)} | {c.source_url} | 只提示,未采用 |")
    return rows


def factbase_md(fb: FactBase) -> str:
    head = ["| 字段 | 值 | 来源类型 · 位置 | 来源 URL | 冲突 / 备注 |", "|---|---|---|---|---|"]
    lines = [f"# 事实库 · {fb.host}", "", f"- 网址:{fb.site_url}", f"- 运行号:{fb.run_id}",
             f"- 生成时间:{fb.generated_at.isoformat(timespec='seconds')}", "",
             "全部自动预填:网站自己说的默认采信;同一字段多个说法按 JSON-LD / microdata > meta / og > 标题 > 本机文件 取,并标冲突;"
             "空缺留空,不猜。这是一张可以改的表:改 `factbase.json` 里的值即可。", "",
             "## 必填层(品牌)", "", *head]
    e = fb.entity
    for key in ("name", "aliases", "category", "official_domain", "same_as", "description", "logo"):
        lines += _field_rows(ENTITY_LABEL[key], getattr(e, key))
    lines.append("")
    lines += ["## 可选层(按产品)", ""]
    if not fb.products:
        lines += ["没有找到机器可读的商品数据。", ""]
    for p in fb.products:
        title = p.name.values[0].value if p.name.values else p.product_id
        lines += [f"### {_cell(title)}", "", f"所在页面:{p.page_url}", "", *head]
        for key in ("name", "description", "brand", "sku", "category", "price", "currency", "availability",
                    "comparison_claims", "updated_date", "author"):
            lines += _field_rows(PRODUCT_LABEL[key], getattr(p, key))
        for spec in p.specs:
            lines.append(f"| 规格:{_cell(spec.name, 40)} | {_cell(spec.value)} | {KIND_LABEL[spec.source_kind]} · "
                         f"{_cell(spec.source_path, 60)} | {spec.source_url} | |")
        if not p.specs:
            lines.append("| 规格 | (空缺) | | | |")
        lines.append("")
    lines += ["## 空缺清单", ""]
    lines += [f"- `{g.code}` {g.message}" for g in fb.gaps] or ["- 无。"]
    lines += ["", "## 合并的本机文件", ""]
    lines += [f"- {KIND_LABEL[m.kind]} `{m.path}`:{'已合并' if m.status == 'merged' else '未合并'}。{m.note}"
              for m in fb.merged_inputs] or ["- 本次没有给 brands.yml 或 brand-domains.json。"]
    lines += ["", "## 各页现状(标题建议与日期检查用)", "", "| 页面 | 标题 | H1 | 找到的日期 |", "|---|---|---|---|"]
    for page in fb.pages:
        dates = ";".join(f"{d.raw}({d.kind})" for d in page.dates[:4])
        lines.append(f"| {page.url} | {_cell(page.title, 70)} | {_cell(' / '.join(page.h1), 70)} | {_cell(dates, 120)} |")
    return "\n".join(lines) + "\n"


def audit_md(report: AuditReport, snapshot: SiteSnapshot) -> str:
    counts = {level: sum(1 for f in report.findings if f.level == level) for level in LEVEL_LABEL}
    lines = [f"# 审计报告 · {snapshot.host}", "", f"- 网址:{report.site_url}", f"- 运行号:{report.run_id}",
             f"- 生成时间:{report.generated_at.isoformat(timespec='seconds')}",
             f"- 检查了 {len(report.pages_checked)} 个页面;抓取结束原因:{snapshot.stop_reason}", "",
             f"**合计:问题 {counts['problem']} · 提示 {counts['notice']} · 通过 {counts['pass']} · 查不了 {counts['not_checkable']}**", "",
             "五项检查都是规则判断,没有用模型。每条发现挂一个证据注册表条目 ID。"
             "robots 放行、可达性、CDN、服务端渲染的判定不在这里(Toast 负责)。", ""]
    if snapshot.notes:
        lines += ["抓取备注:", *[f"- {n}" for n in snapshot.notes], ""]
    for check, label in CHECK_LABEL.items():
        group = [f for f in report.findings if f.check == check]
        tally = " · ".join(f"{LEVEL_LABEL[lv]} {sum(1 for f in group if f.level == lv)}" for lv in LEVEL_LABEL)
        lines += [f"## {label}", "", tally, ""]
        if not group:
            lines += ["没有可检查的页面。", ""]
            continue
        order = {"problem": 0, "notice": 1, "not_checkable": 2, "pass": 3}
        for f in sorted(group, key=lambda f: order[f.level]):
            where = f.page_url or "全站"
            lines.append(f"- **{LEVEL_LABEL[f.level]}** `{f.finding_id}` {where}")
            lines.append(f"  - {f.message}")
            for key, value in f.evidence.items():
                shown = collapse_ws(value)[:220].replace("`", "ˋ")  # 放进代码片段:里面的记号不会被解释
                lines.append(f"  - {key}:`{shown}`")
            lines.append(f"  - 条目 ID:`{f.entry_id}`")
        lines.append("")
    lines += ["## 已知限制", ""] + [f"- {text}" for text in report.limitations]
    return "\n".join(lines) + "\n"
