"""引用独立核验:三档证据「提及 → 被引 → 已核验」的第三档(v0.4.2 附录条目 1)。

不轻信平台引用:对被引 URL 自行重抓,核验 (a) 可达 (b) 可见正文确实
出现品牌清晰名——「HTML 可达但正文无品牌」是软 404/张冠李戴的典型指纹,
是唯一的负面证据(Potato 实测教训)。

核验结果四态 + 一(v0.4.13 条目 5):抓不到 ≠ 引用无效——核验器与 ACCESS 探针
同样是「本工具客户端在此条件下得到的响应」,被机器人防护拦截(FETCH_BLOCKED)、
非 HTML 未解析(UNPARSED)、其余失败(UNREACHABLE)都只是核验器缺数据,
不得解读为平台引用错标。

- 与 access_audit 同类:自带爬取器的诊断仪器(L3),存储走 L2 Storage;
- 抓回页面一律视为不可信数据:只做机械文本匹配,绝不回喂任何模型(防注入);
- 按规范化 URL 去重,时效窗口内复用历史核验(只追加的 citation_evidence 表);
  缓存只在「当时查过的品牌集合覆盖本次品牌」时才可复用——没查过的品牌
  缺席不可解读;
- 每次抓取(含失败)都落一条证据记录,失败不蒸发。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from diagnosis.access_audit import BROWSER_UA, PROTECTION_SUSPECT_STATUSES, extract_visible_text
from engine.detectors.matching import find_first_alias
from engine.models import BrandSpec, CitationEvidence

log = structlog.get_logger("diagnosis.citation_verify")

TIMEOUT_S = 15.0


class EvidenceStore(Protocol):
    def insert_citation_evidence(self, evidence: CitationEvidence) -> None: ...
    def recent_citation_evidence(
        self, normalized_urls: list[str], *, max_age_days: int = 7
    ) -> dict[str, CitationEvidence]: ...


def normalize_url(url: str) -> str:
    """去 fragment、主机小写;保留 www 与查询串(规范化后必须仍可抓取)。"""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


class VerificationOutcome(StrEnum):
    """一条被引 URL 对某品牌的核验结果。只有 CONTENT_MISMATCH 是负面证据;
    FETCH_BLOCKED / UNPARSED / UNREACHABLE 是核验器缺数据(不可观测),不进任何负面口径。"""

    VERIFIED_MATCH = "VERIFIED_MATCH"      # HTML 可达且可见正文出现品牌清晰名
    CONTENT_MISMATCH = "CONTENT_MISMATCH"  # HTML 可达、查过该品牌、正文未见——软 404/错标指纹
    FETCH_BLOCKED = "FETCH_BLOCKED"        # 401/403/429/503:疑似机器人防护拦截本工具客户端
    UNPARSED = "UNPARSED"                  # 可达但非 HTML(如 PDF),v1 不解析
    UNREACHABLE = "UNREACHABLE"            # 其余非成功状态或网络层失败(404、超时、DNS…)


def classify(evidence: CitationEvidence, brand_name: str) -> VerificationOutcome:
    """把一条核验记录判为四态之一。没查过该品牌的记录不可解读——那是调用方缓存复用的 bug。"""
    if brand_name not in evidence.checked_brands:
        raise ValueError(
            f"核验记录未查过品牌 {brand_name},缺席不可解读:{evidence.normalized_url}"
        )
    if evidence.reachable:
        if evidence.visible_chars is None:
            return VerificationOutcome.UNPARSED
        if brand_name in evidence.brands_found:
            return VerificationOutcome.VERIFIED_MATCH
        return VerificationOutcome.CONTENT_MISMATCH
    if evidence.http_status in PROTECTION_SUSPECT_STATUSES:
        return VerificationOutcome.FETCH_BLOCKED
    return VerificationOutcome.UNREACHABLE


class CitationVerifier:
    def __init__(
        self,
        storage: EvidenceStore,
        *,
        client: httpx.Client | None = None,
        max_age_days: int = 7,
    ) -> None:
        self._storage = storage
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
        self._max_age_days = max_age_days
        self._memory: dict[str, CitationEvidence] = {}

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def verify(
        self, urls: list[str], brands: list[BrandSpec]
    ) -> dict[str, CitationEvidence]:
        """核验一批 URL,返回 {规范化 URL: 证据}。逐 URL 只抓一次(内存+库缓存)。"""
        checked_names = [b.name for b in brands]
        wanted = {normalize_url(u): u for u in urls}
        result: dict[str, CitationEvidence] = {}

        missing = [n for n in wanted if n not in self._memory]
        cached = self._storage.recent_citation_evidence(missing, max_age_days=self._max_age_days)
        for normalized, evidence in {**self._memory, **cached}.items():
            # 缓存只在其查验品牌集合覆盖本次时可复用
            if normalized in wanted and set(checked_names) <= set(evidence.checked_brands):
                result[normalized] = evidence

        for normalized, original in wanted.items():
            if normalized in result:
                continue
            evidence = self._fetch(original, normalized, brands, checked_names)
            self._storage.insert_citation_evidence(evidence)
            result[normalized] = evidence
        self._memory.update(result)
        return result

    def _fetch(
        self,
        url: str,
        normalized: str,
        brands: list[BrandSpec],
        checked_names: list[str],
    ) -> CitationEvidence:
        try:
            response = self._client.get(normalized, headers={"User-Agent": BROWSER_UA})
        except httpx.HTTPError as exc:
            log.warning("citation_verify_fetch_failed", url=normalized, error=type(exc).__name__)
            return CitationEvidence(
                url=url, normalized_url=normalized, fetched_at=datetime.now(UTC),
                reachable=False, checked_brands=checked_names, brands_found=[],
                error=type(exc).__name__,
            )
        reachable = response.is_success
        brands_found: list[str] = []
        visible_chars: int | None = None
        if reachable and "html" in response.headers.get("content-type", ""):
            text = extract_visible_text(response.text)
            visible_chars = len(text)
            brands_found = [b.name for b in brands if find_first_alias(text, b) is not None]
        return CitationEvidence(
            url=url, normalized_url=normalized, fetched_at=datetime.now(UTC),
            reachable=reachable, http_status=response.status_code,
            final_url=str(response.url), checked_brands=checked_names,
            brands_found=brands_found, visible_chars=visible_chars,
        )
