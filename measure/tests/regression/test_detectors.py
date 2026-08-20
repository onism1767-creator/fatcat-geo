"""判定器回归测试(12.1):固定 SurfaceRun 样本 → 期望 DerivedObservation。

期望值按 detector_version 归档:判定器升级时只允许【新增】样本与新版本的
期望条目;旧版本条目是历史行为的存证,禁止修改——如果你在改旧条目,
说明你正在无声地改写历史判定,停下来提版本号。
"""

from datetime import UTC, datetime

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.models import (
    Answer,
    BrandSpec,
    Citation,
    Outcome,
    RunError,
    RunErrorType,
    SearchMode,
    SurfaceRun,
)

BRAND = BrandSpec(
    name="极光CRM",
    aliases=["AuroraCRM", "Aurora CRM", "极光 CRM"],
    domains=["aurora-crm.com"],
)


def make_run(run_id: str, *, outcome=Outcome.SUCCESS, answer=None, error=None) -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id,
        information_need_id="need-1",
        prompt_variant_id="variant-1",
        provider="test_provider",
        surface="test_surface",
        model="test-model",
        search_mode=SearchMode.DEFAULT,
        locale="zh-CN",
        outcome=outcome,
        error=error,
        answer=answer,
        raw_payload={},
        timestamp=datetime(2026, 8, 20, tzinfo=UTC),
        latency_ms=1000,
        cost_usd=0.005,
    )


def answer_with(text: str, citations: list[Citation] | None = None, available: bool = True) -> Answer:
    return Answer(answer_text=text, citations_available=available, citations=citations or [])


SAMPLES = {
    "mention_exact_name": make_run(
        "r-m1", answer=answer_with("对于中小团队,极光CRM 和 Beta 系统都值得一试。")
    ),
    "mention_alias_case_insensitive": make_run(
        "r-m2", answer=answer_with("Many teams pick auroracrm for its pricing.")
    ),
    "mention_absent": make_run(
        "r-m3", answer=answer_with("常见选择包括 Alpha、Beta 与 Gamma 三家。")
    ),
    "mention_earliest_alias_wins": make_run(
        "r-m4", answer=answer_with("Aurora CRM(即极光CRM)近来讨论度高。")
    ),
    "refused_run": make_run(
        "r-m5",
        outcome=Outcome.REFUSED,
        error=RunError(error_type=RunErrorType.REFUSAL, message="finish_reason=content_filter"),
    ),
    "cited_exact_domain": make_run(
        "r-c1",
        answer=answer_with(
            "推荐参考这两篇评测。",
            [
                Citation(url="https://www.review-site.com/a", domain="www.review-site.com", position=1),
                Citation(url="https://aurora-crm.com/case", domain="aurora-crm.com", position=2),
            ],
        ),
    ),
    "cited_subdomain_and_www": make_run(
        "r-c2",
        answer=answer_with(
            "文档与博客均有提及。",
            [
                Citation(url="https://docs.aurora-crm.com/x", domain="docs.aurora-crm.com", position=1),
                Citation(url="https://www.aurora-crm.com/y", domain="www.aurora-crm.com", position=2),
                Citation(url="https://aurora-crm.com.evil.net/z", domain="aurora-crm.com.evil.net", position=3),
            ],
        ),
    ),
    "not_cited_empty_set": make_run("r-c3", answer=answer_with("一般性建议如下。", [])),
    "citations_not_available": make_run(
        "r-c4", answer=answer_with("回答正文。", available=False)
    ),
}

# ── 期望值,按 detector_version 归档(只增不改)──────────────────────────

MENTION_EXPECTED = {
    1: {
        "mention_exact_name": dict(mentioned=True, matched_alias="极光CRM", first_position=7),
        "mention_alias_case_insensitive": dict(
            mentioned=True, matched_alias="AuroraCRM", first_position=16
        ),
        "mention_absent": dict(mentioned=False, matched_alias=None, first_position=None),
        "mention_earliest_alias_wins": dict(
            mentioned=True, matched_alias="Aurora CRM", first_position=0
        ),
        "refused_run": None,  # 不可判定 → 无观测
    }
}

CITATION_EXPECTED = {
    1: {
        "cited_exact_domain": dict(cited=True, positions=[2]),
        "cited_subdomain_and_www": dict(cited=True, positions=[1, 2]),  # evil.net 不得命中
        "not_cited_empty_set": dict(cited=False, positions=[]),
        "citations_not_available": None,  # 看不到引用集 ≠ 未被引 → 无观测
        "refused_run": None,
    }
}


def test_brand_mention_detector_matches_archived_expectations():
    detector = BrandMentionDetector()
    expectations = MENTION_EXPECTED[detector.version]
    for sample_name, expected in expectations.items():
        observation = detector.detect(SAMPLES[sample_name], BRAND)
        if expected is None:
            assert observation is None, f"{sample_name}: 应无观测"
            continue
        assert observation is not None, f"{sample_name}: 应有观测"
        assert observation.detector_name == detector.name
        assert observation.detector_version == detector.version
        assert observation.run_id == SAMPLES[sample_name].run_id
        for field, value in expected.items():
            assert getattr(observation, field) == value, f"{sample_name}.{field}"


def test_citation_detector_matches_archived_expectations():
    detector = CitationDetector()
    expectations = CITATION_EXPECTED[detector.version]
    for sample_name, expected in expectations.items():
        observation = detector.detect(SAMPLES[sample_name], BRAND)
        if expected is None:
            assert observation is None, f"{sample_name}: 应无观测"
            continue
        assert observation is not None, f"{sample_name}: 应有观测"
        assert observation.detector_version == detector.version
        for field, value in expected.items():
            assert getattr(observation, field) == value, f"{sample_name}.{field}"


def test_citation_detector_requires_brand_domains():
    detector = CitationDetector()
    brand_without_domains = BrandSpec(name="极光CRM")
    assert detector.detect(SAMPLES["cited_exact_domain"], brand_without_domains) is None


def test_current_versions_have_archived_expectations():
    assert BrandMentionDetector().version in MENTION_EXPECTED
    assert CitationDetector().version in CITATION_EXPECTED
