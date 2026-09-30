"""L2 官网候选:在 AI 搜到 / 引用过的网址里,找「可能也是你官网」的网址(如各地区官网),供用户确认。

只是提示,不自动生效——官网名单只认用户确认的(架构书附录 v0.4.24 D4)。
规则:可注册域名的主体与已登记官网相同、且还不在名单里。例:登记 arcteryx.com 时,arcteryx.jp、
arcteryx.com.au 是候选;arcteryx.pissedconsumer.com 的可注册域名是 pissedconsumer.com,不是候选。
公共后缀表用 tldextract 内置快照,完全离线(不联网下载、不写缓存)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import tldextract

from engine.detectors.matching import matches_any_brand_domain
from engine.models import BrandSpec, FrozenModel, SurfaceRun

_EXTRACT = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


class DomainCandidate(FrozenModel):
    domain: str  # 可注册域名,如 arcteryx.jp
    count: int  # 在搜索结果与引用里出现的次数(逐条计)


def registrable(host: str) -> tuple[str, str]:
    """(主体, 可注册域名):www.arcteryx.com.tw → (arcteryx, arcteryx.com.tw)。"""
    parts = _EXTRACT(host)
    return parts.domain.lower(), parts.top_domain_under_public_suffix.lower()


def _hosts(run: SurfaceRun) -> list[str]:
    sources = run.search_trace.retrieved_sources
    hosts = [s.domain for s in sources] if isinstance(sources, list) else []
    if run.answer is not None:
        hosts += [c.domain for c in run.answer.citations]
    return hosts


def official_candidates(runs: Iterable[SurfaceRun], brand: BrandSpec) -> list[DomainCandidate]:
    """按出现次数从多到少。已在官网名单里的(含其子域)不列。"""
    labels = {registrable(domain)[0] for domain in brand.domains}
    counts: Counter[str] = Counter()
    for run in runs:
        for host in _hosts(run):
            if not host or matches_any_brand_domain(host, brand):
                continue
            label, domain = registrable(host)
            if label in labels and domain:
                counts[domain] += 1
    return [DomainCandidate(domain=domain, count=n) for domain, n in counts.most_common()]
