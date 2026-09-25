"""校准分析(只读):始祖鸟首单跑完后,按「上一代 vs 当前一代」并排看每家 AI 各型号的三阶段结论。

数据源 = 库里最新一版报告快照(真实流水线的判定,不另算),外加 surface_run 的原始计数
(有效次数 / 命中次数 / 花费),用来解释结论一致或不一致的原因。
用法: .venv\\Scripts\\python calibrate_generations.py [job_id]
"""

from __future__ import annotations

import sys
from collections import defaultdict

import psycopg

DSN = "postgresql://geo@localhost:5433/geo_tool"
JOB = sys.argv[1] if len(sys.argv) > 1 else "b4099bc0df58"
# 「过时」定义(用户 2026-09-21 拍板):官方标遗留 / 同档已有继任 → 与其继任者配对
PAIRS = [
    ("claude_web_search", "claude-sonnet-4-6", "claude-sonnet-5"),
    ("claude_web_search", "claude-opus-4-8", "claude-opus-5"),
    ("gemini_grounding", "gemini-3.5-flash", "gemini-3.6-flash"),
    ("gemini_grounding", "gemini-3.7-flash", "gemini-3.8-flash"),
]
LIGHT = {"PASS": "🟢", "WEAK": "🟡", "FAIL": "🔴", "NOT_OBSERVABLE": "⚪"}

c = psycopg.connect(DSN)
row = c.execute(
    "SELECT payload, spent_usd, created_at FROM report_snapshot WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
    (JOB,),
).fetchone()
if row is None:
    sys.exit(f"job {JOB} has no report snapshot yet")
payload, spent, created = row
print(f"job {JOB}: snapshot {created:%Y-%m-%d %H:%M}, spent ${spent:.2f}\n")

# 每题 × 型号 → 各阶段状态(来自真实诊断块)
status: dict[tuple[str, str, str], dict[str, str]] = {}
for report in payload["need_reports"]:
    need = report["need"]["statement"][:18]
    for block in report["diagnosis"]["surfaces"]:
        status[(need, block["surface"], block["model"])] = {
            f["stage"]: f["status"] for f in block["findings"]
        } | {"primary": block.get("primary_failure") or "-"}

# 原始计数(有效次数、引用命中、提及命中、花费)按题 × 型号
counts: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
for stmt, surface, model, outcome, n, cost, cited, mentioned in c.execute(
    """
    SELECT left(n.statement, 18), r.surface, r.model, r.outcome, count(*), sum(r.cost_usd),
           sum(CASE WHEN r.outcome = 'SUCCESS' AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements(coalesce(r.answer->'citations', '[]'::jsonb)) ci
                 WHERE ci->>'domain' LIKE '%%arcteryx.com') THEN 1 ELSE 0 END),
           sum(CASE WHEN r.outcome = 'SUCCESS' AND (r.answer->>'answer_text') ~* '始祖鸟|arc.teryx|arcteryx' THEN 1 ELSE 0 END)
    FROM surface_run r JOIN information_need n ON n.need_id = r.information_need_id
    WHERE r.search_mode = 'default' AND n.need_id IN (
        SELECT jsonb_array_elements(snapshot->'needs')->>'need_id' FROM diagnosis_job WHERE job_id = %s)
    GROUP BY 1, 2, 3, 4
    """,
    (JOB,),
):
    key = (stmt, surface, model)
    counts[key]["cost"] += float(cost)
    if outcome == "SUCCESS":
        counts[key]["n"] += n
        counts[key]["cited"] += cited
        counts[key]["mentioned"] += mentioned
    elif outcome == "PERMANENT_FAILURE" or outcome == "TRANSIENT_FAILURE":
        counts[key]["fail"] += n

questions = sorted({k[0] for k in status})
for q in questions:
    print(f"== 题:{q}… ==")
    print(f"{'model':<24} {'检索':>4} {'引用':>4} {'描述':>4} {'瓶颈':<12} "
          f"{'有效':>4} {'引用k':>5} {'提及k':>5} {'失败':>4} {'花费':>7}")
    for (qq, surface, model), st in sorted(status.items(), key=lambda kv: (kv[0][1], kv[0][2])):
        if qq != q:
            continue
        cnt = counts[(qq, surface, model)]
        lights = " ".join(f"{LIGHT.get(st.get(s), '?'):>4}" for s in ("RETRIEVAL", "CITATION", "REPRESENTATION"))
        print(f"{model:<24} {lights} {st['primary']:<12} {int(cnt['n']):>4} {int(cnt['cited']):>5} "
              f"{int(cnt['mentioned']):>5} {int(cnt['fail']):>4} ${cnt['cost']:>6.2f}")
    print()

print("== 上一代 vs 当前一代:三阶段结论是否一致 ==")
for surface, old, new in PAIRS:
    for q in questions:
        a, b = status.get((q, surface, old)), status.get((q, surface, new))
        if a is None or b is None:
            print(f"{old:<20} vs {new:<20} {q[:12]}…  缺数据")
            continue
        same = all(a.get(s) == b.get(s) for s in ("RETRIEVAL", "CITATION", "REPRESENTATION"))
        diff = [f"{s}:{LIGHT.get(a.get(s), '?')}→{LIGHT.get(b.get(s), '?')}"
                for s in ("RETRIEVAL", "CITATION", "REPRESENTATION") if a.get(s) != b.get(s)]
        print(f"{old:<20} vs {new:<20} {q[:12]}…  {'一致' if same else '不一致 ' + ' '.join(diff)}")
