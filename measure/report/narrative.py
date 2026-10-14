"""报告里的人话段落(S8.7,用户定 2026-10-14):各 AI 一句话(写法 1:每个型号的百分比,不用灯、不用 emoji)、
型号数数、末尾一段汇总、每个型号的一句结论(进证据区)。

只拼句,不判定:数字全部来自 report/summary.py 已算好的结构;两种语言各一套模板,文案在 report/i18n.py。
"""

from __future__ import annotations

from diagnosis.messages import msg, render_msg
from diagnosis.verdict import SurfaceVerdict
from report.i18n import INTENT_LABELS, MESSAGES
from report.summary import ApiLine, WrapUp

_STATUS_ORDER = ("PASS", "WEAK", "FAIL", "NOT_OBSERVABLE")


def _pct(share: float) -> int:
    return round(100 * share)


def api_line_text(line: ApiLine, lang: str) -> str:
    """「OpenAI Responses API:用户不提你时,gpt-5 在 40% 的回答里提到你(15 次里 6 次)、gpt-5-mini 27%(15 次里 4 次);
    用户直接问起时,三个型号都能介绍你。」"""
    t = MESSAGES[lang]
    if line.unprompted:
        items = [
            t["line_first" if i == 0 else "line_rest"].format(model=m, p=_pct(r.share), k=r.k, n=r.n)
            for i, (m, r) in enumerate(line.unprompted)
        ]
        unprompted = t["count_sep"].join(items)
    else:
        unprompted = t["line_unprompted_none"]
    if line.prompted:
        ok, total = sum(flag for _, flag in line.prompted), len(line.prompted)
        if ok == total:
            prompted = t["line_prompted_all"].format(n=total) if total > 1 else t["line_prompted_one"]
        elif ok == 0:
            prompted = t["line_prompted_none"]
        else:
            prompted = t["line_prompted_some"].format(k=ok, m=total - ok)
    else:
        prompted = t["line_prompted_untested"]
    return t["line_api"].format(api=line.api, unprompted=unprompted, prompted=prompted)


def count_models(counts: dict[str, int], n: int, lang: str) -> str:
    """「3 个型号里:1 个良好、1 个不够稳定、1 个较弱」——用词不用灯。"""
    t = MESSAGES[lang]
    items = [t["count_item"].format(k=counts[s], word=t["count_" + s]) for s in _STATUS_ORDER if counts.get(s)]
    return t["count_models"].format(n=n, items=t["count_sep"].join(items))


def wrap_up_text(w: WrapUp, lang: str, reps: int, first_action: str | None) -> str:
    """末尾一段:把上面的数字串起来说一遍,末句明写「没有任何新判断,也不打分」。"""
    t = MESSAGES[lang]
    parts = [t["wrap_tested"].format(apis=w.n_apis, models=w.n_models, prompts=w.n_prompts, reps=reps)]
    if w.prompted_ok is None:
        parts.append(t["wrap_prompted_untested"])
    else:
        parts.append(t["wrap_prompted"].format(ok=w.prompted_ok, total=w.prompted_total))
    if w.unprompted is None or w.best_unprompted is None:
        parts.append(t["wrap_unprompted_untested"])
    else:
        best, rate = w.best_unprompted
        parts.append(t["wrap_unprompted"].format(often=w.unprompted["often"], sometimes=w.unprompted["sometimes"],
                                                 rarely=w.unprompted["rarely"], best=best, p=_pct(rate.share)))
    for key, cell in (("wrap_citation_own", w.best_own_citation), ("wrap_citation_third", w.best_third_citation)):
        if cell is None:
            continue
        if cell.rate.k == 0:  # 最高的一格也是 0:直说一次都没有,别写「最高 0 次」
            parts.append(t[key + "_none"])
            continue
        parts.append(t[key].format(display=cell.display, intent=INTENT_LABELS[lang][cell.intent],
                                   k=cell.rate.k, n=cell.rate.n, p=_pct(cell.rate.share)))
    access_value = w.access.value if w.access else ""
    access_key = {"PASS": "wrap_access_PASS", "FAIL": "wrap_access_FAIL"}.get(access_value, "wrap_access_other")
    parts.append(t[access_key])
    if first_action:
        parts.append(t["wrap_action"].format(action=first_action))
    parts.append(t["wrap_note"])
    return t["wrap_join"].join(parts)


def verdict_text(verdict: SurfaceVerdict, display: str, lang: str) -> str:
    """每个型号的一句结论(L3 结论码代入显示名),S8.7 起只进证据区。"""
    text = render_msg(msg(verdict.code, ai=display), lang, "plain")
    if verdict.third_party_code:
        text += render_msg(msg(verdict.third_party_code, ai=display), lang, "plain") + MESSAGES[lang]["sentence_end"]
    if verdict.missing:
        t = MESSAGES[lang]
        missing = t["count_sep"].join(render_msg(msg(code), lang, "plain") for code in verdict.missing)
        text += f"({t['missing_pre']}{missing})"
    return text
