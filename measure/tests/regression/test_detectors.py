"""判定器回归测试(12.1):固定 SurfaceRun 样本 → 期望 DerivedObservation。

期望值按 detector_version 归档:判定器升级时只允许【新增】样本与新版本的
期望条目;旧版本条目是历史行为的存证,禁止修改——如果你在改旧条目,
说明你正在无声地改写历史判定,停下来提版本号。
"""

from datetime import UTC, datetime

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.retrieval_rank import RetrievalRankDetector
from engine.detectors.sentiment import SentimentDetector
from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    BrandSpec,
    Citation,
    Outcome,
    RetrievedSource,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)

BRAND = BrandSpec(
    name="极光CRM",
    aliases=["AuroraCRM", "Aurora CRM", "极光 CRM"],
    domains=["aurora-crm.com"],
)


def make_run(
    run_id: str, *, outcome=Outcome.SUCCESS, answer=None, error=None, search_trace=None
) -> SurfaceRun:
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
        search_trace=search_trace or SearchTrace(),
        raw_payload={},
        timestamp=datetime(2026, 8, 20, tzinfo=UTC),
        latency_ms=1000,
        cost_usd=0.005,
    )


def sources_trace(*specs: tuple[str, object]) -> SearchTrace:
    return SearchTrace(
        search_triggered=True,
        retrieved_sources=[
            RetrievedSource(url=f"https://{domain}/x", domain=domain, rank=rank)
            for domain, rank in specs
        ],
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
    "rank_hit_with_subdomain": make_run(
        "r-r1",
        search_trace=sources_trace(
            ("blog.csdn.net", 1), ("docs.aurora-crm.com", 2),
            ("www.aurora-crm.com", 3), ("aurora-crm.com.evil.net", 4),
        ),
    ),
    "rank_absent": make_run(
        "r-r2", search_trace=sources_trace(("blog.csdn.net", 1), ("reddit.com", 2)),
    ),
    "rank_empty_candidates": make_run(
        "r-r3", search_trace=SearchTrace(search_triggered=True, retrieved_sources=[]),
    ),
    "rank_unobservable_order": make_run(
        "r-r4", search_trace=sources_trace(("aurora-crm.com", NOT_OBSERVABLE)),
    ),
    "rank_candidates_not_observable": make_run("r-r5"),  # 默认 NOT_OBSERVABLE ≠ 空集
    "sentiment_positive": make_run(
        "r-s1", answer=answer_with("对于中小团队,极光CRM 值得一试。另一段无关文字。")
    ),
    "sentiment_negative": make_run(
        "r-s2", answer=answer_with("极光CRM 功能较弱且价格高,不推荐小团队使用。")
    ),
    "sentiment_mixed": make_run(
        "r-s3", answer=answer_with("极光CRM 好用但昂贵。")
    ),
    "sentiment_neutral": make_run(
        "r-s4", answer=answer_with("极光CRM 是一款客户管理软件。")
    ),
    "sentiment_scope_is_mention_sentence": make_run(
        "r-s5", answer=answer_with("市面上最佳工具很多。极光CRM 是其中一款。")
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


RETRIEVAL_RANK_EXPECTED = {
    1: {
        "rank_hit_with_subdomain": dict(
            present=True, best_rank=2,
            matched_domains=["docs.aurora-crm.com", "www.aurora-crm.com"],  # evil.net 不命中
        ),
        "rank_absent": dict(present=False, best_rank=None, matched_domains=[]),
        "rank_empty_candidates": dict(present=False, best_rank=None, matched_domains=[]),
        "rank_unobservable_order": dict(
            present=True, best_rank=None, matched_domains=["aurora-crm.com"],  # 在场但位次不可观测
        ),
        "rank_candidates_not_observable": None,  # 候选集不可观测 → 无观测
        "refused_run": None,
    }
}

SENTIMENT_EXPECTED = {
    1: {
        "sentiment_positive": dict(sentiment="positive"),
        "sentiment_negative": dict(sentiment="negative"),  # "不推荐"遮蔽后不再命中"推荐"
        "sentiment_mixed": dict(sentiment="mixed"),
        "sentiment_neutral": dict(sentiment="neutral"),
        "sentiment_scope_is_mention_sentence": dict(sentiment="neutral"),  # "最佳"在别句,不算
        "mention_absent": None,  # 未提及 → 情感不可判定
        "refused_run": None,
    }
}


def run_expectations(detector, expectations):
    for sample_name, expected in expectations[detector.version].items():
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


def test_retrieval_rank_detector_matches_archived_expectations():
    run_expectations(RetrievalRankDetector(), RETRIEVAL_RANK_EXPECTED)


def test_sentiment_detector_matches_archived_expectations():
    run_expectations(SentimentDetector(), SENTIMENT_EXPECTED)


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
    assert RetrievalRankDetector().version in RETRIEVAL_RANK_EXPECTED
    assert SentimentDetector().version in SENTIMENT_EXPECTED
