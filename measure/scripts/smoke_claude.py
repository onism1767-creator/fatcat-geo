"""手动 smoke:对 Claude web_search 通道打真实请求(开卷 + 闭卷各一发)。

12.3:真实 API 调用只存在于手动 smoke 脚本与 golden payload 更新流程,
本脚本就是前者,永不进 CI。

用法:
    .venv\\Scripts\\python scripts\\smoke_claude.py [--capture DIR]

--capture DIR 时把原始返回体写入该目录(golden payload 更新流程的输入,
入库前须人工 diff 审查)。key 从 .env / 环境变量读取,输出中一律脱敏。

预期成本:healthcheck 近零 + 开卷一发(上界约 $0.17)+ 闭卷一发(约 $0.04)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.base import AdapterError  # noqa: E402
from adapters.claude_web_search import ClaudeWebSearchAdapter  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.models import NOT_OBSERVABLE, AskRequest, SearchMode  # noqa: E402

SMOKE_PROMPT = "中小团队用什么 CRM 好?"


def make_request(prompt: str, mode: SearchMode) -> AskRequest:
    return AskRequest(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        information_need_id="smoke-need",
        prompt_variant_id="smoke-variant",
        prompt_text=prompt,
        search_mode=mode,
        locale="zh-CN",
        timeout_s=120.0,  # 带真实检索的回答比纯生成慢
    )


def summarize(label: str, run) -> None:
    print(f"\n── {label} ──")
    print(f"  outcome={run.outcome}  search_mode={run.search_mode}  "
          f"latency={run.latency_ms}ms  cost=${run.cost_usd:.4f}")
    trace = run.search_trace
    print(f"  search_triggered={trace.search_triggered}")
    if trace.generated_queries != NOT_OBSERVABLE:
        print(f"  模型自拟检索词 {len(trace.generated_queries)} 条:")
        for query in trace.generated_queries:
            print(f"    「{query}」")
    if trace.retrieved_sources != NOT_OBSERVABLE:
        print(f"  检索候选 {len(trace.retrieved_sources)} 条,前 5:")
        for s in trace.retrieved_sources[:5]:
            print(f"    #{s.rank}  {s.domain}  {(s.title or '')[:50]}")
    if run.answer is not None:
        print(f"  回答(前 80 字):{run.answer.answer_text[:80]!r}")
        print(f"  citations_available={run.answer.citations_available},"
              f"引用 {len(run.answer.citations)} 条:")
        for c in run.answer.citations[:5]:
            print(f"    [{c.position}] {c.domain}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, default=None, help="原始返回体落盘目录")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY 未设置(.env 或环境变量)", file=sys.stderr)
        return 1
    print(f"使用 key:****{api_key[-4:]}")

    adapter = ClaudeWebSearchAdapter(api_key=api_key)
    exit_code = 0
    try:
        healthy = adapter.healthcheck()
        print(f"claude_web_search: healthcheck={'OK' if healthy else 'FAILED'}")
        legs = {
            "开卷(默认,真实检索)": SearchMode.DEFAULT,
            "闭卷(search_disabled)": SearchMode.SEARCH_DISABLED,
        }
        for label, mode in legs.items():
            run = adapter.ask(make_request(SMOKE_PROMPT, mode))
            summarize(label, run)
            if args.capture:
                args.capture.mkdir(parents=True, exist_ok=True)
                out = args.capture / f"claude_web_search.{mode.value}.json"
                out.write_text(
                    json.dumps(run.raw_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  原始返回已写入 {out}")
    except AdapterError as exc:
        exit_code = 1
        status = getattr(exc, "http_status", None)
        print(f"claude_web_search: 契约异常 {type(exc).__name__} http_status={status}: {exc}")
    finally:
        adapter.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
