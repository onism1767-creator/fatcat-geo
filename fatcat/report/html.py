"""把长报告写成一个能离线打开的 HTML 文件:没有脚本,没有外链,样式写在文件里。

样子只求能读(版式以后在网站那边统一设计)。所有来自外面的文字一律转义后再写进页面。
"""

from __future__ import annotations

import re
from html import escape

from fatcat.report.build import Block, Report
from fatcat.report.words import WORDS

_CSS = """
/* 白底,网站的颜色:黑字、珊瑚、浅蓝、灰。用色块分区,不画框线。间距都是 --u 的整数倍。
   字体只有两族:标题衬线、正文无衬线。报告要能离线打开,不带字体文件,电脑上没有时退到后面的字体。 */
:root {
  color-scheme: light;
  --bg: #fff; --fg: #000; --muted: rgb(0 0 0 / .66); --line: rgb(0 0 0 / .14);
  --coral: #dc6f52; --blue: #bed8f3; --grey: #9c9d9f;
  --soft: #eef4fb; --warn: #fbe9e3;
  --before: #dcebf9; --after: #fbe3da; --before-dot: #5b9bdc; --after-dot: #dc6f52;
  --good: #1f7a4d; --unstable: #b57c00;
  --serif: "Hedvig Letters Serif", "Iowan Old Style", "Palatino Linotype", Georgia, "Songti SC", "Noto Serif SC",
           "Source Han Serif SC", "PingFang SC", "Microsoft YaHei", serif;
  --sans: "Hanken Grotesk", system-ui, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
  --u: 0.5rem;
  --pad: clamp(1.25rem, 5vw, 3.5rem);
}
*, *::before, *::after { box-sizing: border-box; }
html { background: var(--bg); scrollbar-color: var(--grey) var(--bg); }
body { margin: 0; background: var(--bg); color: var(--fg); font: 1rem/1.6 var(--sans); }
::selection { background: var(--coral); color: #000; }
:focus-visible { outline: 2px solid var(--fg); outline-offset: 3px; }
a { color: inherit; text-decoration-color: var(--coral); text-decoration-thickness: 2px; text-underline-offset: .2em; }
a:hover { text-decoration-color: var(--fg); }

main, header, nav { max-inline-size: 60rem; margin-inline: auto; padding-inline: var(--pad); }
header { padding-block: calc(var(--u) * 8) 0; }
main { padding-block-end: calc(var(--u) * 12); }
h1, h2 { font-family: var(--serif); font-weight: 400; letter-spacing: -0.015em; text-wrap: balance; }
h1 { margin: 0 0 calc(var(--u) * 2); font-size: clamp(2rem, 1.4rem + 2.4vw, 3rem); line-height: 1.1; }
h2 { margin: calc(var(--u) * 12) 0 calc(var(--u) * 4); font-size: clamp(1.5rem, 1.25rem + 1vw, 2rem); line-height: 1.2;
     scroll-margin-block-start: calc(var(--u) * 2); }
h3 { margin: calc(var(--u) * 6) 0 calc(var(--u) * 2); font-size: 1.125rem; line-height: 1.4; }
/* 章标题下面第一样东西,不管是什么,都离标题一样远 */
h2 + *, h2 + .from + * { margin-block-start: 0 !important; }
h4 { margin: calc(var(--u) * 3) 0 var(--u); color: var(--muted); font-size: .9375rem; font-weight: 600; line-height: 1.6; }
p, ul, ol { margin-block: 0 calc(var(--u) * 2); }
p, li { overflow-wrap: anywhere; text-wrap: pretty; }
main p, main ul, header p { max-inline-size: 44rem; }
li + li { margin-block-start: var(--u); }
.sub, .from { color: var(--muted); font-size: .9375rem; }

/* 目录:一块浅蓝底,两栏 */
nav { margin-block-start: calc(var(--u) * 4); }
nav ol { margin: 0; padding: calc(var(--u) * 3) calc(var(--u) * 3) calc(var(--u) * 3) calc(var(--u) * 6);
         columns: 2 16rem; column-gap: calc(var(--u) * 6); border-radius: 1rem; background: var(--soft); }
nav li { break-inside: avoid; }
nav li + li { margin-block-start: var(--u); }
nav li::marker { color: var(--muted); font-variant-numeric: tabular-nums; }

.warn, .note { padding: calc(var(--u) * 2) calc(var(--u) * 3); border-radius: .75rem; background: var(--soft); }
.warn { background: var(--warn); }
header .warn { margin-block-start: calc(var(--u) * 2); }

/* 表:表头一条浅蓝色块,行与行之间一条细线,外面不画框 */
.table-wrapper { max-inline-size: 100%; margin-block: 0 calc(var(--u) * 6); overflow-x: auto; }
h3 + .table-wrapper, p + .table-wrapper, ul + .table-wrapper, .table-wrapper + .table-wrapper,
.note + .table-wrapper, .from + .table-wrapper { margin-block-start: calc(var(--u) * 4); }
table { inline-size: 100%; border-collapse: collapse; font-size: .9375rem; font-variant-numeric: tabular-nums; }
caption { padding: 0 0 calc(var(--u) * 2); font-size: 1.125rem; font-weight: 600; line-height: 1.4; text-align: start; }
th, td { min-inline-size: 7rem; padding: calc(var(--u) * 1.5) calc(var(--u) * 2); border-block-end: 1px solid var(--line);
         text-align: start; vertical-align: top; overflow-wrap: anywhere; }
thead th { background: var(--soft); border-block-end: 0; font-weight: 600; white-space: nowrap; }
thead th:first-child { border-start-start-radius: .5rem; border-end-start-radius: .5rem; }
thead th:last-child { border-start-end-radius: .5rem; border-end-end-radius: .5rem; }
tbody tr:last-child > * { border-block-end: 0; }
tbody th { font-weight: 600; }

/* 一家 AI 在一格里的结果:一个圆点,后面跟着字 */
.mark { display: inline-block; inline-size: .625rem; block-size: .625rem; margin-inline-end: .375rem; border-radius: 50%; }
.mark-good { background: var(--good); }
.mark-unstable { background: var(--unstable); }
.mark-weak { background: var(--coral); }
.mark-none { box-shadow: inset 0 0 0 1.5px var(--grey); }

/* 一条建议:一块浅蓝底 */
.card { margin-block: calc(var(--u) * 4); padding: calc(var(--u) * 3) calc(var(--u) * 3) calc(var(--u) * 1);
        border-radius: 1rem; background: var(--soft); }
.card h3 { margin-block-start: 0; }
.card h4:first-of-type { margin-block-start: 0; }

/* 改前改后:左右两页,窄屏上下叠。改之前浅蓝、改之后珊瑚,是网站的两种主题色,只分前后,不表示好坏。
   两页列宽一样,同一家 AI 在两页的同一行 */
.pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); gap: calc(var(--u) * 2);
        margin-block: calc(var(--u) * 4) calc(var(--u) * 6); }
.side { --tone: var(--before); --dot: var(--before-dot); border-radius: 1rem; background: var(--tone);
        padding: calc(var(--u) * 3) calc(var(--u) * 3) calc(var(--u) * 2); }
.side.after { --tone: var(--after); --dot: var(--after-dot); }
.side h3 { display: flex; align-items: center; gap: var(--u); margin: 0; font-family: var(--serif); font-weight: 400;
           font-size: 1.5rem; line-height: 1.2; }
.side h3::before { content: ""; flex: none; inline-size: .75rem; block-size: .75rem; border-radius: 50%; background: var(--dot); }
.side .sub { margin-block: var(--u) calc(var(--u) * 2); }
.side table { table-layout: fixed; }
.side col.ai { width: 40%; }
.side td { white-space: nowrap; }
.side th, .side td { min-inline-size: 0; padding-inline: 0 var(--u); border-block-end-color: rgb(0 0 0 / .18); }
.side thead th { background: transparent; border-block-end: 1px solid rgb(0 0 0 / .18); border-radius: 0; white-space: normal;
                 vertical-align: bottom; }
.side .unit { display: block; font-size: .8125rem; font-weight: 400; color: var(--muted); }

@media (forced-colors: active) {
  .side, .card, .note, .warn, nav ol { border: 1px solid CanvasText; }
  .mark, .side h3::before { forced-color-adjust: none; }
}
@media print {
  nav { display: none; }
  h2, h3 { break-after: avoid; }
  .card, tr, .side { break-inside: avoid; }
  .side, .card, .note, .warn, thead th, .mark { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  .table-wrapper { overflow: visible; }
}
"""


_MARKS = {"🟢": "good", "🟡": "unstable", "🔴": "weak", "⚪": "none"}
_MARK = re.compile("(" + "|".join(_MARKS) + ") ?")


def _rich(text: str) -> str:
    """转义后,把测量工具话里的 🟢🟡🔴⚪ 换成同一套画出来的圆点(字还在后面,颜色不是唯一的线索)。"""
    return _MARK.sub(lambda m: f'<span class="mark mark-{_MARKS[m.group(1)]}" aria-hidden="true"></span>', escape(text))


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
        return f"<p{attr}>{_rich(block.text)}</p>"
    if block.kind == "note":
        return f'<p class="note"{attr}>{_rich(block.text)}</p>'
    if block.kind == "list":
        items = "".join(f"<li>{_rich(item)}</li>" for item in block.items)
        heading = f"<h3>{escape(block.caption)}</h3>" if block.caption else ""
        return f"{heading}{_from_note(block, lang, told)}<ul{attr}>{items}</ul>"
    if block.kind == "table":
        table_id = ids.new()
        head = "".join(f'<th scope="col">{_rich(cell)}</th>' for cell in block.head)
        body = "".join(
            "<tr>" + "".join(f'<th scope="row">{_rich(cell)}</th>' if i == 0 else f"<td>{_rich(cell)}</td>"
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
                parts.append("<ul>" + "".join(f"<li>{_rich(line)}</li>" for line in lines) + "</ul>")
        parts.append("</section>")
        return "".join(parts)
    if block.kind == "pair":
        parts = ['<div class="pair">']
        for side in block.sides:
            side_id = ids.new()
            units = side.units or [""] * len(side.head)
            head = "".join(f'<th scope="col">{_rich(cell)}'
                           + (f'<span class="unit">{escape(unit)}</span>' if unit else "") + "</th>"
                           for cell, unit in zip(side.head, units, strict=True))
            body = "".join(
                "<tr>" + "".join(f'<th scope="row">{_rich(cell)}</th>' if i == 0 else f"<td>{_rich(cell)}</td>"
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
        '<meta name="color-scheme" content="light">\n'
        f"<title>{escape(report.title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f'<header><h1>{escape(report.title)}</h1><p class="sub">{escape(report.subtitle)}</p>{warning}</header>\n'
        f"{nav}<main>{chapters}</main>\n</body>\n</html>\n"
    )
