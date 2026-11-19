"""本地服务(外壳):FastAPI 路由 + Jinja2 模板。只绑定本机,零 JavaScript。设计 §10。

每个页面都有 `.json` 孪生:同一个视图对象,渲染成网页或 JSON(设计 §10.1)。
这一版是骨架:看运行、看改动包、下载、出题与勾选、看批次与卡片、看证据表、看设置。
起新运行和三个按钮要走后台作业,在下一步接上(设计 §10.2)。

安全(设计 §10.3):Host 只认 127.0.0.1 和 localhost;会改东西的 POST 要带本次启动的随机令牌,
并且来源必须是本机;模板自动转义,抓来的文字只当文字显示;下载文件名做清洗;密钥只显示"有 / 没有"。
"""

from __future__ import annotations

import io
import json
import re
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

import butter
from butter import cards, sources, store
from butter.config import load_defaults, load_pricing
from butter.contracts import AuditReport, ChangePackage, FactBase, PotatoSettings
from butter.drafting import draft_offer
from butter.factedit import changed_edits
from butter.jobs import JobManager
from butter.llm.keys import available_providers
from butter.measure import driver
from butter.questions import categories_from_factbase, draft_questions, guess_lang, match_by_category, to_board_questions
from butter.registry import load_registry
from butter.web import views

TEMPLATES = Path(__file__).resolve().parent / "templates"
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,80}$")
MAX_CATEGORIES = 5


@dataclass(frozen=True)
class WebSettings:
    out_root: Path                 # 运行产出(每次运行一个文件夹)
    workspace: Path                # 卡片库、题板、提案
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # 每次启动一个新令牌
    port: int = 8700
    env: dict[str, str] = field(default_factory=dict)   # 环境变量(含 .env 里读到的 key);只交给 llm,不显示
    jobs: JobManager | None = None                      # 测试可以塞一个不起线程的
    potato: PotatoSettings | None = None                # 不给就用同级文件夹里的 Potato
    spawn: Any = None                                   # 起测量驱动的动作;测试可以换成当场跑完的


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
    return env


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def create_app(settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Butter", docs_url=None, redoc_url=None, openapi_url=None)
    env = _env()
    jobs = settings.jobs or JobManager(settings.out_root, settings.workspace, env=settings.env)
    potato = settings.potato or driver.default_potato()
    spawn = settings.spawn or driver.spawn

    # ── 安全 ──────────────────────────────────────────────────────────

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _host_only(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return Response("只接受本机访问。", status_code=400, media_type="text/plain; charset=utf-8")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if not origin or (urlsplit(origin).hostname or "") not in ALLOWED_HOSTS:
                return Response("拒绝来自别的网站的请求。", status_code=403, media_type="text/plain; charset=utf-8")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
        return response

    def check_token(token: str) -> None:
        if not secrets.compare_digest(token or "", settings.token):
            raise HTTPException(403, "这个页面打开太久了,服务已经重启过。请回到页面刷新后再提交。")

    # ── 渲染:同一个视图对象 → 网页或 JSON ─────────────────────────────

    def render(request: Request, template: str, view: dict[str, Any], status: int = 200) -> Response:
        if request.url.path.endswith(".json"):
            return JSONResponse(view, status_code=status)
        html = env.get_template(template).render(v=view, token=settings.token, version=butter.__version__,
                                                 path=request.url.path)
        return HTMLResponse(html, status_code=status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return render(request, "error.html", {"title": "出了点问题", "message": str(exc.detail), "status": exc.status_code},
                      status=exc.status_code)

    # ── 读数据(只读) ──────────────────────────────────────────────────

    def run_index() -> list[dict]:
        path = settings.out_root / "runs.jsonl"
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # 容忍最后一行是半行
            if isinstance(row, dict) and row.get("run_id"):
                rows.append(row)
        return rows

    def run_dir(run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise HTTPException(404, "没有这次运行。")
        root = settings.out_root.resolve()
        for row in run_index():
            if row["run_id"] == run_id:
                path = Path(row.get("run_dir", "")).resolve()
                if path.is_dir() and root in path.parents:  # 只认产出目录里面的
                    return path
        raise HTTPException(404, "没有这次运行,或者它的文件夹已经不在了。")

    def load_run(run_id: str) -> dict[str, Any]:
        path = run_dir(run_id)
        try:
            return {
                "dir": path,
                "meta": json.loads((path / "run.json").read_text(encoding="utf-8")),
                "factbase": FactBase.model_validate_json((path / "factbase.json").read_text(encoding="utf-8")),
                "audit": AuditReport.model_validate_json((path / "audit.json").read_text(encoding="utf-8")),
                "package": ChangePackage.model_validate_json(
                    (path / "change-package" / "manifest.json").read_text(encoding="utf-8")),
            }
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"这次运行的文件读不了:{type(exc).__name__}") from exc

    def cards_view():
        view, problems = cards.load(settings.workspace, as_of=datetime.now(UTC).date())
        return view, problems

    # ── 页面 ──────────────────────────────────────────────────────────

    @app.get("/")
    @app.get("/index.json")
    def home(request: Request):
        view, _ = cards_view()
        return render(request, "home.html", views.home(run_index(), view, draft_offer(settings.env),
                                                        [j for j in jobs.jobs().values() if j.active]))

    @app.post("/runs")
    def new_run(token: str = Form(""), url: str = Form(""), max_pages: int = Form(10), draft: str = Form("")):
        check_token(token)
        try:
            job = jobs.submit_run(url, max_pages=max_pages, draft=draft == "on")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id.removesuffix(".json"))
        if job is None:
            raise HTTPException(404, "没有这个任务。")
        return render(request, "job.html", views.job(job))

    @app.get("/runs/{run_id}")
    def run_detail(request: Request, run_id: str):
        run = load_run(run_id.removesuffix(".json"))
        view, _ = cards_view()
        return render(request, "run.html", views.run_detail(run, load_registry(), view))

    @app.get("/runs/{run_id}/package.zip")
    def package_zip(run_id: str):
        root = run_dir(run_id) / "change-package"
        if not root.is_dir():
            raise HTTPException(404, "这次运行没有改动包。")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(p for p in root.rglob("*") if p.is_file()):
                archive.write(file, file.relative_to(root).as_posix())
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"butter-change-package-{run_id}.zip")
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def question_inputs(run: dict, lang: str, categories: str, brand: str | None) -> dict[str, Any]:
        fb: FactBase = run["factbase"]
        from_facts = categories_from_factbase(fb)
        typed = [c.strip() for c in re.split(r"[\n,,;;、]+", categories) if c.strip()][:MAX_CATEGORIES]
        default_brand = fb.entity.name.values[0].value if fb.entity.name.values else ""
        return {"lang": lang if lang in ("en", "zh") else guess_lang(fb), "from_facts": from_facts,
                "categories": typed, "brand": default_brand if brand is None else brand.strip()[:80]}

    @app.get("/runs/{run_id}/questions.json")
    @app.get("/runs/{run_id}/questions")
    def questions_page(request: Request, run_id: str, lang: str = "", categories: str = "", brand: str | None = None,
                       saved: str = ""):
        run = load_run(run_id)
        inputs = question_inputs(run, lang, categories, brand)
        generated = (draft_questions(sources.load_question_templates(), lang=inputs["lang"],
                                     categories=inputs["categories"], brand=inputs["brand"])
                     if inputs["categories"] else [])
        matches = match_by_category(run["factbase"].pages, generated)
        return render(request, "questions.html",
                      views.questions(run, inputs, generated, matches, panels(run_id), saved))

    def panels(run_id: str) -> list[dict]:
        root = settings.workspace / "panels" / run_id
        out = []
        for path in sorted(root.glob("*/panel.json"), reverse=True) if root.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"saved_at": data.get("saved_at", ""), "lang": data.get("lang", ""),
                        "count": len(data.get("questions", [])), "categories": data.get("categories", []),
                        "folder": path.parent.name})
        return out

    @app.post("/runs/{run_id}/questions")
    async def save_questions(request: Request, run_id: str, token: str = Form(""), lang: str = Form(""),
                             categories: str = Form(""), brand: str = Form("")):
        check_token(token)
        run = load_run(run_id)
        inputs = question_inputs(run, lang, categories, brand)
        if not inputs["categories"]:
            raise HTTPException(400, "还没有填品类。")
        form = await request.form()
        generated = draft_questions(sources.load_question_templates(), lang=inputs["lang"],
                                    categories=inputs["categories"], brand=inputs["brand"])
        picked = {q.id for q in generated if form.get(f"pick-{q.id}") == "on"}
        edits = {q.id: str(form.get(f"text-{q.id}") or "")[:300] for q in generated}
        board = to_board_questions(generated, edits=edits, picked=picked)
        if not board:
            raise HTTPException(400, "一道题都没有勾。至少勾一道再保存。")
        original = {q.id: q.text for q in generated}
        now = datetime.now(UTC)
        record = {
            "schema_version": "panel/1", "run_id": run_id, "saved_at": now.isoformat(timespec="seconds"),
            "source": "butter_generated", "template_version": generated[0].template_version, "lang": inputs["lang"],
            "categories": inputs["categories"], "brand": inputs["brand"],
            "questions": [{**q, "edited": q["text"] != original[q["id"]]} for q in board],
        }
        folder = now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        store.save_text_files(settings.workspace / "panels" / run_id / folder,
                              {"panel.json": json.dumps(record, ensure_ascii=False, indent=2) + "\n"})
        return RedirectResponse(f"/runs/{run_id}/questions?saved={folder}", status_code=303)

    @app.get("/runs/{run_id}/facts.json")
    @app.get("/runs/{run_id}/facts")
    def facts_page(request: Request, run_id: str):
        return render(request, "facts.html", views.facts(load_run(run_id)))

    @app.post("/runs/{run_id}/facts")
    async def save_facts(request: Request, run_id: str, token: str = Form(""), note: str = Form("")):
        check_token(token)
        run = load_run(run_id)
        form = await request.form()
        submitted = {key[5:]: str(value) for key, value in form.items() if key.startswith("fact:")}
        edits = changed_edits(run["factbase"], submitted, note=note,
                              at=datetime.now(UTC).isoformat(timespec="seconds"))
        if not edits:
            raise HTTPException(400, "你什么都没改。改了至少一处再保存。")
        store.append_fact_edits(settings.workspace, edits)
        job = jobs.submit_recompute(run_id)
        return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

    @app.get("/batches.json")
    @app.get("/batches")
    def batches(request: Request):
        view, problems = cards_view()
        return render(request, "batches.html", views.batches(view, problems))

    @app.get("/batches/{batch_id}")
    def batch_detail(request: Request, batch_id: str):
        batch_id = batch_id.removesuffix(".json")
        view, _ = cards_view()
        state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
        if state is None:
            raise HTTPException(404, "没有这一批改动。")
        plan = plan_error = None
        if state.phase in views.CAN_MEASURE:
            try:
                plan = driver.prepare(settings.workspace, settings.out_root, batch_id, env=settings.env, potato=potato)
            except (ValueError, OSError) as exc:
                plan_error = str(exc)
        _, legs, alive = driver.current_progress(settings.workspace, batch_id)
        return render(request, "batch.html", views.batch_detail(state, view, load_defaults(), plan=plan,
                                                                 plan_error=plan_error, legs=legs, alive=alive))

    @app.post("/batches/{batch_id}/baseline")
    def start_baseline(batch_id: str, token: str = Form("")):
        check_token(token)
        try:
            plan = driver.prepare(settings.workspace, settings.out_root, batch_id, env=settings.env, potato=potato)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        reason = driver.start(settings.workspace, plan)
        if reason:
            raise HTTPException(400, reason)
        spawn(settings.workspace, settings.out_root, batch_id, "baseline", env=settings.env, potato=potato)
        driver.wait_started(settings.workspace, batch_id)
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)

    @app.post("/batches/{batch_id}/resume")
    def resume_measure(batch_id: str, token: str = Form("")):
        check_token(token)
        view, _ = cards_view()
        state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
        if state is None or state.phase not in ("测基线中", "再测中"):
            raise HTTPException(400, "这一批现在没有测到一半的测量。")
        _, _, alive = driver.current_progress(settings.workspace, batch_id)
        if not alive:
            spawn(settings.workspace, settings.out_root, batch_id, "baseline" if state.phase == "测基线中" else "remeasure",
                  env=settings.env, potato=potato)
            driver.wait_started(settings.workspace, batch_id)
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)

    @app.get("/registry.json")
    @app.get("/registry")
    def registry(request: Request):
        view, _ = cards_view()
        return render(request, "registry.html", views.registry(load_registry(), view))

    @app.get("/settings.json")
    @app.get("/settings")
    def settings_page(request: Request):
        return render(request, "settings.html", views.settings(
            settings.out_root, settings.workspace, available_providers(settings.env), load_defaults(), load_pricing(),
            load_registry()))

    return app
