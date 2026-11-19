"""假的 Potato(测试用,不联网、不花钱)。学的是 Butter 用到的那部分行为(依据:research/architecture-inputs/A §1):

  python -m aivis.cli --config-dir X --data-dir Y --provider P --model M [--run-id R] --yes run
  python -m aivis.cli --config-dir X --surfaces p:m,p:m estimate
  python -m aivis.cli --config-dir X --phase final validate

- 开跑即写 raw/run_locks.jsonl;一条回答一行追加进 raw/raw_responses.jsonl;每条打印 `PROGRESS probe 已做/总数 已花费`。
- 全部问完才写 raw/run_manifests.jsonl(带 cost_usd_actual)和 analysis/<run_id>/extraction.json;中途断掉就没有这两样。
- 同一个 --run-id 再跑:已有的行(含失败行)跳过,从后面接着问。
- 退出码:0 成功;1 中途崩;2 配置或参数错;4 缺密钥。

用环境变量调行为:
  STUB_MENTION_RATE   每条回答提到本品牌的概率(默认 0.34);按(题号, 第几跑, STUB_SEED)算,同样的输入结果一样
  STUB_MENTION_BOOST  "题号,题号=概率":这些题用另一个概率(模拟改动之后变多或变少)
  STUB_CITE_RATE      引用本品牌官网的概率(默认 0.2)
  STUB_FAIL           每第 k 条失败(error_code=rate_limit)
  STUB_CRASH_AFTER    写完 n 条后退出码 1(模拟中途崩溃)
  STUB_COST           每条多少钱(默认 0.01)
  STUB_SLEEP          每条等几秒(默认 0)
  STUB_SEED           换一个数就是"另一天"的回答
"""
import datetime
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import yaml

KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY", "grok": "XAI_API_KEY", "mock": None}
SURFACE = {"anthropic": "anthropic_api_web_search", "openai": "openai_responses_web_search",
           "gemini": "gemini_generate_content_google_search", "grok": "grok_responses_web_search", "mock": "mock"}


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def chance(*parts) -> float:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def load_config(cfg: Path) -> dict:
    try:
        q = yaml.safe_load((cfg / "questions.yml").read_text(encoding="utf-8"))
        b = yaml.safe_load((cfg / "brands.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(q, dict) or not q.get("questions") or not isinstance(b, dict) or not b.get("peers"):
        print("[配置错误] questions.yml 或 brands.yml 不完整", file=sys.stderr)
        raise SystemExit(2)
    return {**q, **b}


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = next((a for a in reversed(args) if a in ("run", "estimate", "validate")), None)
    cfg_dir = opt(args, "--config-dir")
    if command is None or cfg_dir is None:
        print("[参数错误] 要给 --config-dir 和子命令", file=sys.stderr)
        return 2
    config = load_config(Path(cfg_dir))
    questions = config["questions"]
    n_runs = int(config.get("n_runs", 1))
    cost_each = float(os.environ.get("STUB_COST", "0.01"))
    if command == "validate":
        return 0
    if command == "estimate":
        surfaces = [s.split(":", 1) for s in (opt(args, "--surfaces") or "mock:mock").split(",")]
        total = len(questions) * n_runs
        print(f"[预算估计] {cfg_dir}")
        print(f"  调用数: {len(questions)} 题 × {n_runs} 轮 = {total} 次(每个表面)")
        print(f"  预算上限 budget_cap_usd(每个表面各自受限): ${float(config.get('budget_cap_usd', 0)):.2f}")
        for provider, model in surfaces:
            print(f"  - {provider}:{model}  预估(保守上界) ≈ ${total * cost_each * 3:.2f}  [OK]")
        print(f"  合计(保守上界): ${total * cost_each * 3 * len(surfaces):.2f}")
        return 0

    provider, model = opt(args, "--provider", "mock"), opt(args, "--model", "mock")
    if provider not in KEYS:
        print(f"[表面/模型错误] 不认识的引擎 {provider}", file=sys.stderr)
        return 2
    if "--yes" not in args:
        raise EOFError("没有 --yes:真的 Potato 会在这里等键盘输入")
    if KEYS[provider] and not os.environ.get(KEYS[provider]):
        print(f"[引擎错误] 缺少环境变量 {KEYS[provider]}", file=sys.stderr)
        return 4
    data = Path(opt(args, "--data-dir"))
    raw = data / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    run_id = opt(args, "--run-id") or uuid.uuid4().hex
    fp = fingerprint(config)
    lock = {"run_id": run_id, "config_fingerprint": fp, "provider": provider, "model": model, "tool_version": "stub-1",
            "n_runs": n_runs, "sequential": False, "min_runs": 2, "prompt_version": config.get("prompt_version"),
            "competitor_set_version": config.get("competitor_set_version")}
    locks_path = raw / "run_locks.jsonl"
    old_locks = [json.loads(line) for line in locks_path.read_text(encoding="utf-8").splitlines()] if locks_path.exists() else []
    mine = next((x for x in old_locks if x["run_id"] == run_id), None)
    if mine is None:
        with locks_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(lock, ensure_ascii=False) + "\n")
    elif mine["config_fingerprint"] != fp or mine["model"] != model:
        print("[配置锁] 续跑的配置和第一次不一样", file=sys.stderr)
        return 2

    responses_path = raw / "raw_responses.jsonl"
    rows = [json.loads(line) for line in responses_path.read_text(encoding="utf-8").splitlines()] if responses_path.exists() else []
    done = {(r["question_id"], r["run_index"]) for r in rows if r["run_id"] == run_id}
    focal = next((p for p in config["peers"] if p.get("is_focal")), config["peers"][0])
    seed = os.environ.get("STUB_SEED", "0")
    rate = float(os.environ.get("STUB_MENTION_RATE", "0.34"))
    boost_ids, boost_rate = set(), rate
    if os.environ.get("STUB_MENTION_BOOST"):
        ids, _, value = os.environ["STUB_MENTION_BOOST"].partition("=")
        boost_ids, boost_rate = set(filter(None, ids.split(","))), float(value)
    cite_rate = float(os.environ.get("STUB_CITE_RATE", "0.2"))
    fail_every = int(os.environ.get("STUB_FAIL", "0"))
    crash_after = int(os.environ.get("STUB_CRASH_AFTER", "0"))
    pause = float(os.environ.get("STUB_SLEEP", "0"))
    total = len(questions) * n_runs
    count = len(done)
    spent = count * cost_each
    written = 0
    for q in questions:
        for run_index in range(n_runs):
            if (q["id"], run_index) in done:
                continue
            count += 1
            ok = not (fail_every and count % fail_every == 0)
            p = boost_rate if q["id"] in boost_ids else rate
            mentioned = ok and chance(q["id"], run_index, seed, "m") < p
            cited = ok and chance(q["id"], run_index, seed, "c") < cite_rate
            answer = (f"{focal['name']} is one option." if mentioned else "Several options exist.") if ok else ""
            citations = [{"url": f"https://{focal['domains'][0]}/x", "title": "t", "cited_text": "", "position": 0}] if cited else []
            with responses_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "run_id": run_id, "question_id": q["id"], "run_index": run_index, "answer_text": answer,
                    "citations": citations, "search_count": 1 if ok else 0, "model": model, "finished_ok": ok,
                    "error_code": None if ok else "rate_limit", "error_detail": None, "raw_json": "{}",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }, ensure_ascii=False) + "\n")
            spent += cost_each
            written += 1
            print(f"PROGRESS probe {count}/{total} {spent:.4f}", flush=True)
            if pause:
                time.sleep(pause)
            if crash_after and written >= crash_after:
                print("stub: 模拟中途崩溃", file=sys.stderr)
                return 1

    rows = [json.loads(line) for line in responses_path.read_text(encoding="utf-8").splitlines()]
    mine_rows = [r for r in rows if r["run_id"] == run_id]
    manifests_path = raw / "run_manifests.jsonl"
    have = manifests_path.read_text(encoding="utf-8") if manifests_path.exists() else ""
    if f'"run_id": "{run_id}"' not in have and f'"run_id":"{run_id}"' not in have:
        manifest = {**lock, "surface_id": SURFACE.get(provider, provider), "tool_path": "web_search",
                    "locale": config.get("locale"), "region": config.get("region"), "tier": config.get("tier"),
                    "max_uses": config.get("max_uses"), "budget_cap_usd": config.get("budget_cap_usd"),
                    "temperature": config.get("temperature"),
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "search_count": sum(r["search_count"] for r in mine_rows), "temperature_applied": False,
                    "region_applied": True, "surface_kind": "answer", "cost_usd_actual": round(len(mine_rows) * cost_each, 6)}
        with manifests_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False) + "\n")
    out = data / "analysis" / run_id
    out.mkdir(parents=True, exist_ok=True)
    mentions, cites = [], []
    for r in mine_rows:
        if not r["finished_ok"]:
            continue
        for peer in config["peers"]:
            hit = peer["name"].lower() in r["answer_text"].lower()
            mentions.append({"run_id": run_id, "question_id": r["question_id"], "run_index": r["run_index"],
                             "brand": peer["name"], "mentioned": hit, "first_position": 0 if hit else None,
                             "mention_count": int(hit), "counted": True, "reason": None,
                             "answer_length": len(r["answer_text"])})
        for c in r["citations"]:
            domain = c["url"].split("/")[2]
            cites.append({"run_id": run_id, "question_id": r["question_id"], "run_index": r["run_index"], "url": c["url"],
                          "normalized_url": c["url"], "canonical_domain": domain, "brand": focal["name"],
                          "owned_or_earned": "owned", "basis": "domain", "counted": True, "reason": None})
    (out / "extraction.json").write_text(json.dumps({
        "run_id": run_id,
        "response_refs": [{"question_id": r["question_id"], "run_index": r["run_index"], "finished_ok": r["finished_ok"],
                           "error_code": r["error_code"]} for r in mine_rows],
        "mentions": mentions, "citations": cites}, ensure_ascii=False), encoding="utf-8")
    print(f"probe 完成: run_id={run_id}  search_count={sum(r['search_count'] for r in mine_rows)}")
    print(f"score 完成: {out / 'score.json'}  健康灯=green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
