"""单条回答页(S8.3,架构书 v0.4.26 §10):题目、第几次、型号、时间、提到没提到、摘录、
引用了哪些网页(原链接 / 终点 / 是什么)、
完整回答、原始记录下载;只读库里已存的判定行,不重新判;外来文字自动转义;不属于本单的回答 404。"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from engine.models import (
    Answer,
    BrandDomainList,
    Citation,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    SearchMode,
    SurfaceRun,
)
from engine.observe import ObserveContext, observe, persist_observations
from engine.page_check import PageIndex
from report.app import create_app
from tests.unit.conftest import TEST_DSN
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job

ANSWER = "市面上常见的有几家。极光CRM 适合中小团队 <script>alert(1)</script>。别家也不错。"


def _run(prompt_id: str, run_id: str = "r1", index: int = 2, text: str = ANSWER) -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id=prompt_id, run_index=index, provider="test_provider", surface="s1", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
        answer=Answer(answer_text=text, citations_available=True, citations=[
            Citation(url="https://www.aurora-crm.com/pricing", domain="www.aurora-crm.com", position=1),
            Citation(url="https://review.test/best-crm", domain="review.test", position=2),
        ]),
        raw_payload={"platform": "raw"}, timestamp=datetime(2026, 10, 14, 3, 4, tzinfo=UTC), latency_ms=1234,
        cost_usd=0.0123,
    )


def _client(job) -> TestClient:
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn=TEST_DSN, evidence=REGISTRY,
                     job_runner=lambda **_: None)
    app.state.ctx.registry.add(job)
    return TestClient(app)


def test_response_page_shows_one_answer_with_its_recorded_judgements(storage):
    job = finished_job()
    job.domain_list = BrandDomainList(primary="aurora-crm.com", discovered_at=datetime(2026, 10, 14, tzinfo=UTC),
                                      public_suffix_list="t")
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    run = _run(prompt.prompt_id)
    storage.insert_surface_run(run)
    # 出报告时落的判定行
    persist_observations(storage, [observe(run, job.brand, ObserveContext(competitors=[], pages=PageIndex()))])
    client = _client(job)
    html = client.get(f"/jobs/{job.job_id}/responses/r1?lang=zh").text
    assert "中小团队用什么 CRM 好" in html and "第 2 次 · S1 · m1 · 2026-10-14 03:04" in html
    assert "提到了" in html and "「极光CRM 适合中小团队 &lt;script&gt;alert(1)&lt;/script&gt;」" in html  # 摘录转义
    assert "<script>alert(1)</script>" not in html
    assert "https://www.aurora-crm.com/pricing" in html and "你的官网" in html and "aurora-crm.com(主站)" in html
    assert "https://review.test/best-crm" in html and "没来得及开" in html  # 第三方页没核实:看不到(原因)
    assert "市面上常见的有几家。" in html  # 完整回答
    assert "brand_mention_rules v2" in html  # 技术细节里列判定器版本
    raw = client.get(f"/jobs/{job.job_id}/responses/r1/download")
    assert raw.status_code == 200 and raw.headers["content-disposition"].startswith("attachment")
    assert raw.json()["run_id"] == "r1" and raw.json()["raw_payload"] == {"platform": "raw"}
    en = client.get(f"/jobs/{job.job_id}/responses/r1?lang=en").text
    assert "Mentioned you?" in en and "Yes" in en


def test_response_page_refuses_answers_outside_this_job(storage):
    job = finished_job()
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    storage.insert_surface_run(_run(prompt.prompt_id, run_id="mine"))
    other = Prompt(text="另一单的题", intent=PromptIntent.BRAND_FACT, origin=PromptOrigin.USER, locale="zh-CN")
    storage.insert_prompt(other)
    storage.insert_surface_run(_run(other.prompt_id, run_id="theirs"))
    client = _client(job)
    assert client.get(f"/jobs/{job.job_id}/responses/mine").status_code == 200
    assert client.get(f"/jobs/{job.job_id}/responses/theirs").status_code == 404  # 别单的回答
    assert client.get(f"/jobs/{job.job_id}/responses/nope").status_code == 404
    assert client.get("/jobs/unknown/responses/mine").status_code == 404
    assert client.get(f"/jobs/{job.job_id}/responses/theirs/download").status_code == 404


def test_response_page_without_judgement_rows_still_shows_the_answer(storage):
    """判定行还没落(或旧版本没存归类):页面照样给回答与原链接,提到没提到写「没有记录」,不现算、不猜。"""
    job = finished_job()
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    storage.insert_surface_run(_run(prompt.prompt_id, run_id="bare"))
    html = _client(job).get(f"/jobs/{job.job_id}/responses/bare?lang=zh").text
    assert "没有判定记录" in html and "https://review.test/best-crm" in html and "没有归类记录" in html


def test_response_page_explains_a_fuzzy_match(storage):
    """S9.2:中档(规则 + 模糊)认出来的,单条回答页写明回答里写的是什么、和登记名差几个字母。"""
    from engine.tiers import MentionMethod, TierChoice

    job = finished_job()
    job.tier = TierChoice(name="medium", runs_per_prompt=5, mention_method=MentionMethod.RULES_FUZZY, custom=False)
    job.brand = job.brand.model_copy(update={"aliases": ["AuroraCRM"]})  # 模糊只对拉丁名字;夹具品牌只有中文名
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    run = _run(prompt.prompt_id, run_id="fz1", text="我们用 auroracrms 管客户,挺推荐的。")
    storage.insert_surface_run(run)
    context = ObserveContext(competitors=[], pages=PageIndex(), mention_method=MentionMethod.RULES_FUZZY)
    persist_observations(storage, [observe(run, job.brand, context)])
    html = _client(job).get(f"/jobs/{job.job_id}/responses/fz1?lang=zh").text
    assert "提到了" in html and "差 1 个字母(模糊匹配)" in html and "auroracrms" in html
    assert "brand_mention_fuzzy v1" in html and "brand_mention_rules v2" in html  # 两个判定器都存了
    assert "sentiment_lexicon v3" in html and "正面" in html  # 情感句跟主判定器定位


def test_unreadable_citations_link_to_the_error_code_guide(storage):
    """S10.3:单条回答页里「没来得及开」「跳转去向查不到」这类原因链到 /errors 对应的码。"""
    job = finished_job()
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    run = _run(prompt.prompt_id, run_id="lnk1")
    storage.insert_surface_run(run)
    persist_observations(storage, [observe(run, job.brand, ObserveContext(competitors=[], pages=PageIndex()))])
    html = _client(job).get(f"/jobs/{job.job_id}/responses/lnk1?lang=zh").text
    assert 'href="/errors?lang=zh#PAGE_NOT_ATTEMPTED">没来得及开</a>' in html
