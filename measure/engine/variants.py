"""问法变体生成(第 3 章步骤 1):同一信息需求的不同问法。

LLM 辅助任务同样走适配器(第 11 章),不绕过 L1,统一经 engine/ai_task.py(重试、不合格重问)。
生成调用是辅助行为,不是观测采样:不进任何指标分母。
生成的变体 origin=ASSISTED,与用户原话(origin=USER)在口径上可区分。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import structlog

from adapters.base import SurfaceAdapter
from engine.ai_task import run_task
from engine.models import InformationNeed, PromptOrigin, PromptVariant

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
    """生成 count 个 ASSISTED 变体。问满轮数仍失败显式抛错(TaskFailed),不静默返回空。"""
    template = _TEMPLATE_EN if locale.lower().startswith("en") else _TEMPLATE

    def parse(text: str) -> list[str]:
        lines = parse_variant_lines(text, count=count, exclude={need.statement})
        if not lines:
            raise ValueError("回答中解析不出任何问法")
        return lines

    lines = run_task(
        adapter, template.format(statement=need.statement, count=count, locale=locale), parse,
        label="变体生成", need_id=need.need_id, locale=locale,
    ).value
    log.info("variants_generated", need_id=need.need_id, requested=count, parsed=len(lines))
    # 同批逐条错开 1 微秒:库里按 created_at 排问法,而 Windows 上 Python 时钟粒度毫秒级,
    # 不错开则同批平局,续跑 / 重新生成报告时问法顺序看运气
    batch_start = datetime.now(UTC)
    return [
        PromptVariant(need_id=need.need_id, prompt_text=line, origin=PromptOrigin.ASSISTED,
                      created_at=batch_start + timedelta(microseconds=index))
        for index, line in enumerate(lines)
    ]
