"""官网名单(架构书附录 v0.4.24 D4):候选只是提示、只认用户确认的;确认后按新名单只重算、不向 AI 提问。"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    BrandSpec,
    Citation,
    Outcome,
    RetrievedSource,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from engine.official_domains import official_candidates, registrable
from engine.resolve import ResolutionContext
from report.forms import brand_from_form

BRAND = BrandSpec(name="始祖鸟", domains=["arcteryx.com"])


def _run(run_id: str, sources: list[str] | None, cited: list[str]) -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id="n", run_index=1, provider="p", surface="s", model="m",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
        search_trace=SearchTrace(retrieved_sources=[
            RetrievedSource(url=f"https://{d}/x", domain=d, rank=i + 1) for i, d in enumerate(sources)
        ] if sources is not None else NOT_OBSERVABLE),
        answer=Answer(answer_text="回答", citations_available=True, citations=[
            Citation(url=f"https://{d}/y", domain=d, position=i + 1) for i, d in enumerate(cited)
        ]),
        timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0,
    )


def test_registrable_handles_multi_part_suffixes_offline():
    assert registrable("www.arcteryx.com.tw") == ("arcteryx", "arcteryx.com.tw")
    assert registrable("arcteryx.pissedconsumer.com") == ("pissedconsumer", "pissedconsumer.com")


def test_candidates_are_regional_sites_counted_by_appearance_not_lookalikes():
    runs = [
        _run("r1", ["arcteryx.jp", "www.arcteryx.com", "arcteryx.pissedconsumer.com"], ["arcteryx.jp"]),
        _run("r2", None, ["www.arcteryx.com.au", "shop.arcteryx.com"]),  # 候选集不可观测也照样看引用
    ]
    got = [(c.domain, c.count) for c in official_candidates(runs, BRAND)]
    # 已在名单里的(含子域)不列;投诉站只是子域名里带品牌名,主体不同,不列
    assert got == [("arcteryx.jp", 2), ("arcteryx.com.au", 1)]


def test_redirect_surface_candidates_come_from_resolved_destinations_only():
    """跳转仪器(S4):候选看还原后的终点主机;还原失败的那条看不到,不列;非跳转仪器照原样。"""
    redirect_run = _run("r3", None, ["redir.test", "redir.test"]).model_copy(update={"surface": "redirect_surface"})
    context = ResolutionContext(frozenset({"redirect_surface"}), {
        "https://redir.test/y": "https://www.arcteryx.co.kr/men",  # 两条引用同一链接 → 都算
    })
    assert [(c.domain, c.count) for c in official_candidates([redirect_run], BRAND, context)] == [("arcteryx.co.kr", 2)]
    assert official_candidates([redirect_run], BRAND, ResolutionContext(frozenset({"redirect_surface"}), {})) == []
    assert official_candidates([redirect_run], BRAND) == []  # 不当作跳转仪器时,redir.test 主体不同,也不列


def test_intake_takes_other_official_sites_from_the_user():
    brand, website = brand_from_form("始祖鸟", "Arc'teryx,Arcteryx", "arcteryx.com",
                                     "arcteryx.jp, https://www.ARCTERYX.com.au/zh,arcteryx.com")
    assert website == "https://arcteryx.com"
    assert brand.domains == ["arcteryx.com", "arcteryx.jp", "www.arcteryx.com.au"]
    assert brand.aliases == ["Arc'teryx", "Arcteryx"]
