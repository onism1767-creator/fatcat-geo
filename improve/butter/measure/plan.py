"""测量前的准备(纯层):把勾好的题和品牌写成 Potato 认的配置;分出处理组、对照组;估费;解读读回来的结果。

Potato 的规矩(research/architecture-inputs/A §1.3):一个配置目录只能有一种语言和地区;questions.yml 里多余的键
会被悄悄丢掉,所以"题对上哪页"存在批次里,不写进题板;改任何一处配置都要换版本号,预算上限也算在指纹里。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from butter.config.core import Defaults
from butter.contracts import FactBase, LegData, LegSample, QuestionObs
from butter.contracts.measure import LegEstimate, LegReading, QuestionCount

LOCALE = {"zh": ("zh-CN", "CN"), "en": ("en-US", "US")}
# 和 E0 配置里的一样(Potato 的健康灯阈值;Butter 的比对不用它们)
VERIFY_THRESHOLDS = {"citation_validity_yellow": 0.85, "citation_validity_red": 0.7, "ambiguous_yellow": 0.25,
                     "ambiguous_red": 0.4, "failure_yellow": 0.05, "failure_red": 0.15}
_URL = re.compile(r"^(https?://[^\s::(]+)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def brand_block(fb: FactBase) -> dict[str, Any]:
    """本品牌在 Potato 品牌表里的一行。只放本品牌:Butter 的指标只看本品牌,不替站长列竞品。"""
    name = fb.entity.name.values[0].value if fb.entity.name.values else fb.host
    aliases = [v.value for v in fb.entity.aliases.values if v.value and v.value != name]
    domains = [v.value for v in fb.entity.official_domain.values] or [fb.host.removeprefix("www.")]
    return {"name": name, "aliases": aliases, "ambiguous_aliases": [], "domains": domains, "is_focal": True}


def potato_config(questions: list[dict[str, str]], brand: dict[str, Any], *, lang: str, runs: int,
                  budget_cap_usd: float) -> dict[str, str]:
    """一种语言的 Potato 配置目录内容:{文件名: 文本}。版本号里带内容的指纹:内容一变,版本号跟着变。"""
    if lang not in LOCALE:
        raise ValueError(f"只支持中文和英文的题板,没有 {lang}")
    locale, region = LOCALE[lang]
    qs = [{"id": q["id"], "intent": q["intent"], "text": q["text"]} for q in questions]
    q_digest = _sha(yaml.safe_dump(qs, allow_unicode=True, sort_keys=True))[:10]
    b_digest = _sha(yaml.safe_dump(brand, allow_unicode=True, sort_keys=True))[:10]
    questions_doc = {
        "version": "butter", "prompt_version": f"butter-{lang}-{q_digest}", "tier": "standard", "locale": locale,
        "region": region, "temperature": 0.0, "n_runs": runs, "sequential": False, "max_uses": 3,
        "budget_cap_usd": budget_cap_usd, "strata": ["region", "locale", "tier"],
        "verify_thresholds": VERIFY_THRESHOLDS, "questions": qs,
    }
    brands_doc = {"competitor_set_version": f"butter-brand-{b_digest}", "focal_domain": brand["domains"][0],
                  "competitors_on": False, "peers": [brand]}
    dump = lambda doc: yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)  # noqa: E731
    return {"questions.yml": dump(questions_doc), "brands.yml": dump(brands_doc)}


def page_of_target(target: str) -> str | None:
    """卡片上"放在哪"那句话开头的网址;全站类改动(robots、事实页)没有网址。"""
    match = _URL.match(target.strip())
    return match.group(1).rstrip("/") if match else None


def split_questions(question_pages: dict[str, list[str]], brand_question_ids: set[str], changed_pages: set[str],
                    ) -> tuple[list[str], list[str], dict[str, str]]:
    """每道题归哪一组(设计 §9.2):对上被改页面的 → 处理组;只对上没改页面的 → 对照组;
    对不上任何页面的、直接问品牌的 → 只作背景,不进这两组。返回(处理组, 对照组, 题 → 它对上的那一页)。"""
    changed = {u.rstrip("/") for u in changed_pages}
    treated, control, pages = [], [], {}
    for qid, urls in question_pages.items():
        if qid in brand_question_ids or not urls:
            continue
        hit = [u for u in urls if u.rstrip("/") in changed]
        pages[qid] = (hit or urls)[0]
        (treated if hit else control).append(qid)
    return treated, control, pages


ENGINE_NAME = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini", "grok": "Grok"}


def parse_potato_estimate(text: str) -> dict[tuple[str, str], float]:
    """Potato `estimate` 的文字输出 → {(引擎, 型号): 保守上界美元}。读不懂的行跳过。"""
    out = {}
    for line in text.splitlines():
        match = re.search(r"-\s*([a-z_]+):(\S+)\s+预估\(保守上界\)\s*≈\s*\$([0-9.]+)", line)
        if match:
            out[(match[1], match[2])] = float(match[3])
    return out


def estimate_leg(defaults: Defaults, *, lang: str, provider: str, model: str, calls: int,
                 upper: float | None, judged: bool = True) -> LegEstimate:
    per_call = float(defaults.get(f"measure.cost_per_call.{provider}"))
    seconds = float(defaults.get(f"measure.seconds_per_call.{provider}"))
    return LegEstimate(lang=lang, provider=provider, model=model, calls=calls, upper_usd=upper, judged=judged,
                       typical_usd=round(calls * per_call, 2), minutes=round(calls * seconds / 60, 1))


def reading_from_bridge(data: dict, *, lang: str, provider: str, model: str, focal: str, question_ids: list[str],
                        runs: int, min_success: float) -> LegReading:
    """读取桥给的 JSON → 这条腿每道题的数。提及只算 Potato 判的清晰命中(counted)。"""
    responses = data.get("responses") or []
    ok_by_q: dict[str, int] = {}
    searched = 0
    for r in responses:
        if r["finished_ok"]:
            ok_by_q[r["question_id"]] = ok_by_q.get(r["question_id"], 0) + 1
            searched += 1 if r.get("search_count") else 0
    hits: dict[str, set[int]] = {}
    for m in data.get("mentions") or []:
        if m["brand"] == focal and m["mentioned"] and m["counted"]:
            hits.setdefault(m["question_id"], set()).add(m["run_index"])
    cites: dict[str, set[int]] = {}
    for c in data.get("owned_citations") or []:
        if c["brand"] == focal:
            cites.setdefault(c["question_id"], set()).add(c["run_index"])
    manifest = data.get("manifest") or {}
    lock = data.get("lock") or {}
    ok = sum(ok_by_q.values())
    expected = len(question_ids) * runs
    complete = bool(data.get("manifest")) and bool(data.get("has_extraction")) and expected > 0 and ok / expected >= min_success
    return LegReading(
        lang=lang, provider=provider, model=manifest.get("model") or lock.get("model") or model,
        run_id=data.get("run_id", ""), surface_id=manifest.get("surface_id") or "",
        tool_version=manifest.get("tool_version") or lock.get("tool_version") or "",
        config_fingerprint=manifest.get("config_fingerprint") or lock.get("config_fingerprint") or "",
        ok=ok, expected=expected, cost_usd=float(manifest.get("cost_usd_actual") or 0.0),
        searched_share=round(searched / ok, 4) if ok else None, complete=complete,
        questions=[QuestionCount(question_id=q, ok=ok_by_q.get(q, 0), mention_hits=len(hits.get(q, ())),
                                 citation_hits=len(cites.get(q, ()))) for q in question_ids])


def leg_sample(reading: LegReading, treated: list[str], runs: int) -> LegSample:
    """卡片上"基线""修改后结果"两栏要写的摘要:处理组的提及率和被引率。"""
    rows = [q for q in reading.questions if q.question_id in set(treated) and q.ok]
    n = sum(q.ok for q in rows)
    return LegSample(
        lang=reading.lang, provider=reading.provider, model=reading.model, surface_id=reading.surface_id,
        tool_version=reading.tool_version, config_fingerprint=reading.config_fingerprint, ok=reading.ok,
        expected=reading.expected, n_questions=len(rows), runs_per_question=runs,
        mention_rate=round(sum(q.mention_hits for q in rows) / n, 4) if n else None,
        citation_rate=round(sum(q.citation_hits for q in rows) / n, 4) if n else None)


def leg_data(pre: LegReading, post: LegReading, metric: str) -> LegData:
    """基线和再测的同一条腿 → 比对要的输入(按题配对)。"""
    before = {q.question_id: q for q in pre.questions}
    obs = []
    for q in post.questions:
        b = before.get(q.question_id)
        if b is None:
            continue
        pick = (lambda x: x.mention_hits) if metric == "mention" else (lambda x: x.citation_hits)
        obs.append(QuestionObs(question_id=q.question_id, pre_hits=pick(b), pre_n=b.ok, post_hits=pick(q), post_n=q.ok))
    return LegData(lang=post.lang, provider=post.provider, metric=metric, questions=obs, pre_ok=pre.ok,
                   pre_expected=pre.expected, post_ok=post.ok, post_expected=post.expected,
                   pre_searched_share=pre.searched_share)


def config_mismatch(pre: LegReading, post: LegReading) -> list[str]:
    """前后两次是不是同一套配置(设计 §9.4 总表第 3 行)。预算上限也在指纹里,所以只改上限也算变了。"""
    out = []
    if pre.config_fingerprint != post.config_fingerprint:
        out.append("题板、品牌表或采样设置")
    if pre.model != post.model:
        out.append("引擎型号")
    if pre.tool_version != post.tool_version:
        out.append("搜索工具版本")
    return out
