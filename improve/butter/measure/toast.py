"""再测不另起一套提问:把测量工具(Toast)前后两单的结果,读成比对核心要的输入(FatCat 整合,阶段 3)。

前一单是改之前测的,后一单是它的「修好后再测」:同样的题、同样的型号、同样的档位。Butter 这边不问 AI、不叫 Potato。

「提没提到」「引没引用官网」用的是 Toast 出报告时存下的判定(下载包 responses.jsonl 里的 judgements),这里不重算:
- 提及:成功的回答里,判了「提到」的算命中;「只出现了这个词、看不出是不是你」的不进分母(和 Toast 的报告同一个算法)。
- 被引:成功的回答里,brand_site_cited 为 true 的算命中;看不到引用的(null)不进分母。
- 题目按题号配对:再测时 Toast 给题目换了新的内部编号,题号和文字不变。

这几个型号、这套判定方法,还没有量过「不改动时自己会晃多少」。所以这里不带波动数据:
数照列,不判变没变(比对核心原有的规矩「没有这条腿的波动数据」)。要判,得先在这套测法上量出波动。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from butter.cards.build import SITEWIDE_KINDS
from butter.compare import compare_batch
from butter.compare.core import METHOD_VERSION, label_text
from butter.config import load_defaults
from butter.config.core import Defaults
from butter.contracts import (
    BatchFacts, ChangePackage, CompareResult, CompareRules, FactBase, LegData, LegRule, QuestionObs,
)
from butter.generate.headings import tokens
from butter.measure import plan as P
from butter.questions import categories_from_factbase
from butter.sources import SourceFileError, _csv_cell, _toast_members

BRAND_INTENT = "brand_fact"  # Toast 五类题里点名问品牌的那一类:只作背景,不进处理组和对照组
SUCCESS = "SUCCESS"
METRICS = ("mention", "citation")
_VERDICT = {"mention": "mentioned", "citation": "brand_site_cited"}
# 判定器的版本前后不一样,数就不能直接比。只看决定这两个数的那几个
_DETECTORS = {"mention": ("brand_mention_rules", "brand_mention_fuzzy", "brand_mention_judge"),
              "citation": ("citation_domain_match",)}


_METHOD = re.compile(r"(?:方法版本|Method version)\s*([0-9][0-9.]*)")  # Toast 汇总里那一行,中英两种写法


@dataclass(frozen=True)
class Leg:
    """一个型号在一单里的全部回答。"""

    ai: str                      # Toast 报告里的叫法:「接口 · 型号」
    surface: str
    model: str
    lang: str
    rows: list[dict[str, Any]]

    @property
    def ok(self) -> int:
        return sum(1 for r in self.rows if r.get("outcome") == SUCCESS)

    def detectors(self, metric: str) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for row in self.rows:
            for name, version in ((row.get("judgements") or {}).get("detectors") or {}).items():
                if name in _DETECTORS[metric]:
                    seen[name] = version
        return seen


@dataclass(frozen=True)
class ToastRun:
    """一单测量读回来的样子。"""

    path: str
    day: str                                 # 最后一条回答的日期(这台电脑的时区)
    prompts: dict[str, dict[str, str]]       # 题号 → intent、text、locale
    legs: dict[tuple[str, str], Leg]         # (接口, 型号) → 这个型号的回答
    domains: list[str] = field(default_factory=list)   # 算引用时认的官网域名
    method_version: str = ""                           # Toast 写在汇总里的测量方法版本;读不到就是空

    @property
    def unprompted(self) -> list[str]:
        """不点名品牌的题(题号)。"""
        return [no for no, p in self.prompts.items() if p["intent"] != BRAND_INTENT]


def load_run(path: str | Path) -> ToastRun:
    """读一个 Toast 下载包(zip 或解开的文件夹)。缺文件、读不懂就报错,不猜。"""
    path = Path(path)
    if not path.exists():
        raise SourceFileError(f"读不了 {path}:文件不存在")
    members = _toast_members(path)
    for name in ("prompts.csv", "responses.jsonl"):
        if name not in members:
            raise SourceFileError(f"{path} 里没有 {name},不能拿来比对")
    prompts: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(members["prompts.csv"].decode("utf-8-sig", errors="replace"))):
        no = _csv_cell(row.get("no") or "").strip()
        if no:
            prompts[no] = {"intent": _csv_cell(row.get("intent") or ""), "text": _csv_cell(row.get("text") or ""),
                           "locale": _csv_cell(row.get("locale") or "")}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    latest = ""
    for number, line in enumerate(members["responses.jsonl"].decode("utf-8-sig", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            key = (str(row["surface"]), str(row["model"]))
            row["prompt_no"] = str(row["prompt_no"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceFileError(f"{path} 的 responses.jsonl 第 {number} 行读不懂:{type(exc).__name__}") from exc
        grouped.setdefault(key, []).append(row)
        latest = max(latest, str(row.get("timestamp") or ""))
    if not prompts or not grouped:
        raise SourceFileError(f"{path} 里没有题目或没有回答,不能拿来比对")
    try:
        day = datetime.fromisoformat(latest).astimezone().date().isoformat()
    except ValueError as exc:
        raise SourceFileError(f"{path} 里的回答没有可用的时间,不知道这一单是哪天测的") from exc
    legs = {key: Leg(ai=str(rows[0].get("ai") or f"{key[0]} · {key[1]}"), surface=key[0], model=key[1],
                     lang=str(rows[0].get("locale") or "")[:2].lower(), rows=rows)
            for key, rows in grouped.items()}
    domains: list[str] = []
    if "brand-domains.json" in members:
        try:
            doc = json.loads(members["brand-domains.json"].decode("utf-8-sig"))
            listed = (doc.get("list") or {}).get("domains") or []
            domains = sorted({str(d.get("domain")) for d in listed if d.get("status") == "confirmed"}
                             | {str(d) for d in (doc.get("brand") or {}).get("domains") or []})
        except (ValueError, AttributeError):
            domains = []
    method = _METHOD.search(members["summary.md"].decode("utf-8-sig", errors="replace")) if "summary.md" in members else None
    return ToastRun(path=str(path), day=day, prompts=prompts, legs=legs, domains=domains,
                    method_version=method.group(1) if method else "")


@dataclass(frozen=True)
class Groups:
    """哪些题进处理组、哪些进对照组,以及是怎么分出来的。"""

    treated: list[str]
    control: list[str]
    sitewide: bool
    basis: str                               # by_page:按题对上的页面分的;all_unprompted:分不出来,退回全部不点名的题
    pages: dict[str, str] = field(default_factory=dict)   # 题号 → 它对上的那一页
    notes: list[str] = field(default_factory=list)


def groups_from_site_run(run_dir: str | Path, run: ToastRun) -> Groups:
    """在统一的题目上分处理组和对照组(设计 §9.2 的分法,原来只用在 Butter 自己出的题上)。

    一道题对上一个页面,要两头都成立:事实库里的某个品类词整个出现在题目里,也整个出现在页面的标题或 H1 里
    (和 `questions.match_by_category` 同一条规矩;那边的品类词是出题时填进去的,这里的题是 Toast 出的,所以先看题里有没有)。
    对上被改页面的 → 处理组;只对上没改页面的 → 对照组;对不上任何页面的、点名问品牌的 → 只作背景。
    一道题都进不了处理组时,退回「全部不点名的题」,并说明原因:那时比的不是某个页面的改动,只是前后两次的总数。
    """
    run_dir = Path(run_dir)
    try:
        fb = FactBase.model_validate_json((run_dir / "factbase.json").read_text(encoding="utf-8"))
        package = ChangePackage.model_validate_json(
            (run_dir / "change-package" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceFileError(f"读不了 {run_dir} 里的事实库或改动包清单:{type(exc).__name__}") from exc
    items = [item for item in package.items if item.action != "none"]
    changed = {page for item in items if (page := P.page_of_target(item.target))}
    sitewide = any(item.kind in SITEWIDE_KINDS for item in items)
    categories = [words for c in categories_from_factbase(fb) if (words := tokens(c))]
    page_words = {p.url: set().union(*(tokens(f) for f in (p.title or "", *p.h1))) for p in fb.pages}
    question_pages: dict[str, list[str]] = {}
    for no in run.unprompted:
        in_question = tokens(run.prompts[no]["text"])
        mine = [words for words in categories if words <= in_question]
        question_pages[no] = [url for url, words in page_words.items() if any(c <= words for c in mine)]
    treated, control, pages = P.split_questions(question_pages, set(), changed)
    if treated:
        return Groups(treated=treated, control=control, sitewide=sitewide, basis="by_page", pages=pages)
    if not categories:
        why = "网站上没有读到品类词,分不出哪道题对应哪个页面"
    elif not changed:
        why = "这批改动里没有落在具体页面上的(都是全站性的,或者要你自己定怎么改的)"
    else:
        why = "没有一道题对得上被改的页面"
    return Groups(treated=run.unprompted, control=[], sitewide=True, basis="all_unprompted",
                  notes=[f"{why}。下面列的是全部不点名品牌的题前后两次的数,不是某个页面改动的效果。"])


def _per_question(leg: Leg, metric: str) -> dict[str, tuple[int, int]]:
    """每道题:(命中几次, 判得出来的有几次)。只算成功的回答。"""
    out: dict[str, tuple[int, int]] = {}
    for row in leg.rows:
        if row.get("outcome") != SUCCESS:
            continue
        verdict = (row.get("judgements") or {}).get(_VERDICT[metric])
        if verdict is None:
            continue
        hits, n = out.get(row["prompt_no"], (0, 0))
        out[row["prompt_no"]] = (hits + (1 if verdict is True else 0), n + 1)
    return out


def _searched_share(leg: Leg) -> float | None:
    """成功的回答里,看得出「搜了」的占多少。一条都看不出搜没搜(这家不公开)就返回 None。"""
    known = [r.get("search_triggered") for r in leg.rows if r.get("outcome") == SUCCESS]
    known = [v for v in known if isinstance(v, bool)]
    return round(sum(known) / len(known), 4) if known else None


def leg_data(pre: Leg, post: Leg, metric: str) -> LegData:
    """同一个型号前后两单 → 比对核心要的输入(按题号配对)。"""
    before, after = _per_question(pre, metric), _per_question(post, metric)
    questions = [QuestionObs(question_id=no, pre_hits=before[no][0], pre_n=before[no][1],
                             post_hits=after[no][0], post_n=after[no][1])
                 for no in sorted(before.keys() & after.keys(), key=lambda x: (len(x), x))]
    return LegData(lang=post.lang, provider=post.ai, metric=metric, questions=questions, pre_ok=pre.ok,  # type: ignore[arg-type]
                   pre_expected=len(pre.rows), post_ok=post.ok, post_expected=len(post.rows),
                   pre_searched_share=_searched_share(pre))


def config_mismatch(before: ToastRun, after: ToastRun, pre: Leg, post: Leg, metric: str) -> list[str]:
    """前后两单是不是同一套测法。不是的话,差在哪(这些话会写进结果里)。"""
    out = []
    texts = lambda run: {no: p["text"] for no, p in run.prompts.items()}  # noqa: E731
    if texts(before) != texts(after):
        out.append("题目")
    runs = lambda leg: len(leg.rows) / max(1, len({r["prompt_no"] for r in leg.rows}))  # noqa: E731
    if runs(pre) != runs(post):
        out.append("每题问的次数")
    if pre.detectors(metric) != post.detectors(metric):
        out.append("判定方法的版本")
    if {r.get("search_mode") for r in pre.rows} != {r.get("search_mode") for r in post.rows}:
        out.append("搜索开关")
    if metric == "citation" and before.domains != after.domains:
        out.append("官网名单")
    return out


def rules_without_noise(defaults: Defaults, legs: list[tuple[str, str, str]], n_t: int, n_c: int,
                        sitewide: bool) -> CompareRules:
    """比对规则,门槛照默认值,但不带波动数据:这套测法上还没量过,拿在别的测法上量的来判就是乱判。"""
    th = defaults.thresholds()
    return CompareRules(
        method_version=METHOD_VERSION, thresholds=th, noise=[],
        legs=[LegRule(lang=lang, provider=ai, metric=metric, n_T=n_t, n_C=n_c,  # type: ignore[arg-type]
                      control_valid=n_c >= th.min_control and not sitewide, primary=None, tier_days=None,
                      band_pp=None, band_did=None) for lang, ai, metric in legs],
        defaults_used=[v for v in defaults.all() if not v.key.startswith("noise.")])


def _pooled(data: LegData, ids: list[str]) -> dict[str, list[int]]:
    """处理组合起来的「几次里几次」,给人看的(比对核心算的是按题平均)。"""
    picked = [q for q in data.questions if q.question_id in set(ids)]
    return {"pre": [sum(q.pre_hits for q in picked), sum(q.pre_n for q in picked)],
            "post": [sum(q.post_hits for q in picked), sum(q.post_n for q in picked)]}


def compare_runs(before: ToastRun, after: ToastRun, *, treated: list[str] | None = None,
                 control: list[str] | None = None, deploy_day: str | None = None, sitewide: bool | None = None,
                 groups: Groups | None = None, defaults: Defaults | None = None) -> dict[str, Any]:
    """比两单。返回一份可以直接存成 JSON 的结果。

    分组三种给法,先后是:groups(按读网站那次运行分出来的,见 groups_from_site_run)> treated / control(直接给题号)>
    都不给(处理组 = 全部不点名品牌的题,没有对照组,按「全站改动」处理)。结果里的 basis 写明用的是哪一种。
    deploy_day:改动放上网站的日子(ISO 日期);不给就是还没部署。
    """
    defaults = defaults or load_defaults()
    notes: list[str] = []
    pages: dict[str, str] = {}
    if groups is not None:
        treated, control, sitewide, basis = groups.treated, groups.control, groups.sitewide, groups.basis
        notes, pages = list(groups.notes), dict(groups.pages)
    else:
        basis = "given" if treated is not None else "all_unprompted"
        if treated is None:
            treated, control, sitewide = before.unprompted, [], True
    control = control or []
    sitewide = bool(sitewide)
    if before.method_version and after.method_version and before.method_version != after.method_version:
        # Toast 的规矩:同一方法版本的两单才能直接对照。这里不替它判哪些数受影响,只把版本不同这件事摆出来
        notes.append(f"前后两单用的测量方法版本不一样({before.method_version} → {after.method_version})。"
                     "按测量工具的规矩,同一方法版本的两单才能直接对照;两个版本之间改了什么,看它的版本说明。")
    shared = [key for key in before.legs if key in after.legs]
    for key in before.legs.keys() - after.legs.keys():
        notes.append(f"{before.legs[key].ai}:改之前测了,再测里没有这个型号,比不了。")
    for key in after.legs.keys() - before.legs.keys():
        notes.append(f"{after.legs[key].ai}:再测里有,改之前没测过这个型号,比不了。")
    rules = rules_without_noise(defaults, [(after.legs[k].lang, after.legs[k].ai, m) for k in shared for m in METRICS],
                                len(treated), len(control), sitewide)
    legs, errors = [], []
    for key in shared:
        pre, post = before.legs[key], after.legs[key]
        for metric in METRICS:
            data = leg_data(pre, post, metric)
            facts = BatchFacts(batch_id="toast", treated_q=treated, control_q=control, baseline_day=before.day,
                               remeasure_day=after.day, deploy_day=deploy_day, deployed=deploy_day is not None,
                               sitewide=sitewide, config_mismatch=config_mismatch(before, after, pre, post, metric))
            result: CompareResult = compare_batch(rules, facts, [data])
            errors += result.errors
            leg = result.legs[0]
            legs.append({"ai": post.ai, "surface": post.surface, "model": post.model,
                         **leg.model_dump(mode="json"), "label_text": label_text(leg),
                         "counts": _pooled(data, treated), "ok": {"pre": [pre.ok, len(pre.rows)],
                                                                  "post": [post.ok, len(post.rows)]}})
    return {
        "schema": "butter.compare-toast/1", "method_version": rules.method_version,
        "before": {"file": Path(before.path).name, "day": before.day, "questions": len(before.prompts),
                   "method_version": before.method_version},
        "after": {"file": Path(after.path).name, "day": after.day, "questions": len(after.prompts),
                  "method_version": after.method_version},
        "deploy_day": deploy_day,
        "groups": {"basis": basis, "treated": treated, "control": control, "sitewide": sitewide, "pages": pages},
        "noise": "none",  # 这套测法上还没有波动数据:只列数字,不判变没变
        "legs": legs, "notes": notes, "errors": errors,
    }


def summary_lines(result: dict[str, Any]) -> list[str]:
    """给命令行看的几行话。"""
    pct = lambda v: "—" if v is None else f"{v:.0%}"  # noqa: E731
    name = {"mention": "提到你", "citation": "引用官网"}
    lines = [f"改之前:{result['before']['day']}({result['before']['file']});再测:{result['after']['day']}"
             f"({result['after']['file']});部署:{result['deploy_day'] or '还没记'}",
             f"比的是 {len(result['groups']['treated'])} 道题"
             + ("(全部不点名品牌的题)" if result["groups"]["basis"] == "all_unprompted" else "")
             + f";对照 {len(result['groups']['control'])} 道。这套测法上还没有波动数据:只列数字,不判变没变。"]
    for leg in result["legs"]:
        (a, b), (c, d) = leg["counts"]["pre"], leg["counts"]["post"]
        lines.append(f"- {leg['ai']} · {name[leg['metric']]}:{a}/{b} → {c}/{d}(按题平均 {pct(leg['pre_T'])} → "
                     f"{pct(leg['post_T'])});{leg['label_text']}" + (f":{leg['reason']}" if leg["reason"] else ""))
    return lines + [f"注意:{note}" for note in result["notes"]] + [f"出错:{err}" for err in result["errors"]]
