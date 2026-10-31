"""手动 smoke:型号入清单的门槛(12.3:真实 API 调用只在手动脚本里,永不进 CI)。

v0.4.19 起每家 AI 的全部型号一起测;型号进适配器清单前,必须用该适配器钉死的请求形状
对它打一发真实联网提问,确认三件事(架构书附录 v0.4.6 条目 2 的教训:新版检索工具吞过引用):
  1. 请求形状被接受(不是 4xx);
  2. 观测窗还在——检索词 / 候选 / 引用按该 surface 的能力声明能看见;
  3. 回答没有被我们的长度保险丝截断(带思考的型号,思考 token 也占输出上限)。
本脚本只打印证据,入不入清单由人判断。样本不落库(冒烟不是观测)。

用法:
    .venv\\Scripts\\python scripts\\smoke_models.py                      # 清单里的全部型号
    .venv\\Scripts\\python scripts\\smoke_models.py --only claude-opus-5-5 gpt-6-astra
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.base import AdapterError  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from engine.models import NOT_OBSERVABLE, AskRequest, SearchMode  # noqa: E402

SMOKE_PROMPT = "中小团队用什么 CRM 好?"
_STOP_KEYS = ("stop_reason", "status", "incomplete_details", "finish_reason")


def _stop_signals(payload: object, found: dict[str, object]) -> dict[str, object]:
    """从原始返回里把各平台的终止信号捞出来(截断与否只看平台自己怎么说,不猜)。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _STOP_KEYS and value is not None and not isinstance(value, (dict, list)):
                found[key] = value
            elif key == "incomplete_details" and value:
                found[key] = value
            else:
                _stop_signals(value, found)
    elif isinstance(payload, list):
        for item in payload:
            _stop_signals(item, found)
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None, help="只测这些型号名")
    args = parser.parse_args()

    load_env_file()
    adapters = build_adapters(os.environ)
    total_cost = 0.0
    print(f"{'surface':<20} {'model':<24} {'结果':<10} {'检索词':>5} {'候选':>5} {'引用':>5} "
          f"{'回答字数':>7} {'花费':>8} {'耗时':>7}  终止信号")
    try:
        for name, adapter in adapters.items():
            for model in (*adapter.meta().models, *adapter.meta().helper_models):
                if args.only is not None and model not in args.only:
                    continue
                request = AskRequest(
                    run_id=f"smoke-{uuid.uuid4().hex[:8]}", prompt_id="smoke-prompt",
                    run_index=1, prompt_text=SMOKE_PROMPT,
                    search_mode=SearchMode.DEFAULT, model=model, locale="zh-CN", timeout_s=300.0,
                )
                try:
                    run = adapter.ask(request)
                except AdapterError as exc:
                    status = getattr(exc, "http_status", None)
                    body = (getattr(exc, "response_body", None) or "")[:300]
                    print(f"{name:<20} {model:<24} 契约异常 {type(exc).__name__} http={status}: {exc} {body}")
                    continue
                trace = run.search_trace
                queries = "看不见" if trace.generated_queries == NOT_OBSERVABLE else len(trace.generated_queries)
                sources = "看不见" if trace.retrieved_sources == NOT_OBSERVABLE else len(trace.retrieved_sources)
                answer = run.answer
                cited = ("看不见" if answer is not None and not answer.citations_available
                         else (len(answer.citations) if answer else 0))
                total_cost += run.cost_usd
                print(f"{name:<20} {model:<24} {run.outcome.value:<10} {queries!s:>5} {sources!s:>5} "
                      f"{cited!s:>5} "
                      f"{(len(answer.answer_text) if answer else 0):>7} ${run.cost_usd:>7.4f} "
                      f"{run.latency_ms / 1000:>6.1f}s  {_stop_signals(run.raw_payload, {})}")
    finally:
        for adapter in adapters.values():
            adapter.close()
    print(f"\n合计花费 ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
