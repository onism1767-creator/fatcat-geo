"""L2 官网候选:在 AI 搜到 / 引用过的网址里,找「可能也是你官网」的网址(如各地区官网),供用户确认。

只是提示,不自动生效——官网名单只认用户确认的(架构书附录 v0.4.24 D4)。
规则:注册域名的主体与已登记官网相同、且还不在名单里。例:登记 arcteryx.com 时,arcteryx.jp、
arcteryx.com.au 是候选;arcteryx.pissedconsumer.com 的注册域名是 pissedconsumer.com,不是候选。
注册域名按公共后缀表算(engine/detectors/matching.py::registrable,离线快照)。
跳转仪器的引用按还原后的终点链接取主机(v0.4.26 S4);还原失败的那条看不到,不列。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from engine.detectors.matching import matches_any_brand_domain, registrable, url_host
from engine.models import BrandSpec, FrozenModel, SurfaceRun
from engine.resolve import ResolutionContext


class DomainCandidate(FrozenModel):
    domain: str  # 注册域名,如 arcteryx.jp
    count: int  # 在搜索结果与引用里出现的次数(逐条计)


def _hosts(run: SurfaceRun, context: ResolutionContext) -> list[str]:
    sources = run.search_trace.retrieved_sources
    hosts = [s.domain for s in sources] if isinstance(sources, list) else []
    if run.answer is not None:
        for citation in run.answer.citations:
            url = context.final_url(run.surface, citation.url)
            if url is None:
                continue  # 跳转链还原失败:看不到去向,不猜
            hosts.append(citation.domain if url == citation.url else url_host(url))
    return hosts


def official_candidates(
    runs: Iterable[SurfaceRun], brand: BrandSpec, context: ResolutionContext | None = None,
) -> list[DomainCandidate]:
    """按出现次数从多到少。已在官网名单里的(含其子域)不列。"""
    context = context or ResolutionContext()
    labels = {registrable(domain)[0] for domain in brand.domains}
    counts: Counter[str] = Counter()
    for run in runs:
        for host in _hosts(run, context):
            if not host or matches_any_brand_domain(host, brand):
                continue
            label, domain = registrable(host)
            if label in labels and domain:
                counts[domain] += 1
    return [DomainCandidate(domain=domain, count=n) for domain, n in counts.most_common()]
