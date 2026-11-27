"""L2 收尾件的单元测试:配置装载一致性防线、题型层聚合(v0.4.26 S2:计数 + 比例,逐题并列,题目覆盖率)。"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from engine.aggregate import measure_target, rate_metric, tally_spellings
from engine.loader import DEFAULT_SURFACES_PATH, build_adapters, load_surface_registry
from engine.models import (
    Answer,
    BrandMentionObservation,
    BrandSpec,
    Outcome,
    PromptIntent,
    RunError,
    RunErrorType,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import RunObservations

# ── 配置装载 ─────────────────────────────────────────────────────────


def test_registry_loads_bundled_config():
    entries = load_surface_registry(DEFAULT_SURFACES_PATH)
    assert len(entries) == 6
    enabled = [e for e in entries if e.enabled]
    # 五家服务商、六台仪器(搜索接口 v0.4.26 S1 下线;第五家 2026-11-18 接入;2026-11-20 第五家多一台开着 X 帖子搜索的)
    assert len(enabled) == 6 and len({e.provider for e in enabled}) == 5


def test_build_adapters_with_key_and_capability_consistency():
    adapters = build_adapters({
        "PERPLEXITY_API_KEY": "test-key",
        "ANTHROPIC_API_KEY": "test-key",
        "OPENAI_API_KEY": "test-key",
        "GOOGLE_API_KEY": "test-key",
        "XAI_API_KEY": "test-key",
    })
    assert set(adapters) == {
        "perplexity_agent", "claude_web_search",
        "openai_web_search", "gemini_grounding", "grok_web_search", "grok_web_xposts_search",
    }
    for name, adapter in adapters.items():
        meta = adapter.meta()
        assert meta.surface == name  # 身份与能力一致性校验已通过
        # 预估分价(v0.4.19 按型号):每个型号都必须报价且为正——漏一个,预估就静默漏计
        assert set(meta.est_cost_per_call_usd) == set(meta.models)
        assert all(price > 0 for price in meta.est_cost_per_call_usd.values())
        if meta.est_cost_per_call_search_disabled_usd is not None:
            assert set(meta.est_cost_per_call_search_disabled_usd) == set(meta.models)
        assert meta.default_model in meta.models


def test_build_adapters_skips_surfaces_without_key():
    assert build_adapters({}) == {}


def _write_config(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(DEFAULT_SURFACES_PATH.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "surfaces.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_build_adapters_rejects_capability_drift(tmp_path):
    def flip_capability(data):
        entry = next(s for s in data["surfaces"] if s["name"] == "perplexity_agent")
        entry["capabilities"]["supports_region"] = True  # 配置谎报能力

    path = _write_config(tmp_path, flip_capability)
    with pytest.raises(ValueError, match="能力声明漂移"):
        build_adapters({"PERPLEXITY_API_KEY": "test-key"}, config_path=path)


def test_build_adapters_rejects_identity_mismatch(tmp_path):
    def rename_surface(data):
        entry = next(s for s in data["surfaces"] if s["name"] == "perplexity_agent")
        entry["name"] = "renamed_surface"

    path = _write_config(tmp_path, rename_surface)
    with pytest.raises(ValueError, match="身份不一致"):
        build_adapters({"PERPLEXITY_API_KEY": "test-key"}, config_path=path)


# ── 题型层聚合 ────────────────────────────────────────────────────────

TARGET = Target(surface="test_surface", model="m1")


def make_obs(prompt_id: str, run_index: int, *, mentioned: bool | None = True,
             outcome: Outcome = Outcome.SUCCESS, account: bool = False) -> RunObservations:
    """一条运行的观测:mentioned=None 表示该运行判不了(如失败行)。"""
    error = None
    if account:
        error = RunError(error_type=RunErrorType.ACCOUNT_CREDIT, message="no credit", http_status=400)
    elif outcome is not Outcome.SUCCESS:
        error = RunError(error_type=RunErrorType.TRANSIENT, message="x")
    run = SurfaceRun(
        run_id=f"{prompt_id}-{run_index}", prompt_id=prompt_id, run_index=run_index,
        provider="test_provider", surface="test_surface", model="m1", search_mode="default", locale="zh-CN",
        outcome=outcome, error=error,
        answer=Answer(answer_text="x", citations_available=True) if outcome is Outcome.SUCCESS else None,
        timestamp=datetime(2026, 10, 12, tzinfo=UTC), latency_ms=1, cost_usd=0.001,
    )
    mention = (
        BrandMentionObservation(run_id=run.run_id, detector_name="m", detector_version=1, mentioned=mentioned)
        if mentioned is not None else None
    )
    return RunObservations(run=run, mention=mention, citation=None, presence=None, sentiment=None, query=None,
                           competitor_citations={}, brand_cited_urls=[])


def _hit(obs: RunObservations) -> bool | None:
    return None if obs.mention is None else obs.mention.mentioned


def test_rate_metric_pools_hits_per_prompt_and_reports_coverage():
    observations = [
        make_obs("q1", 1), make_obs("q1", 2, mentioned=False), make_obs("q1", 3),
        make_obs("q2", 1, mentioned=False), make_obs("q2", 2, mentioned=False), make_obs("q2", 3, mentioned=False),
        make_obs("q3", 1, mentioned=None, outcome=Outcome.TRANSIENT_FAILURE),
    ]
    metric = rate_metric(TARGET, "test_provider", PromptIntent.CATEGORY_DISCOVERY, observations, ["q1", "q2", "q3"],
                         _hit, metric_label="brand_mention", stop_reason=StopReason.COMPLETED)
    assert (metric.k_hits, metric.rate.k, metric.rate.n) == (2, 2, 6)
    assert metric.rate.share == pytest.approx(2 / 6)
    assert [(r.k, r.n) if r else None for r in metric.per_prompt] == [(2, 3), (0, 3), None]  # 逐题,与题同序
    # 题目覆盖率的分母是题目总数:q3 没有有效回答,按没命中算(产品文档 15.2)
    assert (metric.prompt_coverage.k, metric.prompt_coverage.n) == (1, 3)
    assert metric.accounting.n_total == 7 and metric.accounting.n_valid == 6
    assert metric.stop_reasons == [StopReason.COMPLETED] and metric.intent is PromptIntent.CATEGORY_DISCOVERY


def test_unsure_answers_stay_out_of_the_mention_rate():
    """BR4(用户定 Q3):说不准不进分母,原话里单独标出来。"""
    observations = [make_obs("q1", 1), make_obs("q1", 2, mentioned=False), make_obs("q1", 3)]
    unsure = BrandMentionObservation(run_id="q1-3", detector_name="m", detector_version=1, mentioned=None)
    observations[2] = replace(observations[2], mention=unsure)
    metric = rate_metric(TARGET, "test_provider", PromptIntent.CATEGORY_DISCOVERY, observations, ["q1"],
                         _hit, metric_label="brand_mention", stop_reason=StopReason.COMPLETED)
    assert (metric.rate.k, metric.rate.n) == (1, 2)
    measured = measure_target(TARGET, "test_provider", PromptIntent.CATEGORY_DISCOVERY, observations, ["q1"],
                              stop_reason=StopReason.COMPLETED, competitor_names=[], exposes_retrieval=False)
    assert [(q.run_index, q.mentioned, q.unsure) for q in measured.quotes] == [(1, True, False), (2, False, False),
                                                                               (3, None, True)]


def test_spellings_list_the_judges_new_ones_as_suggestions():
    """BR5(用户 10-30 定):裁判看到的新写法进写法清单,标明没算进提及率;已登记的、真算过的不标。"""
    def seen(*spellings: str, mentioned: bool | None = False, text: str | None = None) -> RunObservations:
        mention = BrandMentionObservation(run_id="r", detector_name="m", detector_version=1, mentioned=mentioned,
                                          matched_text=text, new_spellings=list(spellings))
        return replace(make_obs("q1", 1), mention=mention)

    brand = BrandSpec(name="极光CRM", aliases=["AuroraCRM"])
    listed = tally_spellings(
        [seen("极光CMR"), seen("极光cmr"), seen("auroracrm"), seen("Aurora", mentioned=True, text="Aurora")], brand)
    assert [(s.text, s.count, s.registered, s.suggested) for s in listed] == [
        ("极光CMR", 2, False, True), ("Aurora", 2, False, False)]


def test_rate_metric_without_valid_samples_has_no_rate():
    metric = rate_metric(TARGET, "test_provider", PromptIntent.BRAND_FACT,
                         [make_obs("q1", 1, mentioned=None, outcome=Outcome.REFUSED)], ["q1"],
                         _hit, metric_label="brand_mention", stop_reason=StopReason.COMPLETED)
    assert metric.rate is None and metric.prompt_coverage is None and metric.per_prompt == [None]
    assert metric.accounting.n_refused == 1 and metric.accounting.low_confidence is True


def test_rate_metric_keeps_account_error_rows_out_of_every_ratio():
    observations = [make_obs("q1", 1), make_obs("q1", 2, mentioned=None, outcome=Outcome.PERMANENT_FAILURE,
                                                 account=True)]
    metric = rate_metric(TARGET, "test_provider", PromptIntent.CATEGORY_DISCOVERY, observations, ["q1"],
                         _hit, metric_label="brand_mention", stop_reason=StopReason.COMPLETED)
    assert metric.accounting.n_total == 1 and metric.accounting.n_account_error == 1
    assert metric.accounting.low_confidence is False
