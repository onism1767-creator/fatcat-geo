"""验收清单 12.2「契约边界测试」:SurfaceRun 及嵌套模型的字段集合白名单断言。

原始层不得出现提及/情感/判定类字段(硬边界 4)。
新增字段必须显式进白名单——那正是本测试想强迫发生的评审动作。
"""

from engine.models import (
    Answer,
    Citation,
    RetrievedSource,
    RunError,
    SearchTrace,
    SurfaceRun,
)

WHITELISTS = {
    SurfaceRun: {
        "run_id",
        "prompt_id",
        "run_index",
        "provider",
        "surface",
        "model",
        "search_mode",
        "locale",
        "region",
        "outcome",
        "error",
        "search_trace",
        "answer",
        "raw_payload",
        "timestamp",
        "latency_ms",
        "cost_usd",
    },
    SearchTrace: {
        "search_triggered",
        "generated_queries",
        "retrieved_sources",
        "opened_sources",
    },
    RetrievedSource: {"url", "domain", "title", "rank"},
    Answer: {"answer_text", "citations_available", "citations"},
    Citation: {"url", "domain", "title", "position"},
    RunError: {"error_type", "message", "http_status", "attempts"},
}

# 判定类词汇不允许作为原始层字段名的任何部分出现
FORBIDDEN_FIELD_TERMS = ("mention", "sentiment", "brand", "claim", "verdict", "score")


def test_raw_layer_fields_exactly_whitelisted():
    for model, expected in WHITELISTS.items():
        actual = set(model.model_fields)
        assert actual == expected, (
            f"{model.__name__} 字段集合偏离白名单:多出 {actual - expected},缺少 {expected - actual}。"
            "原始层字段增删属第 6 章契约改动,需评审后同步更新本白名单。"
        )


def test_raw_layer_fields_contain_no_judgment_terms():
    offenders = [
        (model.__name__, field)
        for model in WHITELISTS
        for field in model.model_fields
        if any(term in field.lower() for term in FORBIDDEN_FIELD_TERMS)
    ]
    assert not offenders, f"原始层出现判定类字段(属 L2 判定器职责): {offenders}"
