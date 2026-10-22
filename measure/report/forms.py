"""L4 表单 → 领域对象:首页建任务、确认页定下要测什么。解析与校验都在这里,路由只管收发。

校验不过返回错误码(文案在 report/i18n.py,随界面语言),代码里不硬写给用户看的句子。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from adapters.base import SurfaceAdapter
from engine.drafting import PromptCandidate, manual_intent
from engine.models import BrandSpec, FrozenModel, Prompt, PromptOrigin, Target
from engine.tiers import TierChoice, TierConfig, tier_targets
from report.jobs import DiagnosisJob

MAX_PROMPTS = 40  # 产品文档默认 24 道;用户可增删,封顶 40
# 表单校验会返回的错误码(S10.1):都在 config/errors.yaml 里(测试把守),文案从库里取
FORM_CODES = ("FORM_PROMPTS_COUNT", "FORM_NO_TARGET", "FORM_BAD_TIER", "FORM_RUNS_RANGE", "FORM_BAD_BUDGET",
              "FORM_NO_QUESTION", "RESUME_MISSING_AI")


def lines_of(text: str) -> list[str]:
    """多行输入框:每行一条,空行不算。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _comma_list(text: str) -> list[str]:
    """逗号分开的一串(中英文逗号都认),去空。"""
    return [part.strip() for part in text.replace(",", ",").split(",") if part.strip()]


def _host(url: str) -> str:
    """网址 → 主机名(不带协议就补 https;大小写不敏感)。"""
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


def brand_from_form(brand_name: str, aliases: str, website: str) -> tuple[BrandSpec, str]:
    """首页填的品牌 → (品牌, 规范化后的官网地址)。官网名单只填主站;地区站、官方渠道由工具开跑时
    自动发现(S5,产品文档 D12),名单随任务快照冻结。"""
    normalized = website if "://" in website else f"https://{website}"
    brand = BrandSpec(
        name=brand_name.strip(),
        aliases=_comma_list(aliases),
        domains=[host for host in [_host(website)] if host],
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


def selected_targets(keys: list[str], adapters: dict[str, SurfaceAdapter]) -> list[Target]:
    """表单勾选的被测对象,只认当前清单里真实存在的(同序)。"""
    return [
        target for target in map(Target.parse, keys)
        if target.surface in adapters and target.model in adapters[target.surface].meta().models
    ]


def count_prompts(job: DiagnosisJob, prompts_selected: list[int], custom: str) -> int:
    """确认页当前勾了几道题(预估用;与 confirm_choice 同一口径)。"""
    return sum(0 <= i < len(job.candidates) for i in prompts_selected) + len(lines_of(custom))


def tier_choice(
    tiers: TierConfig, adapters: dict[str, SurfaceAdapter], *, tier: str, runs: str, targets_selected: list[str],
) -> tuple[TierChoice, list[Target]] | str:
    """档位 + 自定义(S9.1,用户定 T8):以选中的档位为起点,型号与次数改过就按自定义算,「提到你」的认法跟起点档走。
    确认与实时预估共用;不合格返回错误码。"""
    spec = tiers.get(tier)
    if spec is None:
        return "FORM_BAD_TIER"
    try:
        n = int(runs)
    except ValueError:
        return "FORM_RUNS_RANGE"
    if not tiers.allows_runs(n):
        return "FORM_RUNS_RANGE"
    targets = selected_targets(targets_selected, adapters)
    if not targets:
        return "FORM_NO_TARGET"
    standard = {t.key for t in tier_targets(spec, adapters)}
    custom = {t.key for t in targets} != standard or n != spec.runs_per_prompt
    return TierChoice(name=spec.name, runs_per_prompt=n, mention_method=spec.mention_method, custom=custom), targets


class ConfirmChoice(FrozenModel):
    """确认页定下的全部口径,原样交给 DiagnosisJob.freeze。"""

    prompts: list[Prompt]
    competitors: list[BrandSpec]
    targets: list[Target]
    tier: TierChoice
    aliases: list[str]  # 原有别名 + 确认页勾上的常见写法(S9.2b),去重保序
    budget_usd: float | None
    dependency_enabled: bool


def confirm_choice(
    job: DiagnosisJob,
    adapters: dict[str, SurfaceAdapter],
    tiers: TierConfig,
    *,
    prompts_selected: list[int],
    custom: str,
    competitors_selected: list[int],
    targets_selected: list[str],
    tier: str,
    runs: str,
    budget: str,
    dependency: str,
    spellings_selected: list[int] | None = None,
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
        return "FORM_PROMPTS_COUNT"
    chosen = tier_choice(tiers, adapters, tier=tier, runs=runs, targets_selected=targets_selected)
    if isinstance(chosen, str):
        return chosen
    choice, targets = chosen
    try:
        budget_usd = float(budget) if budget.strip() else None
    except ValueError:
        return "FORM_BAD_BUDGET"
    if budget_usd is not None and budget_usd <= 0:
        return "FORM_BAD_BUDGET"
    competitors = [
        BrandSpec(name=c.name, domains=[c.domain])
        for i in competitors_selected if 0 <= i < len(job.competitors_suggested)
        for c in [job.competitors_suggested[i]]
    ]
    picked = [job.spellings_suggested[i].text for i in (spellings_selected or [])
              if 0 <= i < len(job.spellings_suggested)]
    aliases = list(dict.fromkeys([*job.brand.aliases, *picked]))
    return ConfirmChoice(prompts=prompts, competitors=competitors, targets=targets, tier=choice, aliases=aliases,
                         budget_usd=budget_usd, dependency_enabled=bool(dependency))
