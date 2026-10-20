"""判定器回归测试(12.1):固定 SurfaceRun 样本 → 期望 DerivedObservation。

期望值按 detector_version 归档:判定器升级时只允许【新增】样本与新版本的
期望条目;旧版本条目是历史行为的存证,禁止修改——如果你在改旧条目,
说明你正在无声地改写历史判定,停下来提版本号。
"""

from datetime import UTC, datetime

from engine.detectors.brand_mention import BrandMentionDetector
from engine.detectors.brand_mention_fuzzy import FuzzyBrandMentionDetector
from engine.detectors.citation import CitationDetector
from engine.detectors.query_trace import QueryTraceDetector
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
from engine.resolve import ResolutionContext

BRAND = BrandSpec(
    name="极光CRM",
    aliases=["AuroraCRM", "Aurora CRM", "极光 CRM"],
    domains=["aurora-crm.com"],
    official_channels=["aurora-crm.tmall.com/", "www.instagram.com/auroracrm"],  # v4(S5):官方渠道单列
)


_DEFAULT_ANSWER = object()


def make_run(
    run_id: str, *, outcome=Outcome.SUCCESS, answer=_DEFAULT_ANSWER, error=None, search_trace=None,
    surface: str = "test_surface",
) -> SurfaceRun:
    """没指定回答的 SUCCESS 样本给一段中性正文(v2 起空回答不可判定,检索 / 检索词样本需要一条有效回答;
    v1 判定器不看正文,v1 的归档期望不受影响)。"""
    if answer is _DEFAULT_ANSWER:
        answer = answer_with("一般性说明。") if outcome is Outcome.SUCCESS else None
    return SurfaceRun(
        run_id=run_id,
        prompt_id="need-1",
        run_index=1,
        provider="test_provider",
        surface=surface,
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
    # v2 词边界样本(D18):拉丁字母的名字两侧查词边界,中文按子串
    "mention_latin_substring_rejected": make_run(
        "r-m6", answer=answer_with("The auroracrms plugin and Aurora CRMS are different products.")
    ),
    "mention_latin_then_cjk": make_run("r-m7", answer=answer_with("我们用AuroraCRM管理客户。")),
    "mention_cjk_name_then_cjk": make_run("r-m8", answer=answer_with("极光CRM系统很好用。")),
    "mention_cjk_name_latin_tail_rejected": make_run("r-m9", answer=answer_with("极光CRMPro 是另一款产品。")),
    "mention_name_with_punctuation": make_run("r-m10", answer=answer_with("Try AuroraCRM, it's cheap.")),
    "empty_answer_success": make_run("r-e1", answer=answer_with("  \n")),  # SUCCESS 但正文为空:v2 判不了
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
    "citation_empty_answer": make_run(
        "r-c5",
        answer=answer_with("", [Citation(url="https://aurora-crm.com/case", domain="aurora-crm.com", position=1)]),
    ),
    # v3:跳转仪器的引用按还原后的终点判;还原失败的那条记看不到(S4)
    "cited_redirect_resolved_and_one_unresolved": make_run(
        "r-c6", surface="redirect_surface",
        answer=answer_with("正文。", [
            Citation(url="https://redir.test/t1", domain="redir.test", position=1),
            Citation(url="https://redir.test/t2", domain="redir.test", position=2),
        ]),
    ),
    "cited_redirect_all_unresolved": make_run(
        "r-c7", surface="redirect_surface",
        answer=answer_with("正文。", [Citation(url="https://redir.test/t9", domain="redir.test", position=1)]),
    ),
    # v4:官方渠道按主机名 + 路径前缀;另一家天猫店与相似账号名不命中;渠道命中不算官网被引(S5,T38)
    "cited_official_channels_only": make_run(
        "r-c8",
        answer=answer_with("正文。", [
            Citation(url="https://aurora-crm.tmall.com/item/123", domain="aurora-crm.tmall.com", position=1),
            Citation(url="https://other-shop.tmall.com/", domain="other-shop.tmall.com", position=2),
            Citation(url="https://www.instagram.com/auroracrm/p/1", domain="www.instagram.com", position=3),
            Citation(url="https://www.instagram.com/auroracrmfans", domain="www.instagram.com", position=4),
        ]),
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
    "rank_empty_answer": make_run(
        "r-r6", answer=answer_with(""), search_trace=sources_trace(("aurora-crm.com", 1)),
    ),
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
    "query_hit_alias": make_run(
        "r-q1",
        search_trace=SearchTrace(
            search_triggered=True,
            generated_queries=["中小团队 CRM 对比 极光CRM Zoho", "CRM 免费档推荐"],
        ),
    ),
    "query_absent": make_run(
        "r-q2",
        search_trace=SearchTrace(
            search_triggered=True, generated_queries=["中小团队 CRM 推荐 2026"]
        ),
    ),
    "query_none_generated": make_run(
        "r-q3", search_trace=SearchTrace(search_triggered=False, generated_queries=[]),
    ),
    "query_not_observable": make_run("r-q4"),  # 默认 NOT_OBSERVABLE ≠ 空列表
    "query_latin_substring_rejected": make_run(
        "r-q5",
        search_trace=SearchTrace(search_triggered=True, generated_queries=["auroracrms review", "best crm 2026"]),
    ),
    "query_empty_answer": make_run(
        "r-q6", answer=answer_with(" "),
        search_trace=SearchTrace(search_triggered=True, generated_queries=["极光CRM 评测"]),
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
# v2(v0.4.26 S3):拉丁字母按词边界、中文按子串;空回答不可判定。v1 的样本行为不变,只新增边界样本
MENTION_EXPECTED[2] = {
    **MENTION_EXPECTED[1],
    "mention_latin_substring_rejected": dict(mentioned=False, matched_alias=None, first_position=None),
    "mention_latin_then_cjk": dict(mentioned=True, matched_alias="AuroraCRM", first_position=3),
    "mention_cjk_name_then_cjk": dict(mentioned=True, matched_alias="极光CRM", first_position=0),
    "mention_cjk_name_latin_tail_rejected": dict(mentioned=False, matched_alias=None, first_position=None),
    "mention_name_with_punctuation": dict(mentioned=True, matched_alias="AuroraCRM", first_position=4),
    "empty_answer_success": None,
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
CITATION_EXPECTED[2] = {**CITATION_EXPECTED[1], "citation_empty_answer": None}  # v2:空回答不可判定
# v3(S4):跳转链先还原;命中的注册域名与判定用链接一并记录。跳转仪器样本用下面这个上下文判
REDIRECT_CONTEXT = ResolutionContext(frozenset({"redirect_surface"}), {
    "https://redir.test/t1": "https://www.aurora-crm.com/landing",
})
CITATION_EXPECTED[3] = {
    **CITATION_EXPECTED[2],
    "cited_exact_domain": dict(cited=True, positions=[2], matched_domains=["aurora-crm.com"],
                               matched_urls=["https://aurora-crm.com/case"], unresolved=0),
    "cited_subdomain_and_www": dict(cited=True, positions=[1, 2], matched_domains=["aurora-crm.com"], unresolved=0),
    "cited_redirect_resolved_and_one_unresolved": dict(
        cited=True, positions=[1], matched_domains=["aurora-crm.com"],
        matched_urls=["https://www.aurora-crm.com/landing"], unresolved=1,
    ),
    "cited_redirect_all_unresolved": dict(cited=False, positions=[], matched_domains=[], unresolved=1),
}
CITATION_EXPECTED[4] = {  # v4(S5):官方渠道单列
    **CITATION_EXPECTED[3],
    "cited_official_channels_only": dict(
        cited=False, positions=[], matched_domains=[],
        matched_channels=["aurora-crm.tmall.com/", "www.instagram.com/auroracrm"], unresolved=0,
    ),
    "cited_exact_domain": dict(cited=True, positions=[2], matched_domains=["aurora-crm.com"], matched_channels=[]),
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
RETRIEVAL_RANK_EXPECTED[2] = {**RETRIEVAL_RANK_EXPECTED[1], "rank_empty_answer": None}  # v2:空回答不可判定

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
SENTIMENT_EXPECTED[2] = {  # v2:随匹配原语改词边界提版;空回答不可判定
    **SENTIMENT_EXPECTED[1],
    "mention_latin_substring_rejected": None,  # 词边界下未提及 → 情感不可判定
    "empty_answer_success": None,
}
# v3(S9.2):提及句跟主提及判定器定位;配规则判定器(本文件的归档口径)时行为与 v2 相同
SENTIMENT_EXPECTED[3] = {**SENTIMENT_EXPECTED[2]}

# 模糊判定器 v1(S9.2):精确 / 规整与规则判定器 v2 同;连写 auroracrms 这类差 1 个字母的接住;中英混合名字不模糊
FUZZY_MENTION_EXPECTED = {
    1: {
        **{name: {**expected, "match_kind": "exact"} if expected and expected["mentioned"] else expected
           for name, expected in MENTION_EXPECTED[2].items()
           if name != "mention_latin_substring_rejected"},
        "mention_latin_substring_rejected": dict(
            mentioned=True, matched_alias="AuroraCRM", first_position=4, match_kind="fuzzy",
            matched_text="auroracrms", edit_distance=1,
        ),
    }
}


QUERY_TRACE_EXPECTED = {
    1: {
        "query_hit_alias": dict(
            present=True, matched_alias="极光CRM",
            matched_queries=["中小团队 CRM 对比 极光CRM Zoho"],
        ),
        "query_absent": dict(present=False, matched_alias=None, matched_queries=[]),
        "query_none_generated": dict(present=False, matched_alias=None, matched_queries=[]),
        "query_not_observable": None,  # 检索词不可观测 → 无观测
        "refused_run": None,
    }
}
QUERY_TRACE_EXPECTED[2] = {  # v2:拉丁字母按词边界;空回答不可判定
    **QUERY_TRACE_EXPECTED[1],
    "query_latin_substring_rejected": dict(present=False, matched_alias=None, matched_queries=[]),
    "query_empty_answer": None,
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
    run_expectations(SentimentDetector(BrandMentionDetector()), SENTIMENT_EXPECTED)


def test_fuzzy_mention_detector_matches_archived_expectations():
    run_expectations(FuzzyBrandMentionDetector(), FUZZY_MENTION_EXPECTED)


def test_query_trace_detector_matches_archived_expectations():
    run_expectations(QueryTraceDetector(), QUERY_TRACE_EXPECTED)


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
    detector = CitationDetector(REDIRECT_CONTEXT)
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
    assert SentimentDetector(BrandMentionDetector()).version in SENTIMENT_EXPECTED
    assert FuzzyBrandMentionDetector().version in FUZZY_MENTION_EXPECTED
    assert QueryTraceDetector().version in QUERY_TRACE_EXPECTED
