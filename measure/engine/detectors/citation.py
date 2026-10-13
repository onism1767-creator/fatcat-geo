"""引用判定器 v4:品牌域名与引用集的域名匹配,外加官方渠道的「主机名 + 路径前缀」匹配。

匹配规则见 engine/detectors/matching.py(与检索排名判定器共享);
规则变更需提版本号并全量重算。
v2(v0.4.26 S3):空回答(SUCCESS 但正文为空)不可判定——有效回答的口径全项目一处定(engine/validity.py)。
v3(v0.4.26 S4):引用链接先还原跳转——跳转仪器(能力 citation_urls_redirect)的引用按终点链接判,
还原失败的那条记「看不到」(不进分子分母,只计数);命中的注册域名与判定用链接一并记录,
供主站引用率、按域名拆分与独立核验。
v4(v0.4.26 S5):官方渠道(电商旗舰店、官方账号)按 BrandSpec.official_channels 的「主机名 + 路径前缀」精确匹配,
记在 matched_channels,单列、不算官网被引(产品文档 15.2:整个 tmall.com 不能都算成官网)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from engine.detectors.matching import matches_any_brand_domain, registrable
from engine.models import BrandSpec, CitationObservation, SurfaceRun
from engine.resolve import ResolutionContext
from engine.validity import is_valid_answer

DETECTOR_NAME = "citation_domain_match"
DETECTOR_VERSION = 4


def channel_hit(url: str, prefixes: list[str]) -> str | None:
    """链接命中哪条官方渠道(主机名 + 路径前缀,按路径段整段匹配):brand.tmall.com/ 命中 brand.tmall.com/item/1,
    www.instagram.com/brand 命中 /brand 与 /brand/p/1,不命中 /brandx。"""
    parts = urlsplit(url)
    key = f"{(parts.hostname or '').lower()}{parts.path or '/'}"
    for prefix in prefixes:
        if key == prefix or key.startswith(prefix.rstrip("/") + "/"):
            return prefix
    return None


class CitationDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def __init__(self, context: ResolutionContext | None = None) -> None:
        self._context = context or ResolutionContext()

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> CitationObservation | None:
        """品牌域名是否进入引用集。以下情形不可判定,返回 None(不产出观测):
        - 没有有效回答(拒答 / 失败 / 空回答);
        - 该 surface 未暴露引用(citations_available=False):
          「引用集为空」与「看不到引用集」是两回事,不得混判为"未被引";
        - 品牌未提供域名(无判定依据)。"""
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        if not run.answer.citations_available or not brand.domains:
            return None
        positions: list[int] = []
        matched_domains: list[str] = []
        matched_urls: list[str] = []
        matched_channels: list[str] = []
        unresolved = 0
        for citation in run.answer.citations:
            url = self._context.final_url(run.surface, citation.url)
            if url is None:  # 跳转链还原失败:这条引用看不到,不猜
                unresolved += 1
                continue
            host = citation.domain if url == citation.url else (urlsplit(url).hostname or "")
            if matches_any_brand_domain(host, brand):
                positions.append(citation.position)
                matched_domains.append(registrable(host)[1])
                matched_urls.append(url)
                continue
            channel = channel_hit(url, brand.official_channels)
            if channel is not None:
                matched_channels.append(channel)
        return CitationObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            cited=bool(positions),
            positions=positions,
            matched_domains=list(dict.fromkeys(matched_domains)),
            matched_urls=matched_urls,
            matched_channels=list(dict.fromkeys(matched_channels)),
            unresolved=unresolved,
        )
