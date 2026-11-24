"""测量驱动(外壳):准备 → 冻结 → 一条腿一条腿地叫 Potato 去问 → 读回结果 → 记到批次上。设计 §10.2、ADR-B03、ADR-B07。

- Butter 自己不问 AI:问是 Potato 的事。这里只负责起 Potato 的进程、看进度、读结果。
- 每条腿(一种语言 × 一家 AI)一个 Potato 进程,依次跑。不用 Potato 的多表面模式:那样一家出错后面的就不跑了。
- 驱动是脱离网页服务的独立进程:关掉网页或服务,测量照样跑完。同一批同一时刻只有一个驱动(拿着 measure.lock)。
- 进度只追加进 `batches/<批次>/measure/<这次测量>.jsonl`;断了再起一个驱动,做完的腿跳过,没做完的腿用同一个 run_id 接着问。
- 一条腿问满若干次后失败太多就停这条腿,接着跑下一条,免得白花钱。
- 密钥只放进 Potato 进程的环境变量,不进任何文件和日志。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from butter import cards, sources, store
from butter.compare import compare_batch, plan_rules
from butter.config import load_defaults
from butter.contracts import (
    BatchFacts, BatchFreeze, BatchPlan, CompareResult, FactBase, FrozenBrands, FrozenPanel, GeneratedQuestion,
    LegProgress, LegReading, MeasureDone, MeasureFailed, MeasurePlan, MeasureReading, MeasureStart, PotatoSettings,
    Surface,
)
from butter.llm.keys import available_providers, child_process_env
from butter.measure import plan as P
from butter.questions import match_by_category

BRIDGE = Path(__file__).resolve().parent.parent / "bridges" / "potato_read.py"
PROGRESS = re.compile(r"^PROGRESS probe (\d+)/(\d+) ([0-9.]+)")
EXIT_NOTE = {1: "Potato 中途出错停了", 2: "Potato 不认这份配置,或者预算上限拦住了", 3: "Potato 还不支持这个做法",
             4: "这家 AI 的 key 没有,或者被拒了"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,120}$")
LOCK_WAIT_S = 2.0


def default_potato() -> PotatoSettings:
    """默认:和本项目同级的 Potato 文件夹,用它自己的解释器。"""
    root = Path(__file__).resolve().parents[4] / "AI checker"
    return PotatoSettings(potato_dir=str(root), python=str(root / ".venv" / "Scripts" / "python.exe"))


def batch_dir(workspace: Path, batch_id: str) -> Path:
    if not SAFE_ID.match(batch_id):
        raise ValueError(f"批次号不合法:{batch_id!r}")
    return workspace / "batches" / batch_id


def potato_env(env: Mapping[str, str], potato: PotatoSettings) -> dict[str, str]:
    """Potato 进程的环境:系统环境 + 模型的 key(变量名只有 llm/keys.py 知道),再加上编码设置。"""
    out = child_process_env(env)
    out |= {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if potato.pythonpath:
        out["PYTHONPATH"] = potato.pythonpath
    return out


def _potato(potato: PotatoSettings, args: list[str], env: dict[str, str], timeout: float = 120) -> tuple[int, str]:
    done = subprocess.run([potato.python, "-B", "-m", "aivis.cli", *args], capture_output=True, env=env,
                          cwd=potato.potato_dir, timeout=timeout)
    return done.returncode, (done.stdout + done.stderr).decode("utf-8", errors="replace")


# ── 准备:题板、分组、估费 ─────────────────────────────────────────────

def _latest_panels(workspace: Path, run_id: str) -> dict[str, dict]:
    """每种语言最近存的那份题板。"""
    out: dict[str, dict] = {}
    root = workspace / "panels" / run_id
    for path in sorted(root.glob("*/panel.json"), reverse=True) if root.is_dir() else []:
        try:
            panel = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if panel.get("lang") in P.LOCALE and panel.get("questions"):
            out.setdefault(panel["lang"], panel | {"_path": str(path)})
    return out


def _panel_questions(panel: dict) -> list[GeneratedQuestion]:
    cats = panel.get("categories") or [""]
    out = []
    for q in panel["questions"]:
        match = re.match(r"^g(\d+)-", q["id"])
        category = cats[int(match[1]) - 1] if match and int(match[1]) <= len(cats) else cats[0]
        out.append(GeneratedQuestion(id=q["id"], intent=q["intent"], text=q["text"], lang=panel["lang"],
                                     category=category, names_brand=q["intent"] == "brand_defense",
                                     template_version=str(panel.get("template_version", ""))))
    return out


def prepare(workspace: Path, out_root: Path, batch_id: str, *, env: Mapping[str, str], potato: PotatoSettings,
            ) -> MeasurePlan:
    """算出这批要怎么测、大概花多少。会把 Potato 的配置目录写好(内容不变就不重写),别的什么都不动、不花钱。

    做不了的时候抛 ValueError,话是给站长看的。
    """
    bdir = batch_dir(workspace, batch_id)
    view, _ = cards.load(workspace)
    state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
    if state is None:
        raise ValueError("没有这一批改动。")
    run_dir = store.find_run_dir(out_root, state.batch.run_id)
    if run_dir is None:
        raise ValueError("这批改动出自的那次运行,文件夹已经不在了。")
    fb = FactBase.model_validate_json((run_dir / "factbase.json").read_text(encoding="utf-8"))
    panels = _latest_panels(workspace, state.batch.run_id)
    if not panels:
        raise ValueError("还没有选好要问的题。先去「出题和勾选」存一份。")
    defaults = load_defaults()
    tiers = {(t.lang, t.provider) for t in defaults.noise_tiers()}
    known = {v.key.removeprefix("measure.model.") for v in defaults.all() if v.key.startswith("measure.model.")}
    providers = [p for p in available_providers(env) if p in known]
    if not providers:
        raise ValueError("这台电脑上没有找到 Claude、OpenAI、Gemini、Grok 任何一家的 key,没法测。")
    my_cards = [c for c in view.cards if c.batch_id == batch_id]
    changed = {page for c in my_cards if (page := P.page_of_target(c.intervention.target))}
    sitewide = any(c.intervention.kind in cards.SITEWIDE_KINDS for c in my_cards)
    runs = int(defaults.get("measure.runs_per_question"))
    cap_per_call = float(defaults.get("measure.budget_cap_per_call_usd"))
    brand = P.brand_block(fb)
    penv = potato_env(env, potato)

    question_pages: dict[str, list[str]] = {}
    brand_ids: set[str] = set()
    frozen_panels, frozen_brands, surfaces, config_dirs, estimates, n_questions, notes = [], [], [], {}, [], {}, []
    for lang, panel in sorted(panels.items()):
        generated = _panel_questions(panel)
        question_pages |= match_by_category(fb.pages, generated)
        brand_ids |= {q.id for q in generated if q.names_brand}
        cap = round(cap_per_call * len(panel["questions"]) * runs, 2)   # 每条腿最多花这么多,到了 Potato 就停
        files = P.potato_config(panel["questions"], brand, lang=lang, runs=runs, budget_cap_usd=cap)
        digest = hashlib.sha256("".join(files.values()).encode("utf-8")).hexdigest()[:12]
        cfg = bdir / "panel" / f"{lang}-{digest}"
        store.ensure_text_files(cfg, files)   # 目录名带内容指纹:缺文件就补(上次写到一半断了也能接上),有了不动
        config_dirs[lang] = str(cfg)
        n_questions[lang] = len(panel["questions"])
        doc = sources.read_yaml(cfg / "questions.yml")
        frozen_panels.append(FrozenPanel(lang=lang, path=str(cfg / "questions.yml"), prompt_version=doc["prompt_version"],
                                         sha256=hashlib.sha256(files["questions.yml"].encode("utf-8")).hexdigest(),
                                         source=panel.get("source", "butter_generated")))
        frozen_brands.append(FrozenBrands(lang=lang, path=str(cfg / "brands.yml"), source="事实库",
                                          sha256=hashlib.sha256(files["brands.yml"].encode("utf-8")).hexdigest()))
        legs = [(p, str(defaults.get(f"measure.model.{p}"))) for p in providers]
        # Potato 估一次要几秒;同一份配置、同一组 AI 的结果记下来,页面再打开就不用重估
        wanted = ",".join(f"{p}:{m}" for p, m in legs)
        cache = cfg / f"estimate-{hashlib.sha256(wanted.encode('utf-8')).hexdigest()[:8]}.json"
        uppers: dict[tuple[str, str], float] = {}
        try:
            uppers = {(p, m): float(usd) for p, m, usd in json.loads(cache.read_text(encoding="utf-8"))["uppers"]}
        except (OSError, ValueError, TypeError, KeyError):
            uppers = {}
        if not uppers:
            try:
                code, text = _potato(potato, ["--config-dir", str(cfg), "--surfaces", wanted, "estimate"], penv)
                uppers = P.parse_potato_estimate(text) if code == 0 else {}
                if code != 0:
                    notes.append(f"Potato 没给出它自己的花费上界(退出码 {code})。")
                elif uppers and not cache.exists():
                    store.write_new_json(cache, {"uppers": [[p, m, usd] for (p, m), usd in sorted(uppers.items())]})
            except (OSError, subprocess.SubprocessError) as exc:
                notes.append(f"没能叫起 Potato 估费:{type(exc).__name__}。")
        for provider, model in legs:
            if uppers.get((provider, model), 0.0) > cap:
                notes.append(f"Potato 给 {P.ENGINE_NAME.get(provider, provider)} 估的最高花费(${uppers[(provider, model)]:.2f})"
                             f"超过了每家的上限(${cap:.2f}):这一家开跑前会被 Potato 拦下,测不了。")
            surfaces.append(Surface(lang=lang, provider=provider, model=model))
            estimates.append(P.estimate_leg(defaults, lang=lang, provider=provider, model=model,
                                            calls=len(panel["questions"]) * runs, upper=uppers.get((provider, model)),
                                            judged=(lang, provider) in tiers))
    treated, control, pages = P.split_questions(question_pages, brand_ids, changed)
    rules = plan_rules(defaults, [(s.lang, s.provider, metric) for s in surfaces for metric in ("mention", "citation")],
                       len(treated), len(control), sitewide=sitewide)
    freeze = BatchFreeze(panel=frozen_panels, brands=frozen_brands, surfaces=surfaces,
                         plan=BatchPlan(runs_per_question=runs), treated_q=treated, control_q=control,
                         question_pages=pages, rules=rules)
    bands = [r.band_pp for r in rules.legs if r.metric == "mention" and r.band_pp is not None]
    uppers_all = [e.upper_usd for e in estimates]
    if not treated:
        notes.append("这批题里没有一道对上被改的页面,测了也比不出改动的效果。")
    listed_only = sorted({e.provider for e in estimates if not e.judged})
    if listed_only:
        names = "、".join(P.ENGINE_NAME.get(p, p) for p in listed_only)
        notes.append(f"还不知道 {names} 平时自己会波动多少:它的数字照样测、照样列,但说不了变没变。")
    return MeasurePlan(
        batch_id=batch_id, freeze=freeze, config_dirs=config_dirs, legs=estimates, n_questions=n_questions,
        n_treated=len(treated), n_control=len(control), typical_usd=round(sum(e.typical_usd for e in estimates), 2),
        upper_usd=round(sum(uppers_all), 2) if estimates and all(u is not None for u in uppers_all) else None,
        minutes=round(sum(e.minutes for e in estimates), 0), band_pp=round(max(bands) * 100, 1) if bands else None,
        notes=notes)


def _cached_uppers(cfg: Path) -> dict[tuple[str, str], float]:
    """这份配置以前叫 Potato 估过的花费上界(prepare 存下的)。读不到就当没有。"""
    out: dict[tuple[str, str], float] = {}
    for path in sorted(cfg.glob("estimate-*.json")):
        try:
            out |= {(p, m): float(usd) for p, m, usd in json.loads(path.read_text(encoding="utf-8"))["uppers"]}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


def prepare_remeasure(workspace: Path, out_root: Path, batch_id: str, *, env: Mapping[str, str],
                      potato: PotatoSettings) -> MeasurePlan:
    """再测之前给站长看的:要问几次、大概花多少。

    第一次测量时题板、引擎、分组、判读规则都定下了,再测照那一份来(改之前改之后要问一样的题才能比),这里只估费,不写任何文件。
    没测过改之前就放上网站的批次,再测是它的第一次测量:和「测一次」一样准备。做不了的时候抛 ValueError,话是给站长看的。
    """
    batch_dir(workspace, batch_id)
    view, _ = cards.load(workspace)
    state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
    if state is None:
        raise ValueError("没有这一批改动。")
    batch = state.batch
    if not batch.frozen or batch.rules is None:
        return prepare(workspace, out_root, batch_id, env=env, potato=potato)
    defaults = load_defaults()
    runs = batch.plan.runs_per_question if batch.plan else int(defaults.get("measure.runs_per_question"))
    cfg_dirs = {p.lang: Path(p.path).parent for p in batch.panel}
    n_questions = {lang: len(sources.read_yaml(cfg / "questions.yml")["questions"]) for lang, cfg in cfg_dirs.items()}
    have = set(available_providers(env))
    judged = {(r.lang, r.provider) for r in batch.rules.legs if r.metric == "mention" and r.band_pp is not None}
    estimates, notes = [], []
    for surface in batch.surfaces:
        upper = _cached_uppers(cfg_dirs[surface.lang]).get((surface.provider, surface.model))
        estimates.append(P.estimate_leg(defaults, lang=surface.lang, provider=surface.provider, model=surface.model,
                                        calls=n_questions[surface.lang] * runs, upper=upper,
                                        judged=(surface.lang, surface.provider) in judged))
    for provider in sorted({s.provider for s in batch.surfaces} - have):
        notes.append(f"现在找不到 {P.ENGINE_NAME.get(provider, provider)} 的 key:这一家这次会测不成,别的照常测。")
    if not batch.treated_q:
        notes.append("这批题里没有一道对上被改的页面,测了也比不出改动的效果。")
    bands = [r.band_pp for r in batch.rules.legs if r.metric == "mention" and r.band_pp is not None]
    uppers_all = [e.upper_usd for e in estimates]
    freeze = BatchFreeze(panel=batch.panel, brands=batch.brands, surfaces=batch.surfaces, plan=batch.plan,
                         treated_q=batch.treated_q, control_q=batch.control_q, question_pages=batch.question_pages,
                         rules=batch.rules)
    return MeasurePlan(
        batch_id=batch_id, freeze=freeze, config_dirs={lang: str(cfg) for lang, cfg in cfg_dirs.items()},
        legs=estimates, n_questions=n_questions, n_treated=len(batch.treated_q), n_control=len(batch.control_q),
        typical_usd=round(sum(e.typical_usd for e in estimates), 2),
        upper_usd=round(sum(uppers_all), 2) if estimates and all(u is not None for u in uppers_all) else None,
        minutes=round(sum(e.minutes for e in estimates), 0), band_pp=round(max(bands) * 100, 1) if bands else None,
        notes=notes)


# ── 进度 ──────────────────────────────────────────────────────────────

def _stamp(state, kind: str) -> str:
    return f"baseline-{state.baselines_done + 1}" if kind == "baseline" else f"remeasure-{len(state.readings) + 1}"


def _progress_path(bdir: Path, stamp: str) -> Path:
    return bdir / "measure" / f"{stamp}.jsonl"


def leg_progress(bdir: Path, stamp: str) -> dict[tuple[str, str], LegProgress]:
    """每条腿最后一次记下的样子。"""
    out: dict[tuple[str, str], LegProgress] = {}
    for row in store.read_jsonl(_progress_path(bdir, stamp)):
        try:
            item = LegProgress.model_validate(row)
        except ValueError:
            continue
        out[(item.lang, item.provider)] = item
    return out


def current_progress(workspace: Path, batch_id: str) -> tuple[str, list[LegProgress], bool]:
    """给页面用:这批现在在测哪一次、各条腿到哪了、驱动还在不在跑。"""
    bdir = batch_dir(workspace, batch_id)
    view, _ = cards.load(workspace)
    state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
    if state is None or state.phase not in ("测基线中", "再测中"):
        return "", [], False
    stamp = _stamp(state, "baseline" if state.phase == "测基线中" else "remeasure")
    return stamp, list(leg_progress(bdir, stamp).values()), store.lock_is_held(bdir / "measure.lock")


def wait_started(workspace: Path, batch_id: str, seconds: float = 3.0) -> bool:
    """刚起了驱动:等它拿到锁(或者已经测完)再回页面,免得页面上一闪而过"停了"。等不到返回 False。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        stamp, _, alive = current_progress(workspace, batch_id)
        if alive or not stamp:
            return True
        time.sleep(0.1)
    return False


# ── 驱动 ──────────────────────────────────────────────────────────────

def _new_run_id(data_dir: Path, known: set[str]) -> str:
    """这条腿的 run_id:取 run_locks.jsonl 里新多出来的那一行(Potato 开跑就写)。"""
    for row in reversed(store.read_jsonl(data_dir / "raw" / "run_locks.jsonl")):
        if row.get("run_id") and row["run_id"] not in known:
            return str(row["run_id"])
    return ""


def _failure_counts(data_dir: Path, run_id: str) -> tuple[int, int]:
    """这次运行已经写下的回答里,多少条、其中多少条失败。只读 run_id、finished_ok 两个字段(ADR-B03 的唯一例外)。"""
    rows = [r for r in store.read_jsonl(data_dir / "raw" / "raw_responses.jsonl") if r.get("run_id") == run_id]
    return len(rows), sum(1 for r in rows if not r.get("finished_ok"))


def _run_leg(bdir: Path, stamp: str, surface: Surface, cfg_dir: str, *, potato: PotatoSettings, env: dict[str, str],
             resume_run_id: str, abort_rate: float, abort_min: int, total: int) -> LegProgress:
    data_dir = bdir / "potato" / surface.lang
    log = bdir / "logs" / f"measure-{stamp}.log"
    path = _progress_path(bdir, stamp)
    known = {str(r.get("run_id")) for r in store.read_jsonl(data_dir / "raw" / "run_locks.jsonl")}

    def note(state: str, **extra) -> LegProgress:
        item = LegProgress(at=datetime.now(UTC).isoformat(timespec="seconds"), lang=surface.lang,
                           provider=surface.provider, model=surface.model, state=state, **extra)
        store.append_jsonl(path, item.model_dump())
        return item

    args = ["--config-dir", cfg_dir, "--data-dir", str(data_dir), "--provider", surface.provider, "--model", surface.model]
    if resume_run_id:
        args += ["--run-id", resume_run_id]
    args += ["--yes", "run"]
    store.append_text(log, f"===== {datetime.now(UTC).isoformat(timespec='seconds')} {surface.lang} {surface.provider}:{surface.model}"
                           f"{' 接着跑 ' + resume_run_id if resume_run_id else ''} =====\n")
    run_id, done, spent, aborted = resume_run_id, 0, 0.0, False
    note("在跑", run_id=run_id, total=total)
    try:
        proc = subprocess.Popen([potato.python, "-B", "-m", "aivis.cli", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, cwd=potato.potato_dir, text=True, encoding="utf-8",
                                errors="replace")
    except OSError as exc:
        return note("失败", note=f"没能叫起 Potato:{type(exc).__name__}", total=total)
    assert proc.stdout is not None
    for line in proc.stdout:
        store.append_text(log, line)
        match = PROGRESS.match(line)
        if not match:
            continue
        done, spent = int(match[1]), float(match[3])
        if not run_id:
            run_id = _new_run_id(data_dir, known)
        if done % 5 == 0 or done == int(match[2]):
            rows, failed = _failure_counts(data_dir, run_id) if run_id else (0, 0)
            note("在跑", run_id=run_id, done=done, total=total, failed=failed, cost_usd=spent)
            if rows >= abort_min and failed / rows > abort_rate:
                aborted = True
                proc.terminate()
                break
    code = proc.wait()
    if not run_id:
        run_id = _new_run_id(data_dir, known)
    rows, failed = _failure_counts(data_dir, run_id) if run_id else (0, 0)
    common = dict(run_id=run_id, done=rows, total=total, failed=failed, cost_usd=spent)
    if aborted:
        return note("中止", note=f"问了 {rows} 次有 {failed} 次失败,超过 {abort_rate:.0%},这条腿先停了", **common)
    if code != 0:
        return note("失败" if rows == 0 else "不完整", note=EXIT_NOTE.get(code, f"Potato 退出码 {code}"), **common)
    return note("完成", **common)


def read_leg(bdir: Path, surface: Surface, run_id: str, *, potato: PotatoSettings, env: dict[str, str], focal: str,
             question_ids: list[str], runs: int, min_success: float) -> LegReading | None:
    """用 Potato 自己的解释器跑读取桥,把这条腿读回来。"""
    data_dir = bdir / "potato" / surface.lang
    try:
        done = subprocess.run([potato.python, "-B", str(BRIDGE), str(data_dir), run_id], capture_output=True, env=env,
                              cwd=potato.potato_dir, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout.decode("utf-8"))
    except ValueError:
        return None
    return P.reading_from_bridge(data, lang=surface.lang, provider=surface.provider, model=surface.model, focal=focal,
                                 question_ids=question_ids, runs=runs, min_success=min_success)


def _compare(state, freeze_rules, pre: MeasureReading, post: MeasureReading, day: str) -> CompareResult:
    """逐腿逐指标比。每条腿自己看"前后是不是同一套配置",所以一条腿一条腿地比,再合起来。"""
    batch = state.batch
    before = {(leg.lang, leg.provider): leg for leg in pre.legs if leg.complete}
    legs, errors, used = [], [], []
    for leg in post.legs:
        base = before.get((leg.lang, leg.provider))
        if base is None or not leg.complete:
            continue
        facts = BatchFacts(batch_id=batch.batch_id, treated_q=batch.treated_q, control_q=batch.control_q,
                           baseline_day=pre.day, remeasure_day=day,
                           deploy_day=state.deploy.day if state.deploy else None, deployed=state.deploy is not None,
                           sitewide=state.sitewide, config_mismatch=P.config_mismatch(base, leg))
        result = compare_batch(freeze_rules, facts, [P.leg_data(base, leg, m) for m in ("mention", "citation")])
        legs += result.legs
        errors += result.errors
        used = result.defaults_used
    judged = [x for x in legs if x.label in ("up", "down", "within")]
    return CompareResult(batch_id=batch.batch_id, method_version=freeze_rules.method_version, legs=legs,
                         judged=len(judged), exceeded=sum(1 for x in judged if x.label != "within"),
                         defaults_used=used, errors=errors)


def run(workspace: Path, batch_id: str, kind: str, *, env: Mapping[str, str], potato: PotatoSettings) -> str:
    """驱动的主体:拿锁 → 依次跑各条腿 → 读回 → 记事件。返回一句结果(给日志看)。已有驱动在跑就直接返回。"""
    bdir = batch_dir(workspace, batch_id)
    try:
        # 等一小会儿再认定"已经有驱动":网页查锁时会把锁拿到手一瞬间,不等的话刚起的驱动会被自己人挤掉
        with store.hold_lock(bdir / "measure.lock", wait_s=LOCK_WAIT_S):
            return _run_locked(workspace, bdir, batch_id, kind, env, potato)
    except store.LockHeld:
        return "这一批已经有一个测量在跑了"


def _run_locked(workspace: Path, bdir: Path, batch_id: str, kind: str, env: Mapping[str, str],
                potato: PotatoSettings) -> str:
    view, _ = cards.load(workspace)
    state = next((b for b in view.batches if b.batch.batch_id == batch_id), None)
    expected_phase = "测基线中" if kind == "baseline" else "再测中"
    if state is None or state.phase != expected_phase or not state.batch.frozen:
        return f"这一批现在不在「{expected_phase}」这一步,没有开始测"
    batch = state.batch
    stamp = _stamp(state, kind)
    defaults = load_defaults()
    penv = potato_env(env, potato)
    runs = batch.plan.runs_per_question if batch.plan else int(defaults.get("measure.runs_per_question"))
    cfg_dirs = {p.lang: str(Path(p.path).parent) for p in batch.panel}
    ids_by_lang = {lang: [str(q["id"]) for q in sources.read_yaml(Path(cfg) / "questions.yml")["questions"]]
                   for lang, cfg in cfg_dirs.items()}
    focal_by_lang = {b.lang: sources.read_yaml(Path(b.path))["peers"][0]["name"] for b in batch.brands}
    min_success = float(defaults.get("compare.min_success_rate"))
    seen = leg_progress(bdir, stamp)
    finals: list[tuple[Surface, LegProgress]] = []
    for surface in batch.surfaces:
        last = seen.get((surface.lang, surface.provider))
        if last is not None and last.state == "完成":
            finals.append((surface, last))
            continue
        result = _run_leg(bdir, stamp, surface, cfg_dirs[surface.lang], potato=potato, env=penv,
                          resume_run_id=last.run_id if last and last.state in ("在跑", "不完整") else "",
                          abort_rate=float(defaults.get("measure.abort_failure_rate")),
                          abort_min=int(defaults.get("measure.abort_min_rows")),
                          total=len(ids_by_lang[surface.lang]) * runs)
        finals.append((surface, result))

    day = datetime.now().date().isoformat()
    readings = []
    for surface, last in finals:
        if last.state != "完成" or not last.run_id:
            continue
        reading = read_leg(bdir, surface, last.run_id, potato=potato, env=penv, focal=focal_by_lang[surface.lang],
                           question_ids=ids_by_lang[surface.lang], runs=runs, min_success=min_success)
        if reading is not None:
            readings.append(reading)
    record = MeasureReading(batch_id=batch_id, kind=kind, day=day, stamp=stamp, legs=readings)
    out_path = bdir / "readings" / f"{stamp}.json"
    if not out_path.exists():
        store.write_new_json(out_path, record.model_dump(mode="json"))
    complete = [r for r in readings if r.complete]
    at = datetime.now(UTC)
    prefix = "baseline" if kind == "baseline" else "remeasure"
    if not complete:
        why = ";".join(f"{s.lang} {s.provider}:{p.note or p.state}" for s, p in finals) or "没有可测的腿"
        cards.submit(workspace, at=at, actor="system", batch_id=batch_id, type=f"{prefix}_failed",
                     payload=MeasureFailed(reason=f"没有一条腿测完整。{why}"))
        return f"{stamp}:没有一条腿测完整"
    samples = [P.leg_sample(r, batch.treated_q, runs) for r in complete]
    result = None
    if kind == "remeasure":
        base_files = sorted((bdir / "readings").glob("baseline-*.json"))
        if base_files:
            pre = MeasureReading.model_validate_json(base_files[-1].read_text(encoding="utf-8"))
            result = _compare(state, batch.rules, pre, record, day)
    decision = cards.submit(workspace, at=at, actor="system", batch_id=batch_id, type=f"{prefix}_finished",
                            payload=MeasureDone(day=day, legs=samples, result=result))
    done_text = f"{stamp}:{len(complete)}/{len(batch.surfaces)} 条腿测完整"
    return done_text if decision.ok else f"{done_text};但结果没记上:{decision.reason}"


def start(workspace: Path, plan: MeasurePlan, kind: str = "baseline") -> str | None:
    """站长点了按钮:把"开始测"记到批次上(第一次测量同时冻结题板、分组和判读规则)。返回没记上的原因,记上了返回 None。

    再测一般不带冻结参数(早就定下了)。只有一种情况要带:没测过改之前就放上网站,再测是这一批的第一次测量。
    """
    freeze = plan.freeze
    if kind == "remeasure":
        view, _ = cards.load(workspace)
        state = next((b for b in view.batches if b.batch.batch_id == plan.batch_id), None)
        if state is not None and state.batch.frozen:
            freeze = None
    decision = cards.submit(workspace, at=datetime.now(UTC), actor="owner", batch_id=plan.batch_id,
                            type="baseline_started" if kind == "baseline" else "remeasure_started",
                            payload=MeasureStart(freeze=freeze))
    return None if decision.ok else decision.reason


def spawn(workspace: Path, out_root: Path, batch_id: str, kind: str, *, env: Mapping[str, str],
          potato: PotatoSettings) -> None:
    """起一个脱离本进程的驱动:关掉网页服务,它照样跑完(ADR-B07)。key 只经环境变量交给它。"""
    cmd = [sys.executable, "-X", "utf8", "-m", "butter", "measure-batch", batch_id, "--kind", kind,
           "--workspace", str(workspace), "--out", str(out_root), "--potato-dir", potato.potato_dir,
           "--potato-python", potato.python]
    if potato.pythonpath:
        cmd += ["--potato-pythonpath", potato.pythonpath]
    flags = 0
    if os.name == "nt":
        # 不用 DETACHED_PROCESS:那样驱动自己没有控制台,它再起 Potato 时 Windows 会给 Potato 弹一个看得见的黑窗口
        # (2026-11-23 本机实测),站长顺手关掉这条腿就断了。CREATE_NO_WINDOW 给驱动一个看不见的控制台,Potato 接着用它。
        # 新进程组 + 自己的控制台:关掉网页服务的窗口、按 Ctrl+C 都不会带走驱动。
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    child_env = potato_env(env, potato)
    child_env.pop("PYTHONPATH", None)  # 驱动自己是 Butter 的进程,不要带上 Potato 的搜索路径
    subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[2]), env=child_env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
