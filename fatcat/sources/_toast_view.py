"""在 Toast 自己的解释器里跑(当前目录 = Toast 的文件夹),不是给本项目的解释器导入用的。

把库里的任务读出来,用 Toast 自己的函数把结论组成中英两套文字,打印成一行 JSON。结论怎么说全是 Toast 的原话,
这里不加判断、不改措辞。

只读:调用方用 PGOPTIONS 把连接设成只读;这里也只用查询,不调用任何会写库的函数(尤其不碰 restore_registry)。

用法:python -B _toast_view.py list
      python -B _toast_view.py view <job_id>
"""

from __future__ import annotations

import json
import sys
from typing import Any

LANGS = ("en", "zh")


def _fail(text: str) -> None:
    print("FATCAT_ERROR " + text)
    raise SystemExit(3)


def _jobs(storage: Any) -> list[dict[str, Any]]:
    rows = []
    for snap in storage.load_job_snapshots():
        report = storage.latest_report_snapshot(snap["job_id"])
        rows.append({
            "job_id": snap["job_id"], "state": snap.get("state"), "brand": snap["brand"]["name"],
            "website": snap.get("website"), "category": snap.get("category"), "locale": snap.get("locale"),
            "created_at": snap.get("created_at"),
            "report_versions": report["versions"] if report else 0,
            "report_at": report["created_at"] if report else None,
            # 再测要用的三项:它是哪一单的再测、花了多少、上限多少(再测沿用上一单的上限)
            "parent": snap.get("parent_job_id"), "budget_usd": snap.get("budget_usd"),
            "spent_usd": report["spent_usd"] if report else None,
        })
    return rows


def _view(storage: Any, job_id: str) -> dict[str, Any]:
    from diagnosis.messages import msg, render_msg
    from engine.loader import build_adapters, load_surface_registry
    from pydantic import ValidationError
    from report.i18n import INTENT_LABELS, MESSAGES, STATUS_LABELS
    from report.jobs import DiagnosisJob, ReportMeta
    from report.narrative import (
        action_card_lines,
        action_text,
        api_line_text,
        same_ai_reminder,
        sampling_lines,
        sentiment_items_text,
        wrap_up_text,
    )
    from report.summary import summarize
    from report.web import target_namer

    snap = next((s for s in storage.load_job_snapshots() if s["job_id"] == job_id), None)
    if snap is None:
        _fail(f"Toast 的库里没有 {job_id} 这一单")
    row = storage.latest_report_snapshot(job_id)
    if row is None:
        _fail(f"{job_id} 这一单还没有出过报告")
    job = DiagnosisJob.from_snapshot(snap)
    try:
        job.load_report(row["payload"])
    except ValidationError as exc:
        _fail(f"{job_id} 存下的报告是旧格式,现在的 Toast 读不动({exc.error_count()} 处);要在 Toast 里重新生成报告")
    # 和 Toast 自己装回报告时一样带上档案信息;少了它,「程序版本」那一格会印成「—」,和 Toast 的报告页对不上
    job.report_meta = ReportMeta(created_at=row["created_at"], versions=row["versions"],
                                 code_version=row["code_version"])

    # 只为拿接口名:给的是占位字符串,不是 key,不发请求
    entries = [e for e in load_surface_registry() if e.enabled]
    adapters = build_adapters({e.api_key_env: "placeholder-not-a-key" for e in entries})
    name_of = target_namer(adapters)
    api_names = {name: adapter.meta().api_name for name, adapter in adapters.items()}
    summary = summarize(job.class_reports, name_of, lambda surface: api_names.get(surface, surface))
    listed = {name: set(adapter.meta().models) for name, adapter in adapters.items()}

    def cell(c: Any, lang: str) -> dict[str, Any] | None:
        if c is None:
            return None
        return {"status": c.status.value, "status_label": STATUS_LABELS[lang][c.status.value],
                "text": render_msg(c.text, lang, "plain")}

    def finding(f: Any, lang: str) -> dict[str, Any] | None:
        if f is None:
            return None
        return {
            "status": f.status.value, "status_label": STATUS_LABELS[lang][f.status.value],
            "headline": render_msg(f.headline, lang, "plain"),
            "evidence": [render_msg(line, lang, "plain") for line in f.evidence_lines],
            "rate": {"k": f.rate.k, "n": f.rate.n} if f.rate is not None else None,
            "failed_checks": list(f.failed_checks), "hint_checks": list(f.hint_checks),
        }

    def in_lang(lang: str) -> dict[str, Any]:
        t = MESSAGES[lang]
        steps = []
        for step in summary.steps:
            intents = []
            for it in step.intents:
                intents.append({
                    "intent": it.intent.value, "label": INTENT_LABELS[lang][it.intent], "n_prompts": it.n_prompts,
                    "apis": [{
                        "api": api.api,
                        "models": [{"model": mc.model, "name": name_of(mc.surface, mc.model),
                                    "own": cell(mc.own, lang), "third": cell(mc.third, lang)} for mc in api.models],
                    } for api in it.apis],
                    "compares": [{
                        "name": display,
                        "brand": {"k": mc.brand.rate.k, "n": mc.brand.rate.n},
                        "competitors": [{"name": c.name, "k": c.rate.k, "n": c.rate.n,
                                         "relation": c.relation,
                                         "relation_label": t["compare_" + c.relation] if c.relation else None}
                                        for c in mc.competitors],
                    } for display, mc in it.compares],
                    "sentiments": [{"name": display, "total": st.total, "text": sentiment_items_text(st, lang)}
                                   for display, st in it.sentiments],
                })
            steps.append({"stage": step.stage.value,
                          "label": render_msg(msg(f"stage.{step.stage.value}"), lang, "plain"),
                          "intents": intents})
        return {
            "title": f"{job.brand.name} {t['report_title']}",
            "scope_note": t["scope_note"], "api_note": t["api_note"],
            "sampling": [text for _, text in sampling_lines(job, name_of, lang)],
            "reminder": same_ai_reminder(job, lang),
            "ai_lines": [api_line_text(line, lang) for line in summary.api_lines],
            "access": finding(summary.access, lang),
            "access_label": render_msg(msg("stage.ACCESS"), lang, "plain"),
            "steps": steps,
            # 末尾「最该先做什么」那一句不要(第四个参数给 None):长报告的一页结论里另有「先做这几件」,不说两遍
            "wrap_up": (wrap_up_text(summary.wrap_up, lang, job.sampling.tier.runs_per_prompt, None)
                        if summary.wrap_up is not None else None),
            "actions": [{
                "evidence_id": a.recommendation.evidence_id, "kind": a.recommendation.kind,
                "stage": a.recommendation.stage.value, "evidence_level": a.recommendation.evidence_level,
                "needs_experiment": a.recommendation.needs_experiment,
                "title": action_text(a.recommendation, lang),
                "lines": action_card_lines(a.recommendation, lang),
                "supporters": [{"intent": INTENT_LABELS[lang][intent], "ai": ai} for intent, ai in a.supporters],
            } for a in summary.actions],
            "intent_labels": {intent.value: label for intent, label in INTENT_LABELS[lang].items()},
        }

    sampling = job.sampling
    return {
        "job": {
            "job_id": job.job_id, "brand": job.brand.model_dump(mode="json"), "website": job.website,
            "category": job.category, "locale": job.locale, "created_at": job.created_at,
            "started_at": sampling.started_at, "finished_at": sampling.finished_at,
            "tier": sampling.tier.model_dump(mode="json"), "method_version": sampling.method_version,
            "n_prompts": sampling.n_prompts, "n_targets": sampling.n_targets,
            "n_runs": sampling.n_runs, "n_valid": sampling.n_valid,
            "spent_usd": row["spent_usd"],
            "report": {"versions": row["versions"], "code_version": row["code_version"],
                       "created_at": row["created_at"]},
        },
        "targets": [{
            "surface": target.surface, "model": target.model, "name": name_of(target.surface, target.model),
            "provider": adapters[target.surface].meta().provider if target.surface in adapters else None,
            # 这一单测的型号现在还在不在 Toast 的被测清单里(清单会跟着各家换代)
            "still_listed": target.surface in listed and target.model in listed[target.surface],
        } for target in job.targets],
        "prompts": [{"no": no, "intent": prompt.intent.value, "text": prompt.text, "locale": prompt.locale}
                    for no, prompt in enumerate(job.prompts, start=1)],
        "domains": job.domain_list.model_dump(mode="json") if job.domain_list is not None else None,
        "spellings": [s.model_dump(mode="json") for s in job.spellings],
        "stops": [d.model_dump(mode="json") for d in job.stop_details if d.truncated],
        "text": {lang: in_lang(lang) for lang in LANGS},
    }


def main(argv: list[str]) -> None:
    from engine.storage import Storage, default_dsn

    with Storage(default_dsn()) as storage:
        if argv == ["list"]:
            data: Any = _jobs(storage)
        elif len(argv) == 2 and argv[0] == "view":
            data = _view(storage, argv[1])
        else:
            _fail("用法:list | view <job_id>")
    print("FATCAT_JSON " + json.dumps(data, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
