"""Perplexity 生成+引用通道(chat/completions,sonar 系列)。

只做翻译:AskRequest → 平台调用;平台返回 → SurfaceRun(6.3)。

- Answer.citations 记录平台暴露的引用池(search_results / citations 字段,
  顺序即 position)。「正文 [n] 标记是否真的引到某条」属 L2 判定器,
  从 raw_payload 重算,此处不判定。
- 检索候选集在本通道不可观测:retrieved_sources 一律 NOT_OBSERVABLE,
  禁止用引用集反推检索(铁律 7);诊断层把检索阶段如实记不可观测。
- 显式拒答仅认机器可读信号 finish_reason=content_filter → outcome=REFUSED;
  从回答文本猜测"算不算拒答"属 L2 判定器,不做。
"""

from __future__ import annotations

from typing import Any

import httpx

from adapters.base import (
    AdapterError,
    AdapterPermanentError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from adapters.http_support import contract_post, domain_of
from engine.models import (
    NOT_OBSERVABLE,
    UNKNOWN,
    Answer,
    AskRequest,
    Citation,
    Outcome,
    RunError,
    RunErrorType,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.perplexity.ai"

_CAPABILITIES = CapabilityRegistry(supports_citations=True)



def _extract_citations(data: dict[str, Any]) -> tuple[bool, list[Citation]]:
    """平台以 search_results(带标题)或 citations(裸 URL 列表)暴露引用池。
    两个字段都不存在 → 本次响应未暴露引用(citations_available=False)。"""
    if "search_results" in data:
        return True, [
            Citation(url=item["url"], domain=domain_of(item["url"]), title=item.get("title"), position=i + 1)
            for i, item in enumerate(data["search_results"] or [])
        ]
    if "citations" in data:
        return True, [
            Citation(url=url, domain=domain_of(url), title=None, position=i + 1)
            for i, url in enumerate(data["citations"] or [])
        ]
    return False, []


def _cost_usd(data: dict[str, Any]) -> float:
    cost = data.get("usage", {}).get("cost")
    if isinstance(cost, dict) and "total_cost" in cost:
        return float(cost["total_cost"])
    return 0.0  # 平台未报成本时不臆造;系统性计费口径随调度器预算记账落地


class PerplexityAgentAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="perplexity",
            surface="perplexity_agent",
            display_name="Perplexity",
            api_name="Perplexity API",
            # 以官方模型页为准(2026-09-21 核对;sonar-deep-research 是另一种产品形态——
            # 单次分钟级、按检索次数另计费,不入清单)
            models=["sonar", "sonar-pro", "sonar-reasoning-pro"],
            default_model="sonar",
            capabilities=_CAPABILITIES,
            unit_prices={"request_usd": 0.005},  # 检索附加费近似值,待官方价目核对
            est_cost_per_call_usd={
                "sonar": 0.006,  # 口径:2026-08 实测均值(请求费 + 少量 token 费)
                # 口径:推导上界(官方价目页 2026-09-21:$3 / $15 每百万 token,低搜索上下文
                # 请求费 $6 / 千次):$0.006 + 2000/1e6×$15 ≈ $0.036,待实测校准
                "sonar-pro": 0.036,
                # 口径:推导上界(同页:$2 / $8 每百万 token,请求费 $6 / 千次;推理 token 计入输出):
                # $0.006 + 4000/1e6×$8 ≈ $0.038,待实测校准
                "sonar-reasoning-pro": 0.038,
            },
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        _CAPABILITIES.require_search_mode(request.search_mode)
        if request.region is not None:
            _CAPABILITIES.require("supports_region")
        model = request.model or self.meta().default_model
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt_text}],
        }
        data, latency_ms, started_at = contract_post(
            self._client,
            f"{BASE_URL}/chat/completions",
            headers=self._headers,
            payload=payload,
            timeout_s=request.timeout_s,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}") from exc

        citations_available, citations = _extract_citations(data)
        run_fields: dict[str, Any] = dict(
            run_id=request.run_id,
            prompt_id=request.prompt_id,
            run_index=request.run_index,
            provider="perplexity",
            surface="perplexity_agent",
            model=model,
            search_mode=request.search_mode,
            locale=request.locale,
            region=request.region,
            search_trace=SearchTrace(
                # 引用池非空 ⇒ 确实检索了;为空时无法区分"搜了没结果"与"没搜"
                search_triggered=True if citations else UNKNOWN,
                generated_queries=NOT_OBSERVABLE,
                retrieved_sources=NOT_OBSERVABLE,
                opened_sources=NOT_OBSERVABLE,
            ),
            answer=(
                Answer(answer_text=content, citations_available=citations_available, citations=citations)
                if content
                else None
            ),
            raw_payload=data,
            timestamp=started_at,
            latency_ms=latency_ms,
            cost_usd=_cost_usd(data),
        )
        if finish_reason == "content_filter":
            return SurfaceRun(
                outcome=Outcome.REFUSED,
                error=RunError(error_type=RunErrorType.REFUSAL, message="finish_reason=content_filter"),
                **run_fields,
            )
        return SurfaceRun(outcome=Outcome.SUCCESS, **run_fields)

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client,
                f"{BASE_URL}/chat/completions",
                headers=self._headers,
                payload={
                    "model": self.meta().default_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 16,  # 平台下限:max_tokens must be at least 16
                },
                timeout_s=15.0,
            )
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
