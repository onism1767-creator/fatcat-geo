"""手动 smoke:对 Perplexity 生成+引用接口打一发真实请求。

12.3:真实 API 调用只存在于手动 smoke 脚本与 golden payload 更新流程,
本脚本就是前者,永不进 CI。

用法:
    .venv\\Scripts\\python scripts\\smoke_perplexity.py [--capture DIR]

--capture DIR 时把原始返回体写入该目录(golden payload 更新流程的输入,
入库前须人工 diff 审查)。key 从 .env / 环境变量读取,输出中一律脱敏。
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
from adapters.perplexity_agent import PerplexityAgentAdapter  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.models import AskRequest  # noqa: E402

SMOKE_PROMPT = "中小团队用什么 CRM 好?"
SURFACE = "perplexity_agent"


def make_request(prompt: str) -> AskRequest:
    return AskRequest(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        information_need_id="smoke-need",
        prompt_variant_id="smoke-variant",
        prompt_text=prompt,
        locale="zh-CN",
        timeout_s=60.0,
    )


def summarize(run) -> None:
    print(f"\n── {SURFACE} ──")
    print(f"  outcome={run.outcome}  latency={run.latency_ms}ms  cost=${run.cost_usd:.4f}")
    if run.answer is not None:
        print(f"  回答(前 80 字):{run.answer.answer_text[:80]!r}")
        print(f"  citations_available={run.answer.citations_available},引用 {len(run.answer.citations)} 条:")
        for c in run.answer.citations[:5]:
            print(f"    [{c.position}] {c.domain}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, default=None, help="原始返回体落盘目录")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        print("PERPLEXITY_API_KEY 未设置(.env 或环境变量)", file=sys.stderr)
        return 1
    print(f"使用 key:****{api_key[-4:]}")

    adapter = PerplexityAgentAdapter(api_key=api_key)
    try:
        healthy = adapter.healthcheck()
        print(f"{SURFACE}: healthcheck={'OK' if healthy else 'FAILED'}")
        run = adapter.ask(make_request(SMOKE_PROMPT))
        summarize(run)
        if args.capture:
            args.capture.mkdir(parents=True, exist_ok=True)
            out = args.capture / f"{SURFACE}.json"
            out.write_text(json.dumps(run.raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  原始返回已写入 {out}")
    except AdapterError as exc:
        status = getattr(exc, "http_status", None)
        print(f"{SURFACE}: 契约异常 {type(exc).__name__} http_status={status}: {exc}")
        return 1
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
