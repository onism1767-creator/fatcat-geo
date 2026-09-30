"""L4 表单 → 领域对象:首页建任务、确认页定下要测什么。解析与校验都在这里,路由只管收发。

校验不过返回错误码(文案在 report/i18n.py,随界面语言),代码里不硬写给用户看的句子。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from adapters.base import SurfaceAdapter
from engine.models import BrandSpec, FrozenModel, InformationNeed, NeedIntent, Target
from engine.needs import NeedCandidate, manual_intent
from report.jobs import TIERS, DiagnosisJob

MAX_NEEDS = 10
MAX_VARIANTS = 5


def lines_of(text: str) -> list[str]:
    """多行输入框:每行一条,空行不算。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _comma_list(text: str) -> list[str]:
    """逗号分开的一串(中英文逗号都认),去空。"""
    return [part.strip() for part in text.replace(",", ",").split(",") if part.strip()]


def _host(url: str) -> str:
    """网址 → 主机名(不带协议就补 https;大小写不敏感)。"""
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


def brand_from_form(
    brand_name: str, aliases: str, website: str, other_sites: str = ""
) -> tuple[BrandSpec, str]:
    """首页填的品牌 → (品牌, 规范化后的官网地址)。官网名单 = 主官网 + 用户另填的其他官网(如各地区官网),
    只认用户填的(v0.4.24 D4)。"""
    normalized = website if "://" in website else f"https://{website}"
    domains = [host for host in [_host(website), *map(_host, _comma_list(other_sites))] if host]
    brand = BrandSpec(
        name=brand_name.strip(),
        aliases=_comma_list(aliases),
        domains=list(dict.fromkeys(domains)),
    )
    return brand, normalized


def manual_candidates(questions: str, brand: BrandSpec) -> list[NeedCandidate]:
    """用户手填的问题,按是否点名品牌归场景。"""
    return [NeedCandidate(intent=manual_intent(text, brand), text=text) for text in lines_of(questions)]


def preselect_candidates(candidates: list[NeedCandidate], *, n_manual: int) -> list[int]:
    """确认页默认勾哪几道题(v0.4.18):用户自己填的(列表末尾 n_manual 道)全勾;
    一道没填就只勾 2 道——第一个不点名品牌的题 + 第一个直接问品牌的题。
    默认报价必须便宜,想多测由用户自己勾(预估实时更新)。"""
    if n_manual:
        return list(range(len(candidates) - n_manual, len(candidates)))
    first_open = next((i for i, c in enumerate(candidates) if c.intent is not NeedIntent.BRAND_PROBE), None)
    first_brand = next((i for i, c in enumerate(candidates) if c.intent is NeedIntent.BRAND_PROBE), None)
    return [i for i in (first_open, first_brand) if i is not None]


def selected_targets(keys: list[str], adapters: dict[str, SurfaceAdapter]) -> list[Target]:
    """表单勾选的被测对象,只认当前清单里真实存在的(同序)。"""
    return [
        target for target in map(Target.parse, keys)
        if target.surface in adapters and target.model in adapters[target.surface].meta().models
    ]


def count_needs(job: DiagnosisJob, needs_selected: list[int], custom: str) -> int:
    """确认页当前勾了几道题(预估用;与 confirm_choice 同一口径)。"""
    return sum(0 <= i < len(job.candidates) for i in needs_selected) + len(lines_of(custom))


class ConfirmChoice(FrozenModel):
    """确认页定下的全部口径,原样交给 DiagnosisJob.freeze。"""

    needs: list[InformationNeed]
    competitors: list[BrandSpec]
    targets: list[Target]
    tier_id: str
    variants_per_need: int
    budget_usd: float | None
    dependency_enabled: bool


def confirm_choice(
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    *,
    needs_selected: list[int],
    custom: str,
    competitors_selected: list[int],
    targets_selected: list[str],
    tier: str,
    variants: int,
    budget: str,
    dependency: str,
) -> ConfirmChoice | str:
    """确认页表单 → 口径;不合格返回错误码。"""
    needs = [
        InformationNeed(statement=job.candidates[i].text, intent=job.candidates[i].intent)
        for i in needs_selected if 0 <= i < len(job.candidates)
    ] + [
        InformationNeed(statement=text, intent=manual_intent(text, job.brand)) for text in lines_of(custom)
    ]
    if not 1 <= len(needs) <= MAX_NEEDS:
        return "err_needs_count"
    targets = selected_targets(targets_selected, adapters)
    if not targets:
        return "err_no_target"
    try:
        budget_usd = float(budget) if budget.strip() else None
    except ValueError:
        return "err_bad_params"
    if tier not in TIERS or not 1 <= variants <= MAX_VARIANTS or (budget_usd is not None and budget_usd <= 0):
        return "err_bad_params"
    competitors = [
        BrandSpec(name=c.name, domains=[c.domain])
        for i in competitors_selected if 0 <= i < len(job.competitors_suggested)
        for c in [job.competitors_suggested[i]]
    ]
    return ConfirmChoice(needs=needs, competitors=competitors, targets=targets, tier_id=tier,
                         variants_per_need=variants, budget_usd=budget_usd,
                         dependency_enabled=bool(dependency))
