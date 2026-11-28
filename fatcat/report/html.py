"""把长报告写成一个能离线打开的 HTML 文件:没有脚本,没有外链,样式写在文件里。

样子只求能读(版式以后在网站那边统一设计)。所有来自外面的文字一律转义后再写进页面。
"""

from __future__ import annotations

from html import escape

from fatcat.report.build import Block, Report
from fatcat.report.words import WORDS

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1b1b1f; --muted: #5c5f66; --line: #d7d9de; --soft: #f4f5f7; --warn: #fff4d6;
  /* 改之前蓝、改之后橙:两种颜色只分前后,不表示好坏(报告不判变没变)。色盲下也分得开,深浅两种底色都验过 */
  --before: #2a78d6; --after: #eb6834; --tint: 7%;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #16171a; --fg: #e8e9ec; --muted: #a3a6ad; --line: #3a3d44; --soft: #212328; --warn: #4a3d12;
          --before: #3987e5; --after: #d95926; --tint: 12%; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.6 system-ui, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main, header, nav { max-inline-size: 60rem; margin-inline: auto; padding-inline: 1.25rem; }
header { padding-block: 2rem 0.5rem; }
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 0.4rem; }
h2 { font-size: 1.3rem; margin: 2.5rem 0 0.8rem; padding-block-start: 1rem; border-block-start: 1px solid var(--line); }
h3 { font-size: 1.05rem; margin: 1.4rem 0 0.4rem; }
h4 { font-size: 0.95rem; margin: 0.8rem 0 0.2rem; color: var(--muted); }
p, li { overflow-wrap: anywhere; }
.sub, .from { color: var(--muted); font-size: 0.9rem; }
.warn, .note { padding: 0.7rem 1rem; border-radius: 6px; background: var(--soft); }
.warn { background: var(--warn); }
nav ol { columns: 2; padding-inline-start: 1.4rem; }
nav a { color: inherit; }
.table-wrapper { overflow: auto; max-inline-size: 100%; container-type: inline-size; margin-block: 0.6rem 1.2rem;
                 border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: separate; border-spacing: 0; inline-size: 100%; font-size: 0.92rem; }
caption { text-align: start; font-weight: 600; padding: 0.6rem 0.8rem; background: var(--soft); }
th, td { text-align: start; vertical-align: top; padding: 0.45rem 0.8rem; border-block-start: 1px solid var(--line);
         overflow-wrap: anywhere; min-inline-size: 7rem; }
thead th { background: var(--soft); white-space: nowrap; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: 0.2rem 1rem 0.8rem; margin-block: 1rem; }
.card ul { margin-block: 0.2rem; }
/* 改前改后:左右两页,窄屏上下叠;两页列宽一样,同一个 AI 在两页的同一行 */
.pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); gap: 1rem;
        margin-block: 0.8rem 1.4rem; }
.side { --tone: var(--before); border: 1px solid var(--line); border-block-start: 0.3rem solid var(--tone);
        border-radius: 6px; padding: 0.1rem 0.9rem 0.6rem; background: var(--bg);
        background: color-mix(in oklab, var(--tone) var(--tint), var(--bg)); }
.side.after { --tone: var(--after); }
.side h3 { display: flex; align-items: center; gap: 0.5rem; margin-block: 0.7rem 0; }
.side h3::before { content: ""; flex: none; inline-size: 0.75rem; block-size: 0.75rem; border-radius: 2px;
                   background: var(--tone); }
.side .sub { margin-block: 0.1rem 0.5rem; }
.side table { table-layout: fixed; font-variant-numeric: tabular-nums; }
.side col.ai { width: 44%; }
.side .unit { display: block; font-size: 0.8rem; font-weight: 400; color: var(--muted); }
.side th, .side td { min-inline-size: 0; padding-inline: 0.5rem; }
.side thead th { background: transparent; white-space: normal; vertical-align: bottom; }
@media print {
  nav { display: none; }
  h2 { break-after: avoid; }
  .card, tr, .side { break-inside: avoid; }
  .side { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  .table-wrapper { overflow: visible; border: 0; }
}
"""


class _Ids:
    """页面里每张表的标题要有一个不重复的 id,给包着它的可滚动区域当名字。"""

    def __init__(self) -> None:
        self._next = 0

    def new(self) -> str:
        self._next += 1
        return f"t{self._next}"


def _from_note(block: Block, lang: str, told: set[str]) -> str:
    """这一块的原文和报告不是同一种语言时,在它上面标一句;同一章里同一种语言只标一次。"""
    if block.lang is None or block.lang in told:
        return ""
    told.add(block.lang)
    return f'<p class="from">{escape(WORDS[lang]["lang_note_" + block.lang])}</p>'


def _block(block: Block, lang: str, ids: _Ids, told: set[str]) -> str:
    attr = f' lang="{block.lang}"' if block.lang else ""
    if block.kind == "p":
        return f"<p{attr}>{escape(block.text)}</p>"
    if block.kind == "note":
        return f'<p class="note"{attr}>{escape(block.text)}</p>'
    if block.kind == "list":
        items = "".join(f"<li>{escape(item)}</li>" for item in block.items)
        heading = f"<h3>{escape(block.caption)}</h3>" if block.caption else ""
        return f"{heading}{_from_note(block, lang, told)}<ul{attr}>{items}</ul>"
    if block.kind == "table":
        table_id = ids.new()
        head = "".join(f'<th scope="col">{escape(cell)}</th>' for cell in block.head)
        body = "".join(
            "<tr>" + "".join(f'<th scope="row">{escape(cell)}</th>' if i == 0 else f"<td>{escape(cell)}</td>"
                             for i, cell in enumerate(row)) + "</tr>"
            for row in block.rows)
        return (f'{_from_note(block, lang, told)}<div class="table-wrapper" tabindex="0" role="region" '
                f'aria-labelledby="{table_id}"><table{attr}><caption id="{table_id}">{escape(block.caption)}</caption>'
                f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
    if block.kind == "card":
        parts = [f'<section class="card"><h3>{escape(block.caption)}</h3>{_from_note(block, lang, told)}']
        for title, lines in block.sections:
            parts.append(f"<h4>{escape(title)}</h4>")
            if lines:
                parts.append("<ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>")
        parts.append("</section>")
        return "".join(parts)
    if block.kind == "pair":
        parts = ['<div class="pair">']
        for side in block.sides:
            side_id = ids.new()
            units = side.units or [""] * len(side.head)
            head = "".join(f'<th scope="col">{escape(cell)}'
                           + (f'<span class="unit">{escape(unit)}</span>' if unit else "") + "</th>"
                           for cell, unit in zip(side.head, units, strict=True))
            body = "".join(
                "<tr>" + "".join(f'<th scope="row">{escape(cell)}</th>' if i == 0 else f"<td>{escape(cell)}</td>"
                                 for i, cell in enumerate(row)) + "</tr>"
                for row in side.rows)
            cols = '<col class="ai">' + "<col>" * (len(side.head) - 1)
            parts.append(f'<section class="side {side.tone}" aria-labelledby="{side_id}">'
                         f'<h3 id="{side_id}">{escape(side.title)}</h3><p class="sub">{escape(side.sub)}</p>'
                         f'<table aria-labelledby="{side_id}"><colgroup>{cols}</colgroup>'
                         f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></section>")
        return "".join(parts) + "</div>"
    raise ValueError(f"不认识的块:{block.kind}")


def render(report: Report) -> str:
    words, ids = WORDS[report.lang], _Ids()
    chapters = ""
    for c in report.chapters:
        told: set[str] = set()
        blocks = "".join(_block(block, report.lang, ids, told) for block in c.blocks)
        if report.numbered:
            chapters += (f'<section id="c{c.no}" aria-labelledby="h{c.no}">'
                         f'<h2 id="h{c.no}">{c.no}. {escape(c.title)}</h2>{blocks}</section>')
        else:  # 单独的对比报告:标题就是页面的大标题,不再加一层章名
            chapters += f"<section>{blocks}</section>"
    toc = "".join(f'<li><a href="#c{c.no}">{escape(c.title)}</a></li>' for c in report.chapters)
    nav = (f'<nav aria-label="{escape(words["toc"])}"><ol start="0">{toc}</ol></nav>\n' if report.numbered else "")
    warning = f'<p class="warn" role="note">{escape(report.warning)}</p>' if report.warning else ""
    return (
        f'<!doctype html>\n<html lang="{report.lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(report.title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f'<header><h1>{escape(report.title)}</h1><p class="sub">{escape(report.subtitle)}</p>{warning}</header>\n'
        f"{nav}<main>{chapters}</main>\n</body>\n</html>\n"
    )
