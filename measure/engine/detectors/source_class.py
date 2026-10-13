"""来源归属判定器 v1:一次回答接触的每个网页(引用、搜索结果)归入且只归入一类(架构书附录 v0.4.24 D1;S6.1)。

| 类 | 判定 |
|---|---|
| 你的官网 | 终点链接的主机在官网名单里(S5 名单,含用户确认的) |
| 官方渠道 | 命中官网名单里的官方渠道(主机名 + 路径前缀),单列 |
| 竞品官网 | 主机在某竞品的官网名单里(记是哪个竞品) |
| 写到你的第三方网页 | 以上都不是,且打开后正文里写到你(engine/detectors/page_mention.py) |
| 没写到你的其他网页 | 打开了、正文可读,没写到你 |
| 看不到 | 跳转还原失败 / 没来得及开 / 打不开 / 抽不出正文……原因照记,不进分母(铁律 7) |

跳转仪器的引用先按还原后的终点判(S4)。判定用的是存档的网页正文,规则升级可重算。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from engine.detectors.citation import channel_hit
from engine.detectors.matching import matches_any_brand_domain
from engine.detectors.page_mention import page_mention
from engine.models import (
    BrandSpec,
    PageFetchStatus,
    SourceAttribution,
    SourceAttributionObservation,
    SourceClass,
    SurfaceRun,
)
from engine.page_check import PageIndex
from engine.resolve import ResolutionContext
from engine.validity import is_valid_answer

DETECTOR_NAME = "source_attribution"
DETECTOR_VERSION = 1

REASON_UNRESOLVED = "UNRESOLVED"  # 跳转链还原失败
REASON_NOT_ATTEMPTED = "NOT_ATTEMPTED"  # 预算到顶,没来得及开


def host_class(host: str, brand: BrandSpec, competitors: list[BrandSpec]) -> tuple[SourceClass, str | None] | None:
    """光看主机名能定的三类:你的官网 / 官方渠道 / 竞品官网;None = 第三方,要看正文。"""
    if matches_any_brand_domain(host, brand):
        return SourceClass.OWN_SITE, None
    for competitor in competitors:
        if matches_any_brand_domain(host, competitor):
            return SourceClass.COMPETITOR_SITE, competitor.name
    return None


def url_host_class(url: str, brand: BrandSpec, competitors: list[BrandSpec]) -> tuple[SourceClass, str | None] | None:
    host = urlsplit(url).hostname or ""
    if channel_hit(url, brand.official_channels) is not None:
        return SourceClass.OFFICIAL_CHANNEL, None
    return host_class(host, brand, competitors)


class SourceClassifier:
    """按一单的上下文(官网名单、竞品、跳转还原、网页核实结果)归类来源。"""

    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def __init__(
        self,
        competitors: list[BrandSpec],
        resolution: ResolutionContext | None = None,
        pages: PageIndex | None = None,
    ) -> None:
        self._competitors = competitors
        self._resolution = resolution or ResolutionContext()
        self._pages = pages or PageIndex()

    def classify(self, surface: str, url: str, brand: BrandSpec) -> SourceAttribution:
        final = self._resolution.final_url(surface, url)
        if final is None:
            return SourceAttribution(url=url, source_class=SourceClass.NOT_OBSERVABLE, reason=REASON_UNRESOLVED)
        known = url_host_class(final, brand, self._competitors)
        if known is not None:
            source_class, competitor = known
            return SourceAttribution(url=url, final_url=final, source_class=source_class, competitor=competitor)
        check = self._pages.lookup(final)
        if check is None:
            return SourceAttribution(url=url, final_url=final, source_class=SourceClass.NOT_OBSERVABLE,
                                     reason=REASON_NOT_ATTEMPTED)
        if check.status is not PageFetchStatus.OK:
            return SourceAttribution(url=url, final_url=final, source_class=SourceClass.NOT_OBSERVABLE,
                                     reason=check.status.value)
        mention = page_mention(check, brand)
        # 存档行(S6.4):判定照常,但记时间戳与实时状态——聚合时不进实时比例、按实时原因计看不到、另给存档一行
        archived_at = check.archive_timestamp
        reason = check.live_status.value if archived_at and check.live_status is not None else None
        if mention is None or not mention.found:
            return SourceAttribution(url=url, final_url=final, source_class=SourceClass.OTHER,
                                     text_source=check.text_source, archived_at=archived_at, reason=reason)
        return SourceAttribution(url=url, final_url=final, source_class=SourceClass.THIRD_PARTY_MENTIONS,
                                 position=mention.position.value if mention.position else None,
                                 text_source=check.text_source, archived_at=archived_at, reason=reason)

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> SourceAttributionObservation | None:
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        cited = (
            [self.classify(run.surface, c.url, brand) for c in run.answer.citations]
            if run.answer.citations_available else None
        )
        sources = run.search_trace.retrieved_sources
        retrieved = (
            [self.classify(run.surface, s.url, brand) for s in sources] if isinstance(sources, list) else None
        )
        return SourceAttributionObservation(
            run_id=run.run_id, detector_name=self.name, detector_version=self.version,
            computed_at=datetime.now(UTC), cited=cited, retrieved=retrieved,
        )
