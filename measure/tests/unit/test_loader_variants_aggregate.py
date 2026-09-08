"""L2 收尾件的单元测试:配置装载一致性防线、变体解析、需求层聚合。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from engine.loader import DEFAULT_SURFACES_PATH, build_adapters, load_surface_registry
from engine.metrics import account_outcomes, estimate_proportion
from engine.models import (
    Answer,
    InformationNeed,
    NeedIntent,
    Outcome,
    PromptOrigin,
    RunError,
    RunErrorType,
    StopReason,
    SurfaceRun,
)
from engine.scheduler import SamplingResult, aggregate_need_surface
from engine.variants import generate_variants, parse_variant_lines

# ── 配置装载 ─────────────────────────────────────────────────────────


def test_registry_loads_bundled_config():
    entries = load_surface_registry(DEFAULT_SURFACES_PATH)
    assert len(entries) == 5
    enabled = [e for e in entries if e.enabled]
    assert len(enabled) == 4  # 双通道 + claude_web_search + openai_web_search 已落地


def test_build_adapters_with_key_and_capability_consistency():
    adapters = build_adapters({
        "PERPLEXITY_API_KEY": "test-key",
        "ANTHROPIC_API_KEY": "test-key",
        "OPENAI_API_KEY": "test-key",
    })
    assert set(adapters) == {
        "perplexity_search", "perplexity_agent", "claude_web_search", "openai_web_search",
    }
    for name, adapter in adapters.items():
        assert adapter.meta().surface == name  # 身份与能力一致性校验已通过
        assert adapter.meta().est_cost_per_call_usd > 0  # 预估分价:每个 surface 必须报价


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


# ── 变体解析与生成 ────────────────────────────────────────────────────


def test_parse_variant_lines_strips_numbering_and_dedupes():
    text = ("1. 中小团队适合哪个 CRM?\n2、有什么 CRM 推荐给小公司?\n"
            "- 中小团队适合哪个 CRM?\n\n3) 十人团队怎么选客户管理系统\n")
    lines = parse_variant_lines(text, count=4)
    assert lines == [
        "中小团队适合哪个 CRM?",
        "有什么 CRM 推荐给小公司?",
        "十人团队怎么选客户管理系统",
    ]


def test_parse_variant_lines_respects_count_and_exclude():
    text = "问法甲\n问法乙\n问法丙\n"
    assert parse_variant_lines(text, count=2) == ["问法甲", "问法乙"]
    assert parse_variant_lines(text, count=3, exclude={"问法乙"}) == ["问法甲", "问法丙"]


class _FakeGenAdapter:
    """最小假适配器:只实现 generate_variants 用到的 ask()。"""

    def __init__(self, answer_text: str | None):
        self._answer_text = answer_text

    def ask(self, request):
        refused = self._answer_text is None
        return SurfaceRun(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="test_provider", surface="test_surface", model="m1",
            search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.REFUSED if refused else Outcome.SUCCESS,
            error=RunError(error_type=RunErrorType.REFUSAL, message="filtered") if refused else None,
            answer=None if refused else Answer(answer_text=self._answer_text, citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0,
        )


def test_generate_variants_end_to_end():
    need = InformationNeed(need_id="need-v", statement="中小团队 CRM 选型", intent=NeedIntent.DISCOVERY)
    adapter = _FakeGenAdapter("1. 小公司用什么 CRM?\n2. 十人团队 CRM 推荐\n")
    variants = generate_variants(adapter, need, count=2)
    assert [v.prompt_text for v in variants] == ["小公司用什么 CRM?", "十人团队 CRM 推荐"]
    assert all(v.origin is PromptOrigin.ASSISTED and v.need_id == "need-v" for v in variants)


def test_generate_variants_fails_loudly_on_refusal():
    need = InformationNeed(need_id="need-v", statement="测试", intent=NeedIntent.DISCOVERY)
    with pytest.raises(ValueError, match="变体生成失败"):
        generate_variants(_FakeGenAdapter(None), need, count=2)


# ── 需求层聚合 ────────────────────────────────────────────────────────


def make_result(variant_id: str, k: int, n: int, stop=StopReason.CI_CONVERGED, **overrides):
    fields = dict(
        information_need_id="need-a",
        prompt_variant_id=variant_id,
        provider="test_provider", surface="test_surface", model="m1",
        stop_reason=stop,
        n_attempts=n,
        accounting=account_outcomes([Outcome.SUCCESS] * n),
        k_hits=k,
        estimate=estimate_proportion(k, n) if n else None,
        total_cost_usd=0.01 * n,
        run_ids=[f"{variant_id}-{i}" for i in range(n)],
    )
    fields.update(overrides)
    return SamplingResult(**fields)


def test_aggregate_pools_hits_and_denominators():
    metric = aggregate_need_surface(
        [make_result("v1", 2, 8), make_result("v2", 1, 6, stop=StopReason.MAX_N_REACHED)],
        metric_label="citation_domain_match",
    )
    assert metric.k_hits == 3
    assert metric.estimate.n == 14
    assert metric.estimate.point == pytest.approx(3 / 14)
    assert metric.accounting.n_total == 14
    assert metric.stop_reasons == [StopReason.CI_CONVERGED, StopReason.MAX_N_REACHED]
    assert len(metric.per_variant) == 2


def test_aggregate_rejects_mixed_heads():
    with pytest.raises(ValueError, match="同一"):
        aggregate_need_surface(
            [make_result("v1", 1, 5), make_result("v2", 1, 5, surface="other_surface")],
            metric_label="x",
        )


def test_aggregate_rejects_empty():
    with pytest.raises(ValueError):
        aggregate_need_surface([], metric_label="x")
