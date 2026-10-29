"""报告里的人话段落(S8.7,用户定 2026-10-14):各 AI 一句话(写法 1:每个型号的百分比,不用灯、不用 emoji)、
型号数数、末尾一段汇总、每个型号的一句结论(进证据区)。

只拼句,不判定:数字全部来自 report/summary.py 已算好的结构;两种语言各一套模板,文案在 report/i18n.py。
"""

from __future__ import annotations

from diagnosis.messages import msg, render_msg
from diagnosis.recommender import Recommendation
from diagnosis.verdict import SurfaceVerdict
from engine.calibration import CalibrationReport
from engine.models import SearchMode
from engine.scheduler import StopDetail
from report.errors import ErrorLibrary
from report.i18n import INTENT_LABELS, LEVEL_LABELS, MESSAGES
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
    sure = [flag for _, flag in line.prompted if flag is not None]
    unsure = len(line.prompted) - len(sure)
    if sure:
        ok, total = sum(sure), len(sure)
        if ok == total:
            prompted = t["line_prompted_all"].format(n=total) if total > 1 else t["line_prompted_one"]
        elif ok == 0:
            prompted = t["line_prompted_none"]
        else:
            prompted = t["line_prompted_some"].format(k=ok, m=total - ok)
        if unsure:
            prompted += t["line_prompted_unsure_tail"].format(n=unsure)
    elif unsure:  # 测了,只是失败 / 拒答太多:不说「没测」也不说「能 / 不能」(第二轮)
        prompted = t["line_prompted_unsure"]
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
        parts.append(t["wrap_prompted_unsure"] if w.prompted_unsure else t["wrap_prompted_untested"])
    else:
        parts.append(t["wrap_prompted"].format(ok=w.prompted_ok, total=w.prompted_total))
        if w.prompted_unsure:
            parts.append(t["wrap_prompted_unsure_tail"].format(n=w.prompted_unsure))
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


def action_text(rec: Recommendation, lang: str) -> str:
    """一条建议的一句话 + 类别标签:GEO 方法标「实验卡」,技术修复证据弱时标「建议实验验证」(硬边界 9)。"""
    t = MESSAGES[lang]
    if rec.kind == "geo_method":
        return f"{rec.plain[lang]}({t['kind_geo_method']})"
    return f"{rec.plain[lang]}({t['needs_experiment']})" if rec.needs_experiment else rec.plain[lang]


def action_card_lines(rec: Recommendation, lang: str) -> list[str]:
    """结果文件里一条建议的完整写法:实验卡 = 猜想 / 依据 / 怎么验证 / 免责句;技术修复 = 直接建议 + 出处。"""
    t = MESSAGES[lang]
    level = LEVEL_LABELS[lang][rec.evidence_level]
    sources = "; ".join(rec.sources[lang])
    if rec.kind == "geo_method":
        return [f"- [{t['kind_geo_method']}] {rec.plain[lang]}",
                f"  - {t['card_basis']}:{level} · {sources}",
                f"  - {t['col_verify']}:{rec.verify[lang]}",
                f"  - {t['card_disclaimer']} · {t['evidence_id_word']} {rec.evidence_id}"]
    tag = f"({t['needs_experiment']})" if rec.needs_experiment else ""
    return [f"- [{t['kind_technical_fix']}] {rec.plain[lang]}{tag}",
            f"  - {t['card_sources']}:{level} · {sources} · {t['evidence_id_word']} {rec.evidence_id}"]


def verdict_text(verdict: SurfaceVerdict, display: str, lang: str) -> str:
    """每个型号的一句结论(L3 结论码代入显示名),S8.7 起只进证据区。"""
    t = MESSAGES[lang]
    sentences = [render_msg(msg(verdict.code, ai=display), lang, "plain")]
    if verdict.third_party_code:
        sentences.append(render_msg(msg(verdict.third_party_code, ai=display), lang, "plain"))
    # 每句都收尾,句与句之间按语言隔开(原来中文少句号、英文少空格,两句粘在一起;第二轮)
    text = t["sentence_sep"].join(s + t["sentence_end"] for s in sentences)
    if verdict.missing:
        missing = t["count_sep"].join(render_msg(msg(code), lang, "plain") for code in verdict.missing)
        text += f"{t['sentence_sep']}({t['missing_pre']}{missing})"
    return text


def calibration_text(report: CalibrationReport, lang: str) -> str:
    """确认页「准确率对比」(S9.4,用户定):只写自动校准数出来的计数,标明非人工核对;没跑过就不叫这个函数。"""
    t = MESSAGES[lang]
    rules, fuzzy, judge = report.totals("rules"), report.totals("fuzzy"), report.totals("judge")
    parts = [t["calib_head"].format(date=report.calibrated_at.strftime("%Y-%m-%d"), n=report.n_answers),
             t["calib_pos"].format(r=rules["pos_hit"], rn=rules["pos_n"], f=fuzzy["pos_hit"], fn=fuzzy["pos_n"])]
    if judge["pos_n"]:
        parts.append(t["calib_pos_judge"].format(j=judge["pos_hit"], jn=judge["pos_n"]))
    parts.append(t["calib_neg"].format(r=rules["neg_false"], rn=rules["neg_n"], f=fuzzy["neg_false"],
                                       fn=fuzzy["neg_n"]))
    if judge["neg_n"]:
        parts.append(t["calib_neg_judge"].format(j=judge["neg_false"], jn=judge["neg_n"]))
    pair = report.pair("rules", "fuzzy")
    if pair is not None:
        parts.append(t["calib_rf"].format(d=pair.disagree, n=pair.n))
    if report.double_judge is not None and report.double_judge.n:
        parts.append(t["calib_double"].format(a=report.double_judge.agree, n=report.double_judge.n))
    parts.append(t["calib_tail"])
    return "".join(parts)


def stop_detail_text(detail: StopDetail, lang: str, errors: ErrorLibrary, ai: str) -> str:
    """一条腿提前停了怎么说(S10.2,用户定「熔断要说清原因」):
    错误库的一句话 + 失败按码分组的明细 + 最后一条平台原话 + 下一步。"""
    t = MESSAGES[lang]
    entry = errors[detail.stop_reason.value]
    if detail.search_mode is SearchMode.SEARCH_DISABLED:
        ai = ai + t["breaker_offline_leg"]
    parts = t["breaker_sep"].join(t["breaker_code_" + code].format(c=n) for code, n in detail.failure_counts.items())
    lines = [
        entry.message[lang].format(ai=ai, model=detail.model),
        t["breaker_detail"].format(n=detail.n_attempts, k=sum(detail.failure_counts.values()),
                                   parts=parts or t["breaker_no_failures"]),
    ]
    if detail.last_failure_message:
        lines.append(t["breaker_last"].format(last=detail.last_failure_message))
    lines.append(entry.next_step[lang])
    return " ".join(lines)
