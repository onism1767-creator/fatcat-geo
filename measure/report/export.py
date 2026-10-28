"""下载结果文件(S8.4,架构书 v0.4.26 §10 P3;产品文档 15.2「输出文件」):报告页一个按钮,现从库里打包成 zip。

五个文件:prompts.csv(题目)· responses.jsonl(全部回答,一行一条,带出报告时存下的判定)· citations.csv(每条引用一行,带归类)·
brand-domains.json(官网名单)· summary.md(人话汇总,按当前界面语言)。
只读:原始记录与已存的判定行从库里取(每个判定器取最高版本),不现算、不猜;汇总用报告层既有的汇总结果。
CSV 带 BOM(表格软件认 UTF-8);单元格以 = + - @ 或制表符开头时前面补一个单引号(表格软件会把它当公式算,CSV 注入);
citations.csv 的 final_url 与原链接相同时留空。responses.jsonl 不含平台原始返回(单条回答页逐条可下)。
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC
from typing import Any
from urllib.parse import quote, urlsplit

from diagnosis.messages import msg, render_msg
from engine.detectors import citation, sentiment
from engine.detectors import source_class as source_class_detector
from engine.detectors.matching import mention_sentence
from engine.models import SurfaceRun
from engine.observe import mention_detector
from engine.storage import Storage
from engine.tiers import TierChoice
from report.i18n import INTENT_LABELS, MESSAGES, STATUS_LABELS
from report.jobs import DiagnosisJob
from report.narrative import api_line_text, wrap_up_text
from report.response_view import citation_rows, latest_observation
from report.summary import ReportSummary, TargetNamer

FILES = ("prompts.csv", "responses.jsonl", "citations.csv", "brand-domains.json", "summary.md")
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")  # 表格软件会把这样开头的单元格当公式


def bundle_filename(job: DiagnosisJob) -> str:
    return f"geo-{job.job_id}.zip"


def content_disposition(job: DiagnosisJob) -> str:
    """ASCII 文件名兜底 + 带品牌名的 UTF-8 文件名(RFC 5987)。"""
    pretty = quote(f"geo-{job.brand.name}-{job.job_id}.zip", safe="")  # 品牌名里的 / 也要转义
    return f"attachment; filename=\"{bundle_filename(job)}\"; filename*=UTF-8''{pretty}"


def build_bundle(
    job: DiagnosisJob, storage: Storage, lang: str, name_of: TargetNamer, summary: ReportSummary,
    stop_texts: list[tuple[str, str]],
) -> bytes:
    responses, citation_rows = _responses_and_citations(job, storage, name_of)
    files: dict[str, str | bytes] = {
        "prompts.csv": _csv(["no", "prompt_id", "intent", "origin", "locale", "text"], [
            [i, p.prompt_id, p.intent.value, p.origin.value, p.locale, p.text]
            for i, p in enumerate(job.prompts, start=1)
        ]),
        "responses.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in responses),
        "citations.csv": _csv(
            ["run_id", "prompt_no", "run_index", "ai", "position", "url", "domain", "final_url", "source_class",
             "competitor", "reason"], citation_rows),
        "brand-domains.json": json.dumps({
            "brand": job.brand.model_dump(mode="json"),
            "list": job.domain_list.model_dump(mode="json") if job.domain_list is not None else None,
        }, ensure_ascii=False, indent=2),
        "summary.md": _summary_md(job, lang, name_of, summary, stop_texts),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.writestr(name, files[name])
    return buffer.getvalue()


def _csv(header: list[str], rows: list[list[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([[_cell(value) for value in row] for row in rows])
    return out.getvalue().encode("utf-8-sig")


def _cell(value: Any) -> Any:
    """外来文字(题目、回答、链接、标题)以公式字符开头时补一个单引号,表格软件打开就不会执行它。"""
    if isinstance(value, str) and value[:1] in _FORMULA_LEADERS:
        return "'" + value
    return value


def _responses_and_citations(
    job: DiagnosisJob, storage: Storage, name_of: TargetNamer,
) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    runs = [
        (no, run)
        for no, prompt in enumerate(job.prompts, start=1)
        for target in job.targets
        for run in storage.runs_for_prompt(prompt.prompt_id, surface=target.surface, model=target.model)
    ]
    observations = storage.observations_for_runs([run.run_id for _, run in runs])  # 判定行一次取齐,不逐条查库
    mention_name = mention_detector(job.frozen_tier().mention_method).name  # 「提到没提到」按本单档位的主判定器
    responses: list[dict[str, Any]] = []
    rows: list[list[Any]] = []
    for no, run in runs:
        ai = name_of(run.surface, run.model)
        recorded = observations.get(run.run_id, [])
        responses.append(_response_record(run, no, ai, recorded, mention_name))
        rows.extend(_citation_rows(run, no, ai, recorded))
    return responses, rows


def _response_record(
    run: SurfaceRun, prompt_no: int, ai: str, observations: list[dict[str, Any]], mention_name: str,
) -> dict[str, Any]:
    mention = latest_observation(observations, mention_name)
    cited = latest_observation(observations, citation.DETECTOR_NAME)
    tone = latest_observation(observations, sentiment.DETECTOR_NAME)
    excerpt = None
    if mention and mention.get("mentioned") and mention.get("first_position") is not None and run.answer is not None:
        excerpt = mention_sentence(run.answer.answer_text, mention["first_position"]).strip()
    trace = run.search_trace
    return {
        "run_id": run.run_id, "prompt_id": run.prompt_id, "prompt_no": prompt_no, "run_index": run.run_index,
        "ai": ai, "surface": run.surface, "model": run.model, "search_mode": run.search_mode.value,
        "locale": run.locale, "region": run.region, "outcome": run.outcome.value,
        "error": run.error.model_dump(mode="json") if run.error is not None else None,
        "timestamp": run.timestamp.astimezone(UTC).isoformat(), "latency_ms": run.latency_ms, "cost_usd": run.cost_usd,
        "answer_text": run.answer.answer_text if run.answer is not None else None,
        "citations_available": run.answer.citations_available if run.answer is not None else None,
        "citations": [c.model_dump(mode="json") for c in run.answer.citations] if run.answer is not None else [],
        "search_triggered": trace.search_triggered if isinstance(trace.search_triggered, bool) else None,
        "generated_queries": trace.generated_queries if isinstance(trace.generated_queries, list) else None,
        "retrieved_sources": ([s.model_dump(mode="json") for s in trace.retrieved_sources]
                              if isinstance(trace.retrieved_sources, list) else None),
        "judgements": {  # 出报告时存下的判定行;没有就 None(不现算)
            "mentioned": mention.get("mentioned") if mention else None,
            "matched_alias": mention.get("matched_alias") if mention else None,
            "matched_text": mention.get("matched_text") if mention else None,  # S9.2:怎么认出来的
            "match_kind": mention.get("match_kind") if mention else None,
            "edit_distance": mention.get("edit_distance") if mention else None,
            "excerpt": excerpt,
            "brand_site_cited": cited.get("cited") if cited else None,
            "cited_brand_domains": cited.get("matched_domains", []) if cited else [],
            "cited_brand_urls": cited.get("matched_urls", []) if cited else [],
            "cited_official_channels": cited.get("matched_channels", []) if cited else [],
            "unresolved_citations": cited.get("unresolved", 0) if cited else None,
            "sentiment": tone.get("sentiment") if tone else None,
            "detectors": {o["detector_name"]: o["detector_version"] for o in observations},
        },
    }


def _citation_rows(run: SurfaceRun, prompt_no: int, ai: str, observations: list[dict[str, Any]]) -> list[list[Any]]:
    attribution = latest_observation(observations, source_class_detector.DETECTOR_NAME)
    return [
        [run.run_id, prompt_no, run.run_index, ai, c.position, c.url, urlsplit(c.url).netloc, c.final_url or "",
         c.kind, c.competitor or "", c.reason or ""]
        for c in citation_rows(run, attribution)  # 与单条回答页同一套取法
    ]


def _tier_line(tier: TierChoice, t: dict[str, str]) -> str:
    """采样说明里的档位一行(S9.1):自定义写实际参数、不冠档位名,只说以哪一档为起点。"""
    key = "sampling_tier_custom" if tier.custom else "sampling_tier"
    return t[key].format(tier=t["tier_name_" + tier.name], runs=tier.runs_per_prompt,
                         method=t["method_" + tier.mention_method.value])


def _summary_md(
    job: DiagnosisJob, lang: str, name_of: TargetNamer, summary: ReportSummary, stop_texts: list[tuple[str, str]],
) -> str:
    t = MESSAGES[lang]
    plain = lambda m: render_msg(m, lang, "plain")  # noqa: E731
    lines = [f"# {job.brand.name} {t['report_title']}", "", t["scope_note"], t["api_note"], ""]
    sn = job.sampling_note()
    lines += [f"## {t['sampling_title']}", ""]
    counts = t["sampling_counts"].format(prompts=sn.n_prompts, targets=sn.n_targets, reps=sn.tier.runs_per_prompt,
                                        runs=sn.n_runs, valid=sn.n_valid)
    offline = " " + t["sampling_offline"].format(n=sn.n_offline_runs) if sn.n_offline_runs else ""
    lines.append("- " + counts + offline)
    if sn.fresh:  # S11.3:再测的单写明一律重新提问,并记对照的上一单
        lines.append("- " + t["sampling_fresh"])
        if job.parent_job_id:
            lines.append("- " + t["retest_parent_line"].format(id=job.parent_job_id))
    elif sn.n_cached:  # S11.2
        lines.append("- " + t["sampling_cached"].format(n=sn.n_cached, hours=sn.cache_hours))
    lines.append("- " + _tier_line(sn.tier, t))
    fmt = "%Y-%m-%d %H:%M"
    lines.append("- " + t["sampling_time"].format(
        start=sn.started_at.astimezone(UTC).strftime(fmt) if sn.started_at else "—",
        end=sn.finished_at.astimezone(UTC).strftime(fmt) if sn.finished_at else "—"))
    lines.append("- " + t["sampling_targets_pre"] + ";".join(name_of(x.surface, x.model) for x in sn.targets))
    if sn.judge_targets:  # S9.3:高档的 AI 裁判
        primary, backup = sn.judge_targets[0], sn.judge_targets[1]
        lines.append("- " + t["sampling_judges"].format(primary=name_of(primary.surface, primary.model),
                                                         backup=name_of(backup.surface, backup.model)))
    lines.append("- " + t["sampling_domains"].format(
        confirmed=sn.domains_confirmed, unconfirmed=sn.domains_unconfirmed,
        date=sn.domains_at.astimezone(UTC).strftime("%Y-%m-%d") if sn.domains_at else "—"))
    code = job.report_meta.code_version if job.report_meta and job.report_meta.code_version else "—"
    lines += ["- " + t["sampling_version"].format(method=sn.method_version, code=code), ""]
    if stop_texts:  # S10.2:提前停的腿
        lines += [f"## {t['breaker_title']}", ""] + [f"- {text}({code})" for code, text in stop_texts] + [""]
    lines += [f"## {t['verdicts_word']}", ""]
    for line in summary.api_lines:
        lines.append(f"- {api_line_text(line, lang)}")
    lines.append("")
    for cr in job.class_reports:
        lines += [f"## {INTENT_LABELS[lang][cr.intent]}", ""]
        for sd in cr.diagnosis.surfaces:
            stages = "; ".join(f"{plain(msg(f'stage.{f.stage.value}'))} {STATUS_LABELS[lang][f.status.value]}"
                                for f in sd.findings)
            lines.append(f"- {name_of(sd.surface, sd.model)}:{stages}")
            if sd.third_party:
                tp = "; ".join(f"{plain(f.headline)} {STATUS_LABELS[lang][f.status.value]}" for f in sd.third_party)
                lines.append(f"  - {t['tp_prefix']}{tp}")
            if sd.mention_compare is not None:
                mc = sd.mention_compare
                parts = [f"{t['compare_you']} {mc.brand.rate.k}/{mc.brand.rate.n}"] + [
                    f"{c.name} {c.rate.k}/{c.rate.n}({t['compare_' + c.relation]})"
                    for c in mc.competitors if c.relation
                ]
                lines.append(f"  - {t['compare_title']}:" + " · ".join(parts))
            if sd.sentiment is not None:
                st = sd.sentiment
                items = [(t["sentiment_positive"], st.positive), (t["sentiment_neutral"], st.neutral),
                         (t["sentiment_negative"], st.negative)]
                if st.mixed:
                    items.append((t["sentiment_mixed"], st.mixed))
                joined = t["sentiment_sep"].join(f"{label} {n}({round(100 * n / st.total)}%)" for label, n in items)
                lines.append(f"  - {t['sentiment_title']}:" + t["sentiment_line"].format(n=st.total, items=joined))
        lines.append("")
    if summary.wrap_up is not None:
        first_action = summary.actions[0].recommendation.plain[lang] if summary.actions else None
        wrap = wrap_up_text(summary.wrap_up, lang, sn.tier.runs_per_prompt, first_action)
        lines += [f"## {t['wrapup_title']}", "", wrap, ""]
    if job.spellings:  # S9.2b:AI 回答里写你的名字的写法
        lines += [f"## {t['spellings_report_title']}", "", t["spellings_report_lead"], ""]
        for sp in job.spellings:
            status = t["spellings_registered"] if sp.registered else t["spellings_unregistered"]
            lines.append(f"- {sp.text} · {sp.count} · {status}")
        lines.append("")
    if summary.actions:
        lines += [f"## {t['first_word']}", ""]
        for a in summary.actions:
            rec = a.recommendation
            if rec.kind == "geo_method":  # GEO 方法一律实验卡;技术修复只在证据弱时标实验(硬边界 9)
                tag = f"({t['kind_geo_method']})"
            else:
                tag = f"({t['needs_experiment']})" if rec.needs_experiment else ""
            lines.append(f"- {rec.plain[lang]}{tag} · {t['evidence_id_word']} {rec.evidence_id}")
        lines.append("")
    lines += [f"## {t['bundle_files_title']}", ""] + [f"- {t['bundle_file_' + name]}" for name in FILES] + [""]
    return "\n".join(lines)
