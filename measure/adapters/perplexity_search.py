"""Perplexity 裸检索通道(Search API):检索排名观测。

只做翻译:AskRequest.prompt_text → 检索查询;平台返回的有序结果 →
SearchTrace.retrieved_sources(rank = 平台给出的位次,1 起)。

这是全链唯一直接暴露检索排名的通道(P1 双通道对照的一半)。
注意(架构书第 14 章风险 5):官方不保证该候选集与消费端一致,
只当观测通道,不当真值,报告须注明口径。

该 surface 无生成回答、无引用、无拒答概念、不暴露模型版本
(model 记为 "unversioned")。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from adapters.base import (
    AdapterError,
    AdapterPermanentError,
    CapabilityRegistry,
    SurfaceAdapter,
    SurfaceMeta,
)
from adapters.http_support import contract_post
from engine.models import (
    NOT_OBSERVABLE,
    AskRequest,
    Outcome,
    RetrievedSource,
    SearchTrace,
    SurfaceRun,
)

BASE_URL = "https://api.perplexity.ai"
MAX_RESULTS = 20  # 检索排名观测窗口:前 20(与 RETRIEVAL 诊断口径一致)

_CAPABILITIES = CapabilityRegistry(supports_retrieval_rank=True)
_MODEL_LABEL = "unversioned"  # 该 surface 不暴露模型/索引版本


class PerplexitySearchAdapter(SurfaceAdapter):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(
            provider="perplexity",
            surface="perplexity_search",
            display_name="Perplexity Search API",
            models=[_MODEL_LABEL],
            default_model=_MODEL_LABEL,
            capabilities=_CAPABILITIES,
            unit_prices={"request_usd": 0.005},  # 按每千次请求价目折算,待官方价目核对
            est_cost_per_call_usd=0.005,  # 口径:按请求计费,固定价即期望值
        )

    def ask(self, request: AskRequest) -> SurfaceRun:
        _CAPABILITIES.require_search_mode(request.search_mode)
        if request.region is not None:
            _CAPABILITIES.require("supports_region")
        if request.model not in (None, _MODEL_LABEL):
            raise AdapterPermanentError(f"该 surface 不提供模型选择,收到 model={request.model!r}")
        payload = {"query": request.prompt_text, "max_results": MAX_RESULTS}
        data, latency_ms, started_at = contract_post(
            self._client,
            f"{BASE_URL}/search",
            headers=self._headers,
            payload=payload,
            timeout_s=request.timeout_s,
        )
        try:
            sources = [
                RetrievedSource(
                    url=item["url"],
                    domain=urlsplit(item["url"]).hostname or "",
                    title=item.get("title"),
                    rank=i + 1,  # 平台返回按相关性排序,位次即排名
                )
                for i, item in enumerate(data["results"])
            ]
        except (KeyError, TypeError) as exc:
            raise AdapterPermanentError(f"响应结构不符合预期: {exc!r}") from exc

        return SurfaceRun(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="perplexity",
            surface="perplexity_search",
            model=_MODEL_LABEL,
            search_mode=request.search_mode,
            locale=request.locale,
            region=request.region,
            outcome=Outcome.SUCCESS,  # 结果为空也是 SUCCESS:真实负观测
            search_trace=SearchTrace(
                search_triggered=True,
                generated_queries=NOT_OBSERVABLE,
                retrieved_sources=sources,
                opened_sources=NOT_OBSERVABLE,
            ),
            answer=None,  # 该通道无生成回答
            raw_payload=data,
            timestamp=started_at,
            latency_ms=latency_ms,
            cost_usd=self.meta().unit_prices["request_usd"],
        )

    def healthcheck(self) -> bool:
        try:
            contract_post(
                self._client,
                f"{BASE_URL}/search",
                headers=self._headers,
                payload={"query": "ping", "max_results": 1},
                timeout_s=15.0,
            )
        except AdapterError:
            return False
        return True

    def close(self) -> None:
        self._client.close()
