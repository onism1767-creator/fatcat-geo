"""L4 表单 → 领域对象:首页建任务、确认页定下要测什么。解析与校验都在这里,路由只管收发。

校验不过返回错误码(文案在 report/i18n.py,随界面语言),代码里不硬写给用户看的句子。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from adapters.base import SurfaceAdapter
from engine.drafting import PromptCandidate, manual_intent
from engine.models import BrandSpec, FrozenModel, Prompt, PromptOrigin, Target
from report.jobs import DiagnosisJob

MAX_PROMPTS = 40  # 产品文档默认 24 道;用户可增删,封顶 40


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
    只认用户填的(v0.4.24 D4;S5 改为自动发现后撤掉其他官网输入框)。"""
    normalized = website if "://" in website else f"https://{website}"
    domains = [host for host in [_host(website), *map(_host, _comma_list(other_sites))] if host]
    brand = BrandSpec(
        name=brand_name.strip(),
        aliases=_comma_list(aliases),
        domains=list(dict.fromkeys(domains)),
    )
    return brand, normalized


def manual_candidates(questions: str, brand: BrandSpec) -> list[PromptCandidate]:
    """用户手填的题,按是否点名品牌归题型。"""
    return [
        PromptCandidate(origin=PromptOrigin.USER, intent=manual_intent(text, brand), text=text)
        for text in lines_of(questions)
    ]


def preselect_candidates(candidates: list[PromptCandidate]) -> list[int]:
    """确认页默认勾哪几道题:全部(产品文档 15.2 默认 24 道;不想测的由用户自己去掉,预估实时更新)。"""
    return list(range(len(candidates)))


def default_targets(adapters: dict[str, SurfaceAdapter]) -> set[str]:
    """确认页默认勾的被测对象:每家只勾一个当前主力型号(产品文档 D17),其余用户自己勾。"""
    return {Target(surface=name, model=adapter.meta().default_model).key for name, adapter in adapters.items()}


def selected_targets(keys: list[str], adapters: dict[str, SurfaceAdapter]) -> list[Target]:
    """表单勾选的被测对象,只认当前清单里真实存在的(同序)。"""
    return [
        target for target in map(Target.parse, keys)
        if target.surface in adapters and target.model in adapters[target.surface].meta().models
    ]


def count_prompts(job: DiagnosisJob, prompts_selected: list[int], custom: str) -> int:
    """确认页当前勾了几道题(预估用;与 confirm_choice 同一口径)。"""
    return sum(0 <= i < len(job.candidates) for i in prompts_selected) + len(lines_of(custom))


class ConfirmChoice(FrozenModel):
    """确认页定下的全部口径,原样交给 DiagnosisJob.freeze。"""

    prompts: list[Prompt]
    competitors: list[BrandSpec]
    targets: list[Target]
    budget_usd: float | None
    dependency_enabled: bool


def confirm_choice(
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    *,
    prompts_selected: list[int],
    custom: str,
    competitors_selected: list[int],
    targets_selected: list[str],
    budget: str,
    dependency: str,
) -> ConfirmChoice | str:
    """确认页表单 → 口径;不合格返回错误码。"""
    prompts = [
        Prompt(text=c.text, intent=c.intent, origin=c.origin, locale=job.locale)
        for i in prompts_selected if 0 <= i < len(job.candidates)
        for c in [job.candidates[i]]
    ] + [
        Prompt(text=text, intent=manual_intent(text, job.brand), origin=PromptOrigin.USER, locale=job.locale)
        for text in lines_of(custom)
    ]
    if not 1 <= len(prompts) <= MAX_PROMPTS:
        return "err_prompts_count"
    targets = selected_targets(targets_selected, adapters)
    if not targets:
        return "err_no_target"
    try:
        budget_usd = float(budget) if budget.strip() else None
    except ValueError:
        return "err_bad_params"
    if budget_usd is not None and budget_usd <= 0:
        return "err_bad_params"
    competitors = [
        BrandSpec(name=c.name, domains=[c.domain])
        for i in competitors_selected if 0 <= i < len(job.competitors_suggested)
        for c in [job.competitors_suggested[i]]
    ]
    return ConfirmChoice(prompts=prompts, competitors=competitors, targets=targets,
                         budget_usd=budget_usd, dependency_enabled=bool(dependency))
