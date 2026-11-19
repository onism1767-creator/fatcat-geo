"""后台作业(外壳):一个工作线程排队跑短作业(抓站、起草、出包、建批次与卡)。设计 §10.2、ADR-B07。

- 作业状态的每次变化追加进 `events/jobs.jsonl`;当前样子由事件推出。
- 服务重启后,日志里还停在"排队 / 进行中"的作业记成 INTERRUPTED(中断):线程已经没了,不装作还在跑。
- 测量不在这里跑:那是脱离服务的测量驱动的事(以后接)。这里没有任何定时的东西,只在站长点了按钮时才动。
- 密钥不进作业参数、不进日志。
"""

from __future__ import annotations

import queue
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from butter import cards, store
from butter.contracts import ChangePackage, Job, JobEvent, SiteSnapshot
from butter.drafting import draft_settings
from butter.fetch import FetchLimits, fetch_site
from butter.pipeline import RunOptions, RunResult, process_snapshot
from butter.questions import categories_from_factbase, guess_lang
from butter.registry import load_registry
from butter.util import normalize_site_url

Fetch = Callable[[str, int], SiteSnapshot]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fold_jobs(events: list[JobEvent]) -> dict[str, Job]:
    jobs: dict[str, Job] = {}
    for e in events:
        old = jobs.get(e.job_id)
        if old is None:
            jobs[e.job_id] = Job(job_id=e.job_id, kind=e.kind, state=e.state, progress=e.progress, created_at=e.at,
                                 updated_at=e.at, params=e.params, result=e.result, error=e.error)
        else:
            jobs[e.job_id] = old.model_copy(update={
                "state": e.state, "progress": e.progress or old.progress, "updated_at": e.at,
                "result": e.result or old.result, "error": e.error or old.error})
    return jobs


def create_batch(workspace: Path, package: ChangePackage, *, site_host: str, industry: str, lang: str) -> tuple[str, list[str]]:
    """出包之后建批次和卡:一条改动一张卡("不用改"的不建)。返回(批次号, 没记上的原因)。一张卡都没有就不建批次。"""
    batch_id = f"b-{package.run_id}"
    batch, specs = cards.plan_batch(package, load_registry(), batch_id=batch_id, site_host=site_host,
                                    industry=industry, lang=lang)
    if not specs:
        return "", []
    notes: list[str] = []
    at = datetime.now(UTC)
    decision = cards.submit(workspace, at=at, actor="system", batch_id=batch_id, type="batch_created", payload=batch)
    if not decision.ok:
        return "", [decision.reason]
    for spec in specs:
        decision = cards.submit(workspace, at=at, actor="system", batch_id=batch_id, type="card_created", payload=spec,
                                card_id=spec.card_id)
        if not decision.ok:
            notes.append(f"{spec.card_id}:{decision.reason}")
    return batch_id, notes


class JobManager:
    def __init__(self, out_root: Path, workspace: Path, *, env: Mapping[str, str], fetch: Fetch | None = None,
                 start_worker: bool = True) -> None:
        self.out_root, self.workspace, self.env = out_root, workspace, env
        self._fetch = fetch or (lambda url, max_pages: fetch_site(url, limits=FetchLimits(max_pages=max_pages)))
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._params: dict[str, dict[str, Any]] = {}
        self._mark_interrupted()
        if start_worker:
            threading.Thread(target=self._work, name="butter-jobs", daemon=True).start()

    # ── 记录 ──────────────────────────────────────────────────────────

    def _emit(self, job_id: str, state: str, progress: str = "", **extra: Any) -> None:
        with self._lock:
            store.append_job_event(self.workspace, JobEvent(job_id=job_id, at=_now(), state=state, progress=progress,
                                                           **extra))

    def jobs(self) -> dict[str, Job]:
        return fold_jobs(store.read_job_events(self.workspace))

    def get(self, job_id: str) -> Job | None:
        return self.jobs().get(job_id)

    def _mark_interrupted(self) -> None:
        for job in self.jobs().values():
            if job.active:
                self._emit(job.job_id, "INTERRUPTED", "服务重启时这个任务还没做完，已经中断。可以重新开始一次。")

    # ── 排队与执行 ────────────────────────────────────────────────────

    def submit_run(self, url: str, *, max_pages: int = 10, draft: bool = True, budget_usd: float = 1.0) -> Job:
        """排一个"读网站并生成改动包"的作业。网址不合法直接抛 ValueError,不排队。"""
        url = normalize_site_url(url)
        if not 1 <= max_pages <= 30:
            raise ValueError("最多读几页要在 1 到 30 之间。")
        job_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        params = {"url": url, "max_pages": max_pages, "draft": bool(draft), "budget_usd": budget_usd}
        self._params[job_id] = params
        self._emit(job_id, "CONFIRMED", "排队中", params=params)
        self._queue.put(job_id)
        return self.get(job_id)

    def _work(self) -> None:
        while True:
            self.run_one(self._queue.get())

    def run_next(self) -> bool:
        """测试用:在当前线程里跑完队列里的下一个作业。"""
        try:
            job_id = self._queue.get_nowait()
        except queue.Empty:
            return False
        self.run_one(job_id)
        return True

    def run_one(self, job_id: str) -> None:
        params = self._params.pop(job_id, None)
        if params is None:
            return
        try:
            self._emit(job_id, "RUNNING", "正在读你的网站（每秒最多读一页）")
            snapshot = self._fetch(params["url"], params["max_pages"])
            settings, offer = (draft_settings(self.env, self.workspace / "cache" / "llm", max_pages=params["max_pages"],
                                              budget_usd=params["budget_usd"]) if params["draft"] else (None, None))
            self._emit(job_id, "RUNNING", "正在整理事实、检查、生成改动" + ("，并让模型起草文字" if settings else ""))
            result: RunResult = process_snapshot(snapshot, RunOptions(out_root=self.out_root, draft=settings))
            notes = list(result.warnings)
            if params["draft"] and settings is None:
                notes.append("没有找到任何一家模型的 key，这次没有让模型起草；规则生成的部分照常出了。")
            categories = categories_from_factbase(result.factbase)
            batch_id, problems = create_batch(self.workspace, result.bundle.package, site_host=result.factbase.host,
                                              industry=categories[0] if categories else "",
                                              lang=guess_lang(result.factbase))
            notes += problems
            drafts = result.drafts
            outcome = {
                "run_id": result.run_id, "batch_id": batch_id, "pages": len(result.factbase.pages),
                "change_items": len(result.bundle.package.items), "notes": notes,
                "cost_usd": drafts.cost_usd if drafts else 0, "drafted": len(drafts.items) if drafts else 0,
                "draft_model": f"{offer['provider']}:{offer['model']}" if offer else "",
            }
            partial = not result.factbase.pages or bool(drafts and drafts.stopped) or bool(problems)
            self._emit(job_id, "PARTIAL" if partial else "DONE",
                       "做完了，但有几处要你留意" if partial else "做完了", result=outcome)
        except (ValueError, OSError) as exc:
            self._emit(job_id, "FAILED", "没做成", error=str(exc)[:500])
        except Exception as exc:  # 作业线程不能死:任何意外都记下来,换下一个
            self._emit(job_id, "FAILED", "没做成", error=f"{type(exc).__name__}: {str(exc)[:400]}")
