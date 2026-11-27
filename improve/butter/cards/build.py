"""出包时怎么建批次和卡(纯层):改动包里每条真要改的条目一张卡(决定 3、ADR-B06)。

条目的阶段、等级、假设、来源在建卡当时从注册表抄进卡里:以后注册表变了,卡上仍是当时的依据。
"""

from __future__ import annotations

from butter.contracts import Batch, CardSpec, ChangeItem, ChangePackage
from butter.registry.core import Registry

# 全站类改动:影响所有页面,同批的"未改页"不再干净(设计 §8.1)
SITEWIDE_KINDS = frozenset({"robots", "jsonld_organization", "fact_page"})


def card_spec(item: ChangeItem, registry: Registry, *, batch_id: str, industry: str = "", lang: str = "zh") -> CardSpec:
    entry = registry.get(item.entry_id)
    return CardSpec(
        card_id=f"{batch_id}/{item.item_id}", batch_id=batch_id, change_item_id=item.item_id, entry_id=item.entry_id,
        kind=item.kind, origin=item.origin, target=item.target, before=item.before, after=item.after,
        sitewide=item.kind in SITEWIDE_KINDS, industry=industry, entry_stage=entry.stage, entry_kind=entry.kind,
        entry_level=entry.evidence_level, entry_hypothesis=entry.hypothesis.get(lang) or entry.hypothesis["zh"],
        entry_sources=list(entry.sources.get(lang) or entry.sources["zh"]))


def plan_batch(package: ChangePackage, registry: Registry, *, batch_id: str, site_host: str, industry: str = "",
               lang: str = "zh") -> tuple[Batch, list[CardSpec]]:
    """改动包 → 一个批次 + 每条改动一张卡。action 为 none 的条目是"现状已经可以,不用改",不建卡。"""
    specs = [card_spec(item, registry, batch_id=batch_id, industry=industry, lang=lang)
             for item in package.items if item.action != "none"]
    batch = Batch(batch_id=batch_id, site_host=site_host, run_id=package.run_id, card_ids=[s.card_id for s in specs])
    return batch, specs
