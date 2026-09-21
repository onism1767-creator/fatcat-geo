"""问法变体生成(第 3 章步骤 1):同一信息需求的不同问法。

LLM 辅助任务同样走适配器(第 11 章),不绕过 L1。生成调用是辅助行为,
不是观测采样:其 SurfaceRun 不落库、不进任何指标分母。
生成的变体 origin=ASSISTED,与用户原话(origin=USER)在口径上可区分。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from adapters.base import SurfaceAdapter
from engine.models import AskRequest, InformationNeed, Outcome, PromptOrigin, PromptVariant

log = structlog.get_logger("engine.variants")

_LINE_PREFIX = re.compile(r"^\s*(?:[0-9]+[).、.::]?|[-*•])\s*")

_TEMPLATE = (
    "你在为一个 AI 可见度诊断工具生成采样问法。\n"
    "信息需求:{statement}\n"
    "语言:{locale}(问法必须与该语境一致)\n"
    "请生成 {count} 个语义相同但表述不同的问法(口语、正式、场景化混合)。\n"
    "每行输出一个问法,不要编号,不要解释,不要输出任何其他内容。"
)

_TEMPLATE_EN = (
    "You are generating sampling phrasings for an AI-visibility diagnosis tool.\n"
    "Information need: {statement}\n"
    "Language: {locale} (phrasings must match this locale)\n"
    "Write {count} phrasings with the same meaning but different wording "
    "(mix casual, formal and situational).\n"
    "Output one phrasing per line — no numbering, no explanations, nothing else."
)


def parse_variant_lines(text: str, *, count: int, exclude: set[str] | None = None) -> list[str]:
    """把 LLM 输出解析为问法列表:剥行首编号/符号,去空行,去重,截断到 count。"""
    exclude_lowered = {e.strip().lower() for e in (exclude or set())}
    seen: set[str] = set()
    variants: list[str] = []
    for raw_line in text.splitlines():
        line = _LINE_PREFIX.sub("", raw_line).strip()
        key = line.lower()
        if not line or key in seen or key in exclude_lowered:
            continue
        seen.add(key)
        variants.append(line)
        if len(variants) >= count:
            break
    return variants


def generate_variants(
    adapter: SurfaceAdapter,
    need: InformationNeed,
    *,
    count: int = 4,
    locale: str = "zh-CN",
) -> list[PromptVariant]:
    """生成 count 个 ASSISTED 变体。生成失败显式抛错,不静默返回空。"""
    template = _TEMPLATE_EN if locale.lower().startswith("en") else _TEMPLATE
    request = AskRequest(
        run_id=f"vargen-{uuid4().hex[:12]}",
        information_need_id=need.need_id,
        prompt_variant_id="__variant_generation__",
        prompt_text=template.format(statement=need.statement, count=count, locale=locale),
        locale=locale,
    )
    run = adapter.ask(request)  # 辅助调用:不落库
    if run.outcome is not Outcome.SUCCESS or run.answer is None:
        raise ValueError(f"变体生成失败:outcome={run.outcome},无可用回答")
    lines = parse_variant_lines(run.answer.answer_text, count=count, exclude={need.statement})
    if not lines:
        raise ValueError("变体生成失败:回答中解析不出任何问法")
    log.info("variants_generated", need_id=need.need_id, requested=count, parsed=len(lines))
    # 同批逐条错开 1 微秒:库里按 created_at 排问法,而 Windows 上 Python 时钟粒度毫秒级,
    # 不错开则同批平局,续跑 / 重新生成报告时问法顺序看运气
    batch_start = datetime.now(UTC)
    return [
        PromptVariant(need_id=need.need_id, prompt_text=line, origin=PromptOrigin.ASSISTED,
                      created_at=batch_start + timedelta(microseconds=index))
        for index, line in enumerate(lines)
    ]
