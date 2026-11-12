"""标题 / H1 / H2 / meta 建议。全部确定性:不用模型,不改写措辞。

- 没给题板:只列每页现状和机械问题(缺标题、缺 H1、多个 H1、缺 meta 描述、多页同标题)。
- 给了 Potato 题板:按字面重合度给每道题找候选目标页;建议标题直接复述题目原文。
这是 GEO 方法类建议(标题复述查询),验证态一律「假设」:只在该页确实回答这道题时采用,不堆砌关键词。
"""

from __future__ import annotations

import json
import re

from butter.contracts import ChangeItem, FactBase, FactRef, NotGenerated, PageFacts, Question, QuestionBoard
from butter.registry_ids import STRUCT, TITLE_QUERY_MATCH
from butter.util import md_cell, norm_text

MD_FILE = "suggestions/titles.md"
JSON_FILE = "suggestions/titles.json"
_EN_STOP = frozenset("""the a an of for to in on and or is are was be do does did what which who how why when where i my me we
you your should can could would will it its that this these those with at by from as there any some most more than vs
versus right now about get has have""".split())
_ZH_STOP = sorted(["什么", "哪些", "哪个", "哪家", "哪款", "哪种", "怎么", "怎样", "如何", "是否", "有没有", "请问", "可以",
                   "应该", "一下", "值得", "吗", "呢", "的", "了", "和", "与", "及", "或", "是", "有", "在", "我", "你"],
                  key=len, reverse=True)
_MAX_H2 = 5


def tokens(text: str) -> set[str]:
    """比字面重合用的词:英文按词(去掉虚词),中文按相邻两字。"""
    t = norm_text(text)
    for word in _ZH_STOP:
        t = t.replace(word, " ")
    out = {w for w in re.findall(r"[a-z0-9]+", t) if w not in _EN_STOP and len(w) > 1}
    for run in re.findall(r"[一-鿿]+", t):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _page_tokens(page: PageFacts) -> set[str]:
    return tokens(" ".join([page.title or "", *page.h1, *page.h2, page.meta_description or ""]))


def _clean_question(text: str) -> str:
    return text.strip().rstrip("??。.  ")


def mechanical_issues(pages: list[PageFacts], brand: str | None) -> dict[str, list[str]]:
    by_title: dict[str, list[str]] = {}
    for page in pages:
        if page.title:
            by_title.setdefault(norm_text(page.title), []).append(page.url)
    issues: dict[str, list[str]] = {}
    for page in pages:
        found: list[str] = []
        if not page.title:
            found.append("缺 <title>")
        elif len(by_title[norm_text(page.title)]) > 1:
            others = [u for u in by_title[norm_text(page.title)] if u != page.url]
            found.append(f"与另外 {len(others)} 个页面标题相同({others[0]})")
        if not page.h1:
            found.append("缺 H1")
        elif len(page.h1) > 1:
            found.append(f"有 {len(page.h1)} 个 H1")
        if not page.meta_description:
            found.append("缺 meta description")
        if brand and page.title and norm_text(brand) not in norm_text(page.title):
            found.append(f"标题里没有出现名称「{brand}」")
        issues[page.url] = found
    return issues


def build_headings(
    fb: FactBase, board: QuestionBoard | None,
) -> tuple[dict[str, str], list[ChangeItem], list[NotGenerated]]:
    pages = fb.pages
    brand = fb.entity.name.values[0].value if fb.entity.name.values else None
    description = fb.entity.description.values[0] if fb.entity.description.values else None
    issues = mechanical_issues(pages, brand)
    items: list[ChangeItem] = []
    missing: list[NotGenerated] = []
    if not pages:
        return {}, [], [NotGenerated(what="标题 / H1 / H2 / meta 建议", reason="没有抓到可分析的页面。")]

    report: dict = {
        "mode": "with_board" if board else "status_only",
        "board": {"path": board.path, "prompt_version": board.prompt_version} if board else None,
        "pages": [{"url": p.url, "title": p.title, "h1": p.h1, "h2": p.h2, "meta_description": p.meta_description,
                   "issues": issues[p.url]} for p in pages],
        "questions": [], "suggestions": [],
    }
    md: list[str] = ["# 标题 / H1 / H2 / meta 建议", "",
                     "> 验证态:**假设**。只在该页确实回答对应问题时采用;标题必须与页面真实内容一致,不要堆砌关键词。",
                     "> 全部由规则生成,没有用模型,没有改写措辞。", ""]

    md += ["## 各页现状", "", "| 页面 | 标题 | H1 | meta description | 机械问题 |", "|---|---|---|---|---|"]
    for p in pages:
        md.append("| {} | {} | {} | {} | {} |".format(
            p.url, _cell(p.title), _cell(" / ".join(p.h1)), _cell(p.meta_description),
            _cell(";".join(issues[p.url])) or "未见"))
    md.append("")

    if any(issues.values()):
        flagged = [p for p in pages if issues[p.url]]
        items.append(ChangeItem(
            item_id="headings-status", kind="heading_suggestion", entry_id=STRUCT, action="review",
            target=f"{len(flagged)} 个页面的 <title> / H1 / meta description", files=[MD_FILE, JSON_FILE],
            rationale="列出各页标题、H1、meta description 的现状和机械问题(缺失、重复、多个 H1)。只陈述现状,怎么改由你定。",
            validation_state="假设",
            needs_human=["「标题里没有出现名称」只是陈述事实,不是所有页面都需要带名称。"]))

    if board is None:
        missing.append(NotGenerated(what="按题板的标题复述建议",
                                    reason="没有给 Potato 的 questions.yml,只列了各页现状。加 --questions 再跑即可。"))
    else:
        page_tok = {p.url: _page_tokens(p) for p in pages}
        assigned: dict[str, list[tuple[float, Question, int, int]]] = {}
        md += ["## 题板对照", "", f"题板:`{board.path}`(prompt_version:{board.prompt_version or '未标'}),共 {len(board.questions)} 题。",
               ""]
        rows: list[str] = []
        for q in board.questions:
            q_tok = tokens(q.text)
            best_url, best_hit = None, 0
            for p in pages:
                hit = len(q_tok & page_tok[p.url])
                if hit > best_hit:
                    best_url, best_hit = p.url, hit
            total = len(q_tok)
            coverage = "none" if not best_hit else ("covered" if best_hit == total else "partial")
            label = {"none": "没有页面对得上", "covered": "已复述", "partial": "部分重合"}[coverage]
            report["questions"].append({"id": q.id, "intent": q.intent, "text": q.text, "best_page": best_url,
                                        "overlap": f"{best_hit}/{total}", "coverage": coverage})
            if best_url:  # 对不上的题不逐行列(题板常有几十题,大多和这个站无关),下面合成一句
                rows.append(f"| {q.id} | {_cell(q.text)} | {best_url} | {best_hit}/{total} | {label} |")
            if best_url and coverage == "partial":
                assigned.setdefault(best_url, []).append((best_hit / total if total else 0.0, q, best_hit, total))
        if rows:
            md += ["| 题号 | 题目 | 最接近的页面 | 字面重合 | 情况 |", "|---|---|---|---|---|", *rows, ""]
        unmatched = [q for q in report["questions"] if q["coverage"] == "none"]
        if unmatched:
            ids = "、".join(q["id"] for q in unmatched[:12]) + (" 等" if len(unmatched) > 12 else "")
            md += [f"有 {len(unmatched)} 道题和本次读到的页面在字面上完全对不上(页面的标题、H1、H2 里没有题目里的词):{ids}。"
                   f"完整清单在 `{JSON_FILE}`。Butter 不自动生成新页面;要不要为它们单独写页面由你定。", ""]
        if not rows:
            missing.append(NotGenerated(
                what="按题板的标题复述建议",
                reason=f"题板 {len(board.questions)} 题里没有一题和本次读到的页面在字面上对得上,没有给复述建议(不硬凑)。"))

        md += ["## 建议", ""]
        for p in pages:
            group = sorted(assigned.get(p.url, []), key=lambda t: -t[0])
            if not group:
                continue
            _, primary, hit, total = group[0]
            core = _clean_question(primary.text)
            title = core if not brand or norm_text(brand) in norm_text(core) else f"{core} | {brand}"
            h2 = [_clean_question(q.text) for _, q, _, _ in group[1:1 + _MAX_H2]]
            suggestion = {
                "page_url": p.url, "based_on_question": primary.id,
                "current": {"title": p.title, "h1": p.h1, "meta_description": p.meta_description},
                "proposed": {"title": title, "h1": core, "h2": h2,
                             "meta_description": description.value if description else None},
            }
            report["suggestions"].append(suggestion)
            md += [f"### {p.url}", "",
                   f"依据:题 {primary.id}「{primary.text}」,与本页现有标题 / 小标题字面重合 {hit}/{total}。", "",
                   f"- 现在的标题:{md_cell(p.title, 200) or '(无)'}", f"- 建议标题:{title}",
                   f"- 现在的 H1:{md_cell(' / '.join(p.h1), 200) or '(无)'}", f"- 建议 H1:{core}"]
            if h2:
                md.append("- 建议的 H2(同样落在本页的其他题,各用一个小标题):")
                md += [f"  - {text}" for text in h2]
            md.append(f"- 建议 meta description:{description.value}(来自事实库的描述,原样)" if description
                      else "- meta description:事实库里没有描述,没有给建议(不编)。")
            md.append("")
            refs = [FactRef(field="entity.description", value=description.value, source_url=description.source_url)] if description else []
            if brand:
                refs.append(FactRef(field="entity.name", value=brand, source_url=fb.entity.name.values[0].source_url))
            items.append(ChangeItem(
                item_id=f"headings-{len(items) + 1}", kind="heading_suggestion", entry_id=TITLE_QUERY_MATCH,
                action="review", target=f"{p.url} 的 <title> / H1 / H2 / meta description", files=[MD_FILE, JSON_FILE],
                rationale=f"让标题和 H1 直接复述题板里的题 {primary.id};同页的其他 {len(h2)} 道题各给一个 H2。"
                          "题目原文照抄,没有改写。",
                facts_used=refs, validation_state="假设",
                needs_human=["只在这一页确实回答了这道题时采用;页面内容对不上题就不要改标题。",
                             "题目原文直接当标题可能不通顺,措辞由你调整;不要为了匹配而堆砌关键词。"]))
        if not report["suggestions"]:
            md += ["没有需要给出的复述建议(题目要么已被复述,要么没有页面对得上)。", ""]

    files = {MD_FILE: "\n".join(md).rstrip() + "\n", JSON_FILE: json.dumps(report, ensure_ascii=False, indent=2) + "\n"}
    return files, items, missing


def _cell(text: str | None) -> str:
    """Markdown 表格单元格:外来文字先中和(见 util.md_cell)。"""
    return md_cell(text, 80)
