"""标题 / H1 / H2 / meta 建议。全部确定性:不用模型,不改写措辞。

- 没给题板:只列每页现状和机械问题(缺标题、缺 H1、多个 H1、缺 meta 描述、多页同标题)。
- 给了 Potato 题板:给每道题找「对得上」的页面;建议标题直接复述题目原文。
  对得上的标准(2026-11-12 真实运行后定):题目和页面的**标题或 H1** 里要有一个**连续词组**相同
  (英文是去掉虚词后相邻的两个词,中文是连续三个字)。只看标题和 H1,因为建议改的就是标题和 H1,一页讲什么由它们说;
  H2 和 meta 描述只用来数单词重合。只有零散单词重合(best、home、choose 这类)不算:
  一个扫地机器人商品页曾因「home」「options」「available」三个词被配到「睡眠呼吸暂停筛查」那道题上,
  改成看词组后又因某个 H2 里的「options available」配上,所以词组只看标题和 H1。
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


def _stem(word: str) -> str:
    """很粗的单复数归一:vacuums → vacuum、kettles → kettle。题目和页面两边同样处理,所以只求一致。"""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _prepared(text: str) -> str:
    t = norm_text(text)
    for word in _ZH_STOP:
        t = t.replace(word, " ")
    return t


def _en_words(prepared: str) -> list[str]:
    """英文实词,按原文顺序(去掉虚词,单复数归一)。"""
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", prepared) if w not in _EN_STOP and len(w) > 1]


def tokens(text: str) -> set[str]:
    """比单词重合用的词:英文按词(去掉虚词、单复数归一),中文按相邻两字。"""
    t = _prepared(text)
    out = set(_en_words(t))
    for run in re.findall(r"[一-鿿]+", t):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def phrases(text: str) -> set[str]:
    """连续词组:英文是去掉虚词后相邻的两个词,中文是连续三个字(整段只有两个字时就取这两个字)。
    题目和页面都这样切;有一个词组相同,这道题和这页才算「对得上」。"""
    t = _prepared(text)
    words = _en_words(t)
    out = {f"{a} {b}" for a, b in zip(words, words[1:])}
    for run in re.findall(r"[一-鿿]+", t):
        if len(run) == 2:
            out.add(run)
        out.update(run[i:i + 3] for i in range(len(run) - 2))
    return out


def _page_fields(page: PageFacts) -> list[str]:
    return [page.title or "", *page.h1, *page.h2, page.meta_description or ""]


def _page_tokens(page: PageFacts) -> set[str]:
    return tokens(" ".join(_page_fields(page)))


def _page_phrases(page: PageFacts) -> set[str]:
    out: set[str] = set()
    for field in (page.title or "", *page.h1):  # 只看标题和 H1;每个字段单独切,不让标题的尾巴和 H1 的开头拼成一个词组
        out |= phrases(field)
    return out


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
        page_phr = {p.url: _page_phrases(p) for p in pages}
        # 每页分到的题:(单词重合比例, 题, 单词重合数, 题的词数, 对上的词组)
        assigned: dict[str, list[tuple[float, Question, int, int, list[str]]]] = {}
        md += ["## 题板对照", "",
               f"题板:`{board.path}`(prompt_version:{board.prompt_version or '未标'}),共 {len(board.questions)} 题。",
               "一道题和某页「对得上」,要有一个连续词组(英文相邻两个词、中文连续三个字)同时出现在题目和该页的"
               "标题或 H1 里;单词重合只作参考(数的是标题 / H1 / H2 / meta 描述),只有零散单词重合(best、home 这类)不算,不出建议。", ""]
        rows: list[str] = []
        weak: list[str] = []
        for q in board.questions:
            q_tok, q_phr = tokens(q.text), phrases(q.text)
            best_url, best_phr, best_hit = None, 0, 0
            for p in pages:
                phr, hit = len(q_phr & page_phr[p.url]), len(q_tok & page_tok[p.url])
                if (phr, hit) > (best_phr, best_hit):
                    best_url, best_phr, best_hit = p.url, phr, hit
            total = len(q_tok)
            matched = sorted(q_phr & page_phr[best_url]) if best_url else []
            if not best_hit:
                coverage = "none"
            elif best_hit == total:
                coverage = "covered"
            elif best_phr:
                coverage = "partial"
            else:
                coverage = "weak"
            label = {"none": "没有页面对得上", "covered": "已复述", "partial": "对得上", "weak": "只有零散单词重合"}[coverage]
            report["questions"].append({"id": q.id, "intent": q.intent, "text": q.text, "best_page": best_url,
                                        "overlap": f"{best_hit}/{total}", "phrase_hits": best_phr,
                                        "matched_phrases": matched, "coverage": coverage})
            if coverage in ("covered", "partial"):
                rows.append(f"| {q.id} | {_cell(q.text)} | {best_url} | {best_hit}/{total} | "
                            f"{_cell('、'.join(matched)) or '-'} | {label} |")
            elif coverage == "weak":
                weak.append(q.id)
            if best_url and coverage == "partial":
                assigned.setdefault(best_url, []).append((best_hit / total if total else 0.0, q, best_hit, total, matched))
        if rows:
            md += ["| 题号 | 题目 | 最接近的页面 | 单词重合 | 对上的词组 | 情况 |", "|---|---|---|---|---|---|", *rows, ""]
        if weak:
            ids = "、".join(weak[:12]) + (" 等" if len(weak) > 12 else "")
            md += [f"有 {len(weak)} 道题只和页面有零散单词重合,没有一个连续词组对得上,不给建议:{ids}。"
                   f"完整清单(含重合的词数)在 `{JSON_FILE}`。", ""]
        unmatched = [q for q in report["questions"] if q["coverage"] == "none"]
        if unmatched:
            ids = "、".join(q["id"] for q in unmatched[:12]) + (" 等" if len(unmatched) > 12 else "")
            md += [f"有 {len(unmatched)} 道题和本次读到的页面在字面上完全对不上(页面的标题、H1、H2 里没有题目里的词):{ids}。"
                   f"完整清单在 `{JSON_FILE}`。Butter 不自动生成新页面;要不要为它们单独写页面由你定。", ""]
        if not rows:
            missing.append(NotGenerated(
                what="按题板的标题复述建议",
                reason=f"题板 {len(board.questions)} 题里没有一题和本次读到的页面有连续词组对得上,没有给复述建议(不硬凑)。"))

        md += ["## 建议", ""]
        for p in pages:
            group = sorted(assigned.get(p.url, []), key=lambda t: -t[0])
            if not group:
                continue
            _, primary, hit, total, matched = group[0]
            core = _clean_question(primary.text)
            title = core if not brand or norm_text(brand) in norm_text(core) else f"{core} | {brand}"
            h2 = [_clean_question(q.text) for _, q, _, _, _ in group[1:1 + _MAX_H2]]
            suggestion = {
                "page_url": p.url, "based_on_question": primary.id, "matched_phrases": matched,
                "current": {"title": p.title, "h1": p.h1, "meta_description": p.meta_description},
                "proposed": {"title": title, "h1": core, "h2": h2,
                             "meta_description": description.value if description else None},
            }
            report["suggestions"].append(suggestion)
            md += [f"### {p.url}", "",
                   f"依据:题 {primary.id}「{primary.text}」,与本页现有标题 / H1 有连续词组相同"
                   f"({'、'.join(matched)}),单词重合 {hit}/{total}。", "",
                   f"- 现在的标题:{md_cell(p.title, 200) or '(无)'}", f"- 建议标题:{title}",
                   f"- 现在的 H1:{md_cell(' / '.join(p.h1), 200) or '(无)'}", f"- 建议 H1:{core}"]
            if h2:
                md.append("- 建议的 H2(同样和本页对得上的其他题,各用一个小标题):")
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
                rationale=f"让标题和 H1 直接复述题板里的题 {primary.id}(与本页有连续词组相同:{'、'.join(matched)});"
                          f"同页对得上的其他 {len(h2)} 道题各给一个 H2。题目原文照抄,没有改写。",
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
