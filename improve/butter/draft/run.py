"""起草流程(纯层:调模型的动作由外面传进来的 ask 完成,这里只管流程)。设计 §7.2。

每页:问一次 → 解析(格式不对带着错误重问,最多 2 次)→ 逐条过检查
      → K1–K4 不过的带着原因重写一次,重写稿再过一遍,还不过整条作废;K5–K7 不过直接作废
      → 通过的留下。每一次问都记一条账。
整站:账户类错误立即停;到了费用上限不再起草后面的页;最后比一遍各页之间是不是雷同(K9)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from butter.check import check_proposal, decide, k9_duplicates, needs_page_warning, rewrite_reasons
from butter.contracts import (
    DRAFT_FIELDS, CheckContext, Completion, DraftAttempt, DraftedItem, DraftOutcome, DraftProposal, DraftTask,
    VoidedDraft,
)
from butter.draft import prompts

MAX_FORMAT_RETRIES = 2
Ask = Callable[[str, dict], Completion]  # (提示词, 输出格式) → 模型回答


def parse_drafts(parsed: Any, task: DraftTask) -> tuple[list[DraftProposal], list[str]]:
    """模型回答里抠出的 JSON → 草稿。返回(草稿, 格式错误)。同一个字段只留第一条;没要的字段丢掉。"""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("drafts"), list):
        return [], ['The answer must be a JSON object with a "drafts" array.']
    out: list[DraftProposal] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(parsed["drafts"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("field"), str) or not isinstance(raw.get("text"), str):
            errors.append(f'drafts[{i}] must be an object with string "field" and string "text".')
            continue
        field = raw["field"].strip()
        if field in seen or (field in DRAFT_FIELDS and field not in task.fields):
            continue
        seen.add(field)
        cited = raw.get("facts_used")
        out.append(DraftProposal(task_id=task.task_id, field=field, text=raw["text"],
                                 facts_cited=[str(c) for c in cited] if isinstance(cited, list) else [],
                                 rationale=str(raw.get("why") or "")))
    return out, errors


def _attempt(task: DraftTask, comp: Completion, reason: str) -> DraftAttempt:
    return DraftAttempt(task_id=task.task_id, provider=comp.provider, model=comp.model,
                        prompt_version=prompts.PROMPT_VERSION, request_hash=comp.request_hash,
                        tokens_in=comp.tokens_in, tokens_out=comp.tokens_out, cost_usd=comp.cost_usd,
                        latency_ms=comp.latency_ms, cached=comp.cached, error=comp.error, reason=reason)


def draft_site(tasks: list[DraftTask], contexts: dict[str, CheckContext], ask: Ask, *, budget_usd: float,
               skipped: list[str] | None = None, provider: str = "", model: str = "") -> DraftOutcome:
    items: list[DraftedItem] = []
    voided: list[VoidedDraft] = []
    attempts: list[DraftAttempt] = []
    skipped = list(skipped or [])
    warnings: list[str] = []
    stopped = ""
    spent = 0.0
    calls = 0

    def call(task: DraftTask, prompt: str, fields: list[str], reason: str) -> Completion:
        nonlocal spent, calls
        comp = ask(prompt, prompts.output_schema(fields))
        attempts.append(_attempt(task, comp, reason))
        spent += comp.cost_usd
        calls += comp.tries
        return comp

    for n, task in enumerate(tasks):
        if stopped:
            skipped.append(f"{task.page_url}:{stopped}")
            continue
        if spent >= budget_usd:
            stopped = f"到了这次运行的起草费用上限(${budget_usd:.2f}),后面的页没有起草"
            skipped.append(f"{task.page_url}:{stopped}")
            continue
        ctx = contexts[task.task_id]
        comp = call(task, prompts.page_prompt(task), task.fields, "首次")
        proposals, errors = ([], []) if comp.error_kind and comp.error_kind != "truncated" else parse_drafts(comp.parsed, task)
        retries = 0
        while not comp.error_kind and errors and not proposals and retries < MAX_FORMAT_RETRIES and spent < budget_usd:
            retries += 1
            comp = call(task, prompts.format_retry_prompt(task, errors), task.fields, "格式重问")
            proposals, errors = ([], []) if comp.error_kind else parse_drafts(comp.parsed, task)
        if comp.error_kind in ("account", "no_key"):
            stopped = f"起草停下了:{comp.error}"
            skipped.append(f"{task.page_url}:{stopped}")
            continue
        if comp.error_kind and comp.error_kind != "truncated":
            skipped.append(f"{task.page_url}:模型没有给出可用的回答({comp.error_kind}:{comp.error[:120]})")
            continue
        if not proposals:
            skipped.append(f"{task.page_url}:模型的回答不合格式,重问 {retries} 次后仍然不行")
            continue

        accepted: dict[str, tuple[str, list, bool]] = {}
        to_rewrite: dict[str, tuple[str, list[str]]] = {}
        page_flag = False
        for prop in proposals:
            text, results = check_proposal(prop, ctx)
            page_flag = page_flag or needs_page_warning(results)
            verdict = decide(results, already_rewritten=False)
            if verdict == "accept":
                accepted[prop.field] = (text, results, False)
            elif verdict == "rewrite":
                to_rewrite[prop.field] = (text, rewrite_reasons(results))
            else:
                voided.append(VoidedDraft(task_id=task.task_id, page_url=task.page_url, field=prop.field,
                                          reasons=[f"{r.rule}:{r.detail}" for r in results if r.outcome == "作废"]))
        if to_rewrite and spent < budget_usd:
            comp2 = call(task, prompts.rewrite_prompt(task, to_rewrite), list(to_rewrite), "检查后重写")
            second = {} if comp2.error_kind and comp2.error_kind != "truncated" else {
                p.field: p for p in parse_drafts(comp2.parsed, task)[0]}
            if comp2.error_kind in ("account", "no_key"):
                stopped = f"起草停下了:{comp2.error}"
            for field, (_, first_reasons) in to_rewrite.items():
                prop = second.get(field)
                if prop is None:
                    voided.append(VoidedDraft(task_id=task.task_id, page_url=task.page_url, field=field,
                                              reasons=[*first_reasons, "重写时模型没有再给出这个字段"]))
                    continue
                text, results = check_proposal(prop, ctx)
                page_flag = page_flag or needs_page_warning(results)
                if decide(results, already_rewritten=True) == "accept":
                    accepted[field] = (text, results, True)
                else:
                    voided.append(VoidedDraft(
                        task_id=task.task_id, page_url=task.page_url, field=field,
                        reasons=["重写一次后仍然不过"] + [f"{r.rule}:{r.detail}" for r in results if r.outcome == "作废"]))
        elif to_rewrite:
            for field, (_, reasons) in to_rewrite.items():
                voided.append(VoidedDraft(task_id=task.task_id, page_url=task.page_url, field=field,
                                          reasons=[*reasons, "到了费用上限,没有重写"]))
        if page_flag:
            warnings.append(f"{task.page_url}:这页的草稿里出现了像是写给 AI 的指令或隐藏内容,已作废。"
                            "这页原文里可能含有针对模型的指令,建议检查。")
        question_id = task.question_ids[0] if task.question_ids else None
        for field in task.fields:
            if field in accepted:
                text, results, rewritten = accepted[field]
                items.append(DraftedItem(
                    task_id=task.task_id, page_url=task.page_url, field=field, text=text,
                    before=task.current.get(field, ""), rewritten=rewritten, checks=results,
                    question_id=question_id if field in ("answer_first", "title", "h1") else None))

    dupes = k9_duplicates([(i.page_url, i.field, i.text) for i in items])
    items = [item.model_copy(update={"checks": [*item.checks, dupes[idx]]}) if idx in dupes else item
             for idx, item in enumerate(items)]
    return DraftOutcome(provider=provider, model=model, prompt_version=prompts.PROMPT_VERSION, items=items,
                        voided=voided, attempts=attempts, skipped=skipped, page_warnings=warnings, stopped=stopped,
                        cost_usd=round(spent, 6), calls=calls)
