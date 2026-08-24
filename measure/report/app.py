"""L4 Web 层(第 16 章):输入 → 确认 → 任务 → 报告,FastAPI + Jinja2 服务端渲染。

v1 单租户自用:无登录,仅本机访问。确认页是采样花钱前的唯一闸门。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from diagnosis.recommender import EvidenceEntry, load_evidence_registry
from engine.envfile import load_env_file
from engine.loader import build_adapters
from engine.models import BrandSpec, InformationNeed, NeedIntent
from engine.needs import NeedCandidate, draft_need_candidates
from report import runner
from report.jobs import (
    AVG_COST_PER_CALL_USD,
    AVG_SECONDS_PER_CALL,
    PLAN_CONCURRENCY,
    TIERS,
    DiagnosisJob,
    JobRegistry,
    JobState,
    estimate_job,
)

DEFAULT_DSN = "postgresql://geo@localhost:5433/geo_tool"
INTENT_LABELS = {
    NeedIntent.DISCOVERY: "发现",
    NeedIntent.COMPARISON: "比较",
    NeedIntent.SCENARIO: "场景",
    NeedIntent.BRAND_PROBE: "品牌探针",
}


def create_app(
    *,
    adapters: dict | None = None,
    dsn: str | None = None,
    evidence: list[EvidenceEntry] | None = None,
    job_runner=None,
) -> FastAPI:
    if adapters is None:
        load_env_file()
        adapters = build_adapters(os.environ)
    dsn = dsn or os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)
    evidence = evidence if evidence is not None else load_evidence_registry()
    job_runner = job_runner or runner.execute_job

    app = FastAPI(title="GEO 诊断")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    registry = JobRegistry()
    app.state.registry = registry
    app.state.adapters = adapters

    def render(request: Request, name: str, **context) -> HTMLResponse:
        context.setdefault("intent_labels", INTENT_LABELS)
        return templates.TemplateResponse(request, name, context)

    def not_found(request: Request) -> HTMLResponse:
        return HTMLResponse("任务不存在", status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, error: str = ""):
        return render(request, "index.html", jobs=registry.recent(),
                      surfaces=sorted(adapters), error=error)

    @app.post("/jobs")
    async def create_job(
        request: Request,
        brand_name: str = Form(...),
        aliases: str = Form(""),
        website: str = Form(...),
        category: str = Form(...),
        locale: str = Form("zh-CN"),
        questions: str = Form(""),
        generate: str = Form(""),
    ):
        locale = locale if locale in ("zh-CN", "en-US") else "zh-CN"
        normalized = website if "://" in website else f"https://{website}"
        domain = urlsplit(normalized).hostname or ""
        brand = BrandSpec(
            name=brand_name.strip(),
            aliases=[a.strip() for a in aliases.replace(",", ",").split(",") if a.strip()],
            domains=[domain] if domain else [],
        )
        candidates: list[NeedCandidate] = []
        competitors_suggested = []
        if generate and adapters:
            generation_adapter = next(
                (a for a in adapters.values() if a.meta().capabilities.supports_citations), None
            )
            if generation_adapter is not None:
                candidates, competitors_suggested = draft_need_candidates(
                    generation_adapter, category=category, brand=brand, locale=locale
                )
        manual = [line.strip() for line in questions.splitlines() if line.strip()]
        candidates = candidates + [
            NeedCandidate(intent=NeedIntent.DISCOVERY, text=text) for text in manual
        ]
        if not candidates:
            return render(request, "index.html", jobs=registry.recent(),
                          surfaces=sorted(adapters),
                          error="请至少手填一个目标问题,或勾选「帮我生成」")
        job = DiagnosisJob(
            brand=brand, website=normalized, category=category, locale=locale,
            candidates=candidates, competitors_suggested=competitors_suggested,
        )
        registry.add(job)
        return RedirectResponse(f"/jobs/{job.job_id}/confirm", status_code=303)

    @app.get("/jobs/{job_id}/confirm", response_class=HTMLResponse)
    async def confirm_page(request: Request, job_id: str, error: str = ""):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if job.state is not JobState.DRAFT:
            return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)
        estimate_constants = json.dumps({
            "tier_calls": {tid: t.expected_calls_per_plan for tid, t in TIERS.items()},
            "cost_per_call": AVG_COST_PER_CALL_USD,
            "seconds_per_call": AVG_SECONDS_PER_CALL,
            "concurrency": PLAN_CONCURRENCY,
        })
        return render(request, "confirm.html", job=job, tiers=TIERS,
                      surfaces=sorted(adapters), estimate_constants=estimate_constants,
                      error=error)

    @app.post("/jobs/{job_id}/confirm")
    async def confirm(
        request: Request,
        job_id: str,
        needs_selected: list[int] = Form([]),
        custom: str = Form(""),
        competitors_selected: list[int] = Form([]),
        surfaces_selected: list[str] = Form([]),
        tier: str = Form("quick"),
        variants: int = Form(2),
        budget: str = Form(""),
    ):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if job.state is not JobState.DRAFT:
            return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

        needs = [
            InformationNeed(statement=job.candidates[i].text, intent=job.candidates[i].intent)
            for i in needs_selected
            if 0 <= i < len(job.candidates)
        ] + [
            InformationNeed(statement=line.strip(), intent=NeedIntent.DISCOVERY)
            for line in custom.splitlines()
            if line.strip()
        ]

        def back(error: str):
            return RedirectResponse(
                f"/jobs/{job.job_id}/confirm?error={error}", status_code=303
            )

        if not 1 <= len(needs) <= 10:
            return back("需求数量须在 1–10 个之间(架构口径建议 3–10)")
        surfaces = [s for s in surfaces_selected if s in adapters]
        if not surfaces:
            return back("至少选择一个 surface")
        if tier not in TIERS or not 1 <= variants <= 3:
            return back("参数非法")
        budget_usd = float(budget) if budget.strip() else None

        competitors = [
            BrandSpec(name=c.name, domains=[c.domain])
            for i in competitors_selected
            if 0 <= i < len(job.competitors_suggested)
            for c in [job.competitors_suggested[i]]
        ]
        job.freeze(needs=needs, competitors=competitors, surfaces=surfaces,
                   tier_id=tier, variants_per_need=variants, budget_usd=budget_usd)
        threading.Thread(
            target=job_runner,
            kwargs=dict(job=job, adapters=adapters, dsn=dsn, evidence=evidence),
            daemon=True,
        ).start()
        return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_status(request: Request, job_id: str):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        return render(request, "job.html", job=job)

    @app.get("/jobs/{job_id}/report", response_class=HTMLResponse)
    async def job_report(request: Request, job_id: str):
        job = registry.get(job_id)
        if job is None:
            return not_found(request)
        if not job.finished:
            return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)
        return render(request, "report.html", job=job, tier_label=TIERS[job.tier_id].label)

    return app
