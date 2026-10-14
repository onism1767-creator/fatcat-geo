"""L4 页面测试,含 12.2「报告口径测试」的可执行化:

渲染输出不得匹配总分模式;每条指标携带 N 与 stop_reasons;
每条建议携带 evidence id;C 级以下带「建议实验验证」。
页面流用假 surface 与桩执行器,零网络零数据库。
"""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.citation_diag import VerificationSummary, diagnose_citation
from diagnosis.diagnose import access_finding, diagnose_class, diagnose_surface
from diagnosis.findings import Stage
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.recommender import load_evidence_registry
from diagnosis.representation_diag import compare_mentions, diagnose_representation, sentiment_tally
from diagnosis.retrieval_dependency import diagnose_retrieval_dependency
from diagnosis.retrieval_diag import retrieval_not_observable
from diagnosis.verdict import SurfaceVerdict
from engine.aggregate import ClassSurfaceMetric
from engine.drafting import PromptCandidate
from engine.metrics import account_outcomes, rate
from engine.models import (
    AnswerQuote,
    BrandSpec,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    Sentiment,
    StopReason,
    Target,
)
from report.app import create_app
from report.jobs import ClassReport, DiagnosisJob, JobState, SurfaceQuotes

REGISTRY = load_evidence_registry()


class DummyAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="S1", api_name="S1", provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1",
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={"m1": 0.005})

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def make_metric(k: int, n: int) -> ClassSurfaceMetric:
    return ClassSurfaceMetric(
        intent=PromptIntent.CATEGORY_DISCOVERY, provider="test_provider", surface="s1", model="m1",
        metric_label="test", k_hits=k,
        rate=rate(k, n),
        accounting=account_outcomes([Outcome.SUCCESS] * n),
        stop_reasons=[StopReason.COMPLETED],
        per_prompt=[rate(k, n)],
        prompt_coverage=rate(1 if k else 0, 1),
    )


def finished_job() -> DiagnosisJob:
    brand = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
    prompt = Prompt(text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    access = access_finding(None)
    # v0.4.11 分家:两家 surface 各自分诊——s1 不暴露检索链路(检索不可观测),s2 自带生成端链路
    blind_block = diagnose_surface(
        provider="test_provider", surface="s1", model="m1",
        access=access,
        retrieval=retrieval_not_observable(),
        citation=diagnose_citation(
            make_metric(0, 30),
            [("竞品甲", rate(20, 30))],
            verification=VerificationSummary(
                attempted=2, verified_match=0, content_mismatch=1,
                fetch_blocked=1, unparsed=0, unreachable=0,
            ),
        ),
        # 提及 12/30 = 40%:黄灯(夹具要有 🟡;20/30 会成绿灯)
        representation=diagnose_representation(
            PromptIntent.CATEGORY_DISCOVERY, make_metric(12, 30), {Sentiment.POSITIVE: 5}
        ),
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY,
        # 开卷 20/30 vs 闭卷 2/30:相差 60 个百分点且闭卷不足开卷一半 → HIGH
        retrieval_dependency=diagnose_retrieval_dependency(
            make_metric(20, 30), make_metric(2, 30)
        ),
        # S7:和竞品比谁被提得多——甲 20/30 比你(12/30)多 ≥ 20 个百分点;乙 10/30 和你差不多
        mention_compare=compare_mentions("极光CRM", make_metric(12, 30),
                                         [("竞品甲", rate(20, 30)), ("竞品乙", rate(10, 30))]),
        # S8.1:提到你的 18 句话里正面 1、中性 9、负面 8(混合 0 不写)
        sentiment=sentiment_tally({Sentiment.POSITIVE: 1, Sentiment.NEUTRAL: 9, Sentiment.NEGATIVE: 8}),
    )
    generative_block = diagnose_surface(
        provider="test_provider", surface="s2", model="m2",
        access=access,
        # 生成端链路作为该家自己的 RETRIEVAL:在场 0/30 → FAIL
        retrieval=diagnose_generative_retrieval(
            make_metric(0, 30), [], rate(1, 30), rate(0, 30), rate(25, 30),
        ),
        citation=diagnose_citation(make_metric(0, 30), [], verification=None),
        representation=diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(20, 30), {}),
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY,
    )
    diagnosis = diagnose_class(PromptIntent.CATEGORY_DISCOVERY, access=access, surfaces=[blind_block, generative_block])
    job = DiagnosisJob(brand=brand, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(prompts=[prompt], competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=1.0)
    job.state = JobState.DONE
    job.phase = "完成"
    job.finished_at = datetime(2026, 8, 24, tzinfo=UTC)
    quotes = [SurfaceQuotes(surface="s1", model="m1", quotes=[  # S7:AI 原话只进证据区;外来文字含标签,须转义
        AnswerQuote(prompt_id="p", prompt_no=1, run_index=1, run_id="r1", mentioned=True,
                    sentence="极光CRM 适合中小团队 <script>alert(1)</script>"),
        AnswerQuote(prompt_id="p", prompt_no=1, run_index=2, run_id="r2", mentioned=False),
        AnswerQuote(prompt_id="p", prompt_no=1, run_index=3, run_id="r3", mentioned=None),
    ])]
    job.class_reports = [ClassReport(intent=PromptIntent.CATEGORY_DISCOVERY, prompt_texts=[prompt.text],
                                     diagnosis=diagnosis, quotes=quotes)]
    job.surface_verdicts = [  # 每家 AI 一句话(v0.4.14):s2 缺「直接问起」场景,句尾说明
        SurfaceVerdict(surface="s1", model="m1", code="verdict.only_when_named", missing=[]),
        SurfaceVerdict(surface="s2", model="m2", code="verdict.known_sometimes_site_rare",
                       missing=["verdict.missing.probe"]),
    ]
    return job


@pytest.fixture()
def client_and_job():
    calls = []

    def stub_runner(*, job, adapters, dsn, evidence, replay):
        calls.append(job.job_id)
        job.state = JobState.DONE
        job.phase = "完成"

    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=stub_runner)
    job = finished_job()
    app.state.ctx.registry.add(job)
    return TestClient(app), job, calls


# ── 12.2 报告口径测试 ─────────────────────────────────────────────────


def test_report_contains_no_score_patterns(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert not re.search(r"总分|得分|score\s*[::]", html, re.IGNORECASE)
    assert not re.search(r"\d+\s*/\s*100", html)


def test_report_metrics_carry_caliber(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "N_valid" in html and "stop_reasons" in html
    assert "题数=1" in html and "/30 = " in html  # 计数 + 比例,不出区间(v0.4.26 S2)
    assert "操作阈值(v1 初始值,未经校准)" in html  # 阈值口径自报,且标明未校准
    assert "最强瓶颈信号在" in html and "失败最可能" not in html  # 定位措辞去因果
    assert job.fingerprint in html  # 任务快照指纹


def test_report_recommendations_carry_evidence_ids(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "EVIDENCE_STRUCT_V1" in html  # RETRIEVAL FAIL → 结构信号建议(B 级)
    assert "建议实验验证" in html  # C 级以下强制标注
    assert "证据等级" in html


USER_VIEW_FORBIDDEN = (  # v0.4.14 强制词典:主视图可见正文不得出现引擎内部语言(技术词只在「检测依据与方法」)
    "NOT_OBSERVABLE", "N_valid", "stop_reasons", "Jeffreys", "冻结题目集", "候选在场", "入场",
    "操作阈值", "EVIDENCE_", "surface=", "资格门", "瓶颈", "探针", "口径",
    "语义对齐", "语义覆盖", "检索阶段", "生成阶段", "检索层", "候选",  # 研究主张原句只在「为什么这么建议」与审计区
)


def user_view_of(html: str) -> str:
    """用户视图的可见正文 = 「检测依据与方法」之前,去掉样式块与折叠块,再剥掉标签。"""
    head = html.split('id="methods"')[0]
    head = re.sub(r"<style.*?</style>", "", head, flags=re.S)
    head = re.sub(r"<details.*?</details>", "", head, flags=re.S)
    return re.sub(r"<[^>]+>", " ", head)


def test_user_view_leads_with_plain_language(client_and_job):
    """v0.4.14:用户视图在前,全是人话——官网抓取一句、每家 AI 一句话(用显示名)、
    红绿灯状态、去重后的建议(标支持数);技术记录折叠在最后。"""
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "这次检测告诉了我们什么" in html and html.index("这次检测告诉了我们什么") < html.index('id="methods"')
    assert "用户主动问起时 S1 · m1 能介绍你,但用户不提你时 S1 · m1 几乎不会想到你" in html  # 显示名「接口 · 型号」代入
    assert "用户直接问起你时的表现本次没测" in html  # 缺项句尾说明
    assert "🔴 较弱" in html and "🟡 不够稳定" in html  # 红绿灯 + 定性词(夹具无 PASS 项)
    assert "有 1 处检测结果支持这条建议" in html  # s1 卡在引用、s2 卡在检索,各自的建议互不重合
    assert html.count('class="rec"') == 5  # s1 引用三条 + s2 搜到两条(第三方内容那条 S6 起归第三方线,D9)
    assert "用户问这类产品有哪些好的时(品类发现)" in html  # 题型人话
    assert "官网检索(AI 搜索时有没有找到你的官网)" in html  # 正式用词(口语解释),用户定 2026-10-13
    assert "本次 30 次搜索里,0 次(0%)结果中出现了你的官网" in html  # 计数在前、百分比跟在后面(用户定 S8)
    assert "让 AI 搜索时更容易找到你" in html  # 建议用人话版(注册表 plain)
    assert "建议先试" not in html  # 各 AI 详情不再重复列建议:「建议先做的事」已去重并标支持来源
    assert "研究主张原句" in html and "提高语义覆盖" in html  # 原句仍在「为什么这么建议」里
    assert "做完之后怎么检查效果" in html and "这一格有没有从 🔴 变 🟡 或 🟢" in html  # v0.4.17 两列


def test_user_view_has_no_engine_jargon(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    user_view = user_view_of(html)
    for token in USER_VIEW_FORBIDDEN:
        assert token not in user_view, token
    assert not re.search(r"(PASS|WEAK|FAIL)", user_view)  # 状态只以红绿灯 + 定性词出现
    assert "N_valid" in html and "EVIDENCE_" in html  # 技术记录仍完整保留在折叠区


UI_FORBIDDEN = (  # v0.4.15:首页 / 确认页 / 任务页同样不得出现引擎内部语言
    "surface", "口径", "序贯", "指纹", "熔断", "探针", "变体", "闭卷", "开卷", "语境", "采样",
    "ACCESS 审计", "RUNNING", "DONE", "CONFIRMED", "INTERRUPTED",
)


def visible_text(html: str) -> str:
    body = html.split("<main", 1)[1]
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    body = re.sub(r"<details.*?</details>", "", body, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


def test_interactive_pages_use_plain_language(client_and_job):
    """用户明令(2026-09-09):所有面向用户的页面只用简单中文,技术词只进折叠区。"""
    client, job, _ = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com", "category": "CRM",
        "questions": "问题一?", "generate": "",
    }, follow_redirects=False)
    pages = [client.get("/").text, client.get(resp.headers["location"]).text,
             client.get(f"/jobs/{job.job_id}").text]
    for html in pages:
        text = visible_text(html)
        for token in UI_FORBIDDEN:
            assert token not in text, (token, text[:200])
    assert "S1" in pages[1]  # 确认页用 AI 产品名,不用内部名
    assert "完成" in visible_text(pages[2]) and "DONE" not in visible_text(pages[2])


def test_confirm_validation_errors_are_plain_codes_not_free_text(client_and_job):
    """确认页校验不过:跳回确认页,按错误码用人话说明(文案在 i18n,不带技术词);
    地址栏里的错误码只认 err_ 开头的已知码,别的一律不显示(不回显任意文字)。"""
    client, _, _ = client_and_job
    resp = client.post("/jobs", data={"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com",
                                      "category": "CRM", "questions": "问题一?", "generate": ""},
                       follow_redirects=False)
    confirm_url = resp.headers["location"]
    job_path = confirm_url.split("?")[0].rsplit("/", 1)[0]
    back = client.post(f"{job_path}/confirm", data={"targets_selected": ["s1::m1"]}, follow_redirects=False)
    assert "error=err_prompts_count" in back.headers["location"]
    page = visible_text(client.get(back.headers["location"]).text)
    assert "要测的题请选 1 到 40 道" in page and not any(token in page for token in UI_FORBIDDEN)
    forged = visible_text(client.get(f"{job_path}/confirm?error=job_stalled").text)
    assert "卡住" not in forged


def test_job_page_shows_remaining_time_from_actual_pace(client_and_job):
    """v0.4.17:运行中的任务页按实际速度算剩余时间(已用时 ÷ 已完成 × 未完成)。"""
    from datetime import UTC, datetime, timedelta

    client, job, _ = client_and_job
    job.state = JobState.RUNNING
    job.phase = "phase_sampling"
    job.started_at = datetime.now(UTC) - timedelta(minutes=10)
    job.total_runs, job.completed_runs = 16, 4
    html = client.get(f"/jobs/{job.job_id}").text
    assert "预计还需约 30." in html  # 10 分钟完成 4/16 → 还需 30 分钟
    job.completed_runs = 0
    assert "第一批结果还没回来" in client.get(f"/jobs/{job.job_id}").text
    job.state, job.phase = JobState.DONE, "phase_done"


def test_manual_question_naming_the_brand_is_a_brand_question():
    """用户手填的问题:点了品牌名 / 别名 → 「直接询问品牌」;没点 → 「没有点名品牌」。
    点名的问题提及率天然接近 100%,归错场景会把「AI 会主动推荐你」的结论虚高(2026-09-21 真实页面暴露)。"""
    from engine.drafting import manual_intent
    from engine.models import PromptIntent

    brand = BrandSpec(name="始祖鸟", aliases=["Arc'teryx"], domains=["arcteryx.com"])
    assert manual_intent("始祖鸟的整体口碑和做工质量怎么样?", brand) is PromptIntent.BRAND_FACT
    assert manual_intent("is ARC'TERYX worth the price?", brand) is PromptIntent.BRAND_FACT  # 别名,不分大小写
    assert manual_intent("预算有限但想一步到位,哪些更值得先买?", brand) is PromptIntent.CATEGORY_DISCOVERY


def test_preselect_candidates_checks_everything_by_default():
    """产品文档 15.2:默认全测(24 道);不想测的由用户自己去掉,预估实时更新。"""
    from engine.models import PromptIntent, PromptOrigin
    from report.forms import preselect_candidates

    drafted = [PromptCandidate(origin=PromptOrigin.ASSISTED, intent=PromptIntent.CATEGORY_DISCOVERY, text="a"),
               PromptCandidate(origin=PromptOrigin.USER, intent=PromptIntent.BRAND_FACT, text="c")]
    assert preselect_candidates(drafted) == [0, 1]
    assert preselect_candidates([]) == []


def test_report_shows_competitor_mentions_with_percentages_and_rings_and_quotes_only_in_the_fold(client_and_job):
    """S7(用户定):竞品提及对比放主视图,计数 + 百分比 + 每名一环,差 ≥ 20 个百分点才说多 / 少;
    AI 原话只进折叠的证据区、正文只给一句指引;原话里的标签要转义。"""
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report?lang=zh").text
    view = user_view_of(html)
    assert "竞品提及对比(AI 回答里提到谁更多)" in view and "差 20 个百分点以上才说多或少" in view
    assert "12/30" in view and "40%" in view  # 你
    assert "竞品甲" in view and "20/30" in view and "67%" in view and "比你多" in view
    assert "竞品乙" in view and "10/30" in view and "33%" in view and "和你差不多" in view
    assert html.count('class="ring"') == 3  # 你 + 两个竞品,各一个环,不是一张饼
    assert "AI 的原话在页尾" in view
    assert "极光CRM 适合中小团队" in html and "极光CRM 适合中小团队" not in view  # 原话只在折叠区
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "第 2 次:" in html and "这次没提到你" in html and "这次没有有效回答" in html
    en = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "Competitor mentions (who the AI brings up more often)" in en and "more than you" in en


def test_report_names_ai_as_api_and_model_and_shows_sentiment_tally_with_pie(client_and_job):
    """S8.1(用户定 2026-10-14):AI 名字「接口 · 型号」+ 顶部一句「接口回答 ≠ 产品界面」;
    「AI 怎么评价你」= 提到你的 N 句话里正面 / 中性 / 负面各几句(百分比)+ 一张饼(四类互斥);计数句后面跟百分比。"""
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report?lang=zh").text
    view = user_view_of(html)
    assert "AI 的名字写成「接口 · 型号」" in view and "S1 · m1" in view
    assert "回答提及(AI 怎么评价你)" in view
    assert "提到你的 18 句话里:正面 1(6%)、中性 9(50%)、负面 8(44%)" in view  # 混合 0 不写
    assert html.count('class="pie"') == 1  # 只有 s1 有提到你的句子;s2 没有就不画
    assert "本次 30 次回答里,0 次(0%)拿你的官网当参考" in view  # 主视图「依据」:计数在前、百分比跟在后面
    en = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "Of the 18 sentences that mention you: positive 1(6%), neutral 9(50%), negative 8(44%)" in en
    assert "AI names are written as" in en


def test_summarize_orders_by_evidence_level_then_support():
    from report.summary import summarize

    job = finished_job()
    summary = summarize(job.class_reports, job.surface_verdicts, lambda surface, model: surface.upper())
    levels = [a.recommendation.evidence_level for a in summary.actions]
    assert levels == sorted(levels)
    assert all(a.support_count == 1 for a in summary.actions)  # s1 卡在引用、s2 卡在检索:各家各自的建议
    (brief_s1, brief_s2) = summary.engines[job.prompts[0].intent.value]
    assert (brief_s1.primary, brief_s2.primary) == (Stage.CITATION, Stage.RETRIEVAL)
    assert brief_s1.display == "S1" and brief_s1.strongest_evidence is not None  # 本测试自带大写的命名器
    assert [v.display for v in summary.verdicts] == ["S1", "S2"]
    assert summarize([], [], lambda surface, model: surface).verdicts == []


def test_retired_model_keeps_product_name_in_old_reports():
    """型号移出清单后(v0.4.23),历史报告里仍显示「产品名 · 型号」,不把内部名露给用户。"""

    class RetiredModelAdapter(DummyAdapter):
        def meta(self) -> SurfaceMeta:  # 清单里只剩 m-new;历史任务里的 m1 已移出
            return super().meta().model_copy(
                update={"models": ["m-new"], "default_model": "m-new", "est_cost_per_call_usd": {"m-new": 0.005}})

    app = create_app(call_stats=lambda: [], adapters={"s1": RetiredModelAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **_: None)
    job = finished_job()
    app.state.ctx.registry.add(job)
    html = TestClient(app).get(f"/jobs/{job.job_id}/report").text
    assert "S1 · m1" in html and "s1 · m1" not in html


def test_report_shows_upstream_caution_and_competitor_gap(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "不建议】进行全页" in html
    assert "落后竞品 竞品甲 20% 个百分点以上" in html


# ── 页面流 ───────────────────────────────────────────────────────────


def test_index_renders_form(client_and_job):
    client, _, _ = client_and_job
    html = client.get("/").text
    assert "品牌名" in html and "希望 AI 在回答哪些问题时提到你" in html


def test_manual_flow_to_confirm_and_launch(client_and_job):
    client, _, calls = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "极光CRM", "aliases": "AuroraCRM",
        "website": "aurora-crm.com", "category": "CRM",
        "questions": "问题一?\n问题二?", "generate": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    confirm_url = resp.headers["location"]
    html = client.get(confirm_url).text
    assert "问题一?" in html and "确认,开始检测" in html

    resp = client.post(confirm_url, data={
        "prompts_selected": ["0", "1"], "targets_selected": ["s1::m1"], "budget": "1.0", "custom": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    assert calls == [job_id]  # 执行器被触发
    job = client.app.state.ctx.registry.get(job_id)
    assert len(job.prompts) == 2 and job.fingerprint


def test_confirm_rejects_empty_selection(client_and_job):
    client, _, _ = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "X", "website": "x.com", "category": "C",
        "questions": "q1?", "generate": "",
    }, follow_redirects=False)
    confirm_url = resp.headers["location"]
    resp = client.post(confirm_url, data={"targets_selected": ["s1::m1"], "custom": ""},
                       follow_redirects=False)
    assert resp.status_code == 303 and "error=" in resp.headers["location"]


def test_unfinished_report_redirects_to_status(client_and_job):
    client, _, _ = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "X", "website": "x.com", "category": "C",
        "questions": "q1?", "generate": "",
    }, follow_redirects=False)
    job_id = resp.headers["location"].split("/")[2].split("?")[0]
    resp = client.get(f"/jobs/{job_id}/report", follow_redirects=False)
    assert resp.status_code == 303


# ── 预估与指纹 ────────────────────────────────────────────────────────


def test_confirm_estimate_prefers_measured_history():
    """预估口径(v0.4.17):有足量历史按实测均价与均时,没有的 AI 按声明单价,并在确认页说明依据。
    预估公式只在服务端:确认页的实时数字来自 /jobs/{id}/estimate(审查 #3)。"""
    from engine.storage import CallStat

    def run(stats):
        app = create_app(call_stats=lambda: stats, adapters={"s1": DummyAdapter()},
                         dsn="postgresql://unused", evidence=REGISTRY, job_runner=lambda **kw: None)
        client = TestClient(app)
        resp = client.post("/jobs", data={"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com",
                                          "category": "CRM", "questions": "问题一?", "generate": ""},
                           follow_redirects=False)
        page = client.get(resp.headers["location"]).text
        job_id = resp.headers["location"].split("/")[2]
        estimate = client.post(f"/jobs/{job_id}/estimate", data={
            "prompts_selected": ["0"], "targets_selected": ["s1::m1"]}).json()
        return page, estimate

    def stat(n: int) -> CallStat:
        return CallStat(surface="s1", model="m1", search_mode="default", n=n,
                        avg_cost_usd=0.02, avg_latency_ms=15000)

    page, est = run([stat(12)])
    assert "预估按过去实测的平均费用和耗时算:S1" in page
    assert est["calls"] == 3  # 1 道题 × 3 次
    assert est["cost_usd"] == pytest.approx(3 * 0.02) and est["minutes"] == pytest.approx(3 * 15 / 60 / 3)
    assert est["breakdown"] == [{"display": "S1 · m1", "cost_usd": pytest.approx(3 * 0.02)}]
    page, est = run([stat(3)])  # 历史不足:声明单价 + 兜底时长
    assert "S1 · m1 还没有足够的历史记录" in page  # 确认页也用「接口 · 型号」
    assert est["cost_usd"] == pytest.approx(3 * 0.005) and est["minutes"] == pytest.approx(3 * 20 / 60 / 3)


def test_estimate_job_arithmetic():
    from report.estimate import UnitPrices, estimate_job

    cheap, pricey = Target(surface="a", model="x"), Target(surface="b", model="y")
    prices = UnitPrices(default={cheap.key: 0.005, pricey.key: 0.17}, search_disabled={pricey.key: 0.04},
                        seconds_per_call=20.0, measured=[], declared=[cheap, pricey])
    # 按对象分价(v0.4.6):便宜通道与贵通道混选,成本逐个计价求和;次数固定(v0.4.26 S2)
    est = estimate_job(n_prompts=3, runs_per_prompt=3, targets=[cheap, pricey], dependency=False, prices=prices)
    assert est.calls == 18  # 3 题 × 3 次 × 2 对象
    assert est.cost_usd == pytest.approx(9 * (0.005 + 0.17))
    # 检索依赖对照:只有支持闭卷的对象多一条闭卷腿,按闭卷单价另计(每腿次数与开卷相同)
    est_dep = estimate_job(n_prompts=3, runs_per_prompt=3, targets=[cheap, pricey], dependency=True, prices=prices)
    assert est_dep.calls == 27
    assert est_dep.cost_usd == pytest.approx(9 * (0.005 + 0.17 + 0.04))
    assert est_dep.per_target == [(cheap, pytest.approx(9 * 0.005)), (pricey, pytest.approx(9 * (0.17 + 0.04)))]
    assert est_dep.minutes == pytest.approx(27 * 20 / 60 / 3)


def test_job_snapshot_round_trip():
    """快照往返(v0.4.9):冻结配置全保留;报告不入快照,续跑/重算时重建。"""
    job = finished_job()
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    assert restored.job_id == job.job_id
    assert restored.fingerprint == job.fingerprint  # 同快照同指纹,续跑不换基线
    assert restored.state is JobState.DONE and restored.phase == "phase_done"
    assert [p.prompt_id for p in restored.prompts] == [p.prompt_id for p in job.prompts]
    assert restored.targets == job.targets
    assert restored.dependency_enabled == job.dependency_enabled
    assert restored.class_reports == []


def test_resume_endpoint_relaunches_interrupted_job(client_and_job):
    client, job, calls = client_and_job
    job.state = JobState.INTERRUPTED
    resp = client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == [job.job_id]  # 执行器被再次触发(可重入,只补缺口)


def _sync_app(monkeypatch, runs: list):
    """线程换成同步执行;执行器桩只记下被怎么叫(不改状态)。"""

    class SyncThread:
        def __init__(self, *, target, kwargs, daemon):
            self._target, self._kwargs = target, kwargs

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr("report.launcher.threading.Thread", SyncThread)
    return create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                      evidence=REGISTRY, job_runner=lambda *, job, replay, **_: runs.append(replay))


def test_choosing_official_domains_updates_list_and_only_recomputes(monkeypatch):
    """报告列出主站承认的官网(附依据)与看起来像但主站没承认的网址;用户去掉 A 类 / 确认 B 类后:
    官网名单与口径指纹更新,按新名单只重算(replay,不向 AI 提问);没列出的网址一律不认;没改就不重算(S5)。"""
    from datetime import UTC, datetime

    from engine.models import BrandDomain, BrandDomainList, DomainEvidence, DomainStatus, DomainTier

    runs: list = []
    app = _sync_app(monkeypatch, runs)
    job = finished_job()
    job.adopt_domain_list(BrandDomainList(
        primary="aurora-crm.com", discovered_at=datetime(2026, 10, 13, tzinfo=UTC), public_suffix_list="test",
        domains=[
            BrandDomain(domain="aurora-crm.de", tier=DomainTier.A1, status=DomainStatus.CONFIRMED, bidirectional=True,
                        evidence=[DomainEvidence(kind="hreflang", source="https://aurora-crm.com/", detail="de"),
                                  DomainEvidence(kind="back_link")]),
            BrandDomain(domain="aurora-crm.jp", tier=DomainTier.B1, status=DomainStatus.UNCONFIRMED,
                        evidence=[DomainEvidence(kind="seen", detail="5")], seen_count=5),
        ],
    ))
    assert job.brand.domains == ["aurora-crm.com", "aurora-crm.de"]
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    page = user_view_of(client.get(f"/jobs/{job.job_id}/report").text)
    assert "aurora-crm.de" in page and "双向声明" in page and "aurora-crm.jp" in page and "出现 5 次" in page
    assert not any(token in page for token in USER_VIEW_FORBIDDEN)
    before = job.fingerprint
    resp = client.post(f"/jobs/{job.job_id}/official_domains",
                       data={"confirm": ["aurora-crm.jp", "evil.example"], "remove": ["aurora-crm.de"]},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert job.brand.domains == ["aurora-crm.com", "aurora-crm.jp"]  # 确认的进、去掉的出;evil.example 不在名单里,不认
    assert job.fingerprint != before and job.domain_list is not None and job.domain_list.user_changed
    assert runs == [True]  # 只重算
    resp = client.post(f"/jobs/{job.job_id}/official_domains", data={"confirm": ["evil.example"]},
                       follow_redirects=False)
    assert resp.status_code == 303 and runs == [True]  # 名单没变,不重算
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())  # 名单随快照冻结
    assert restored.domain_list == job.domain_list and restored.brand.domains == job.brand.domains


def test_regenerate_only_recomputes_but_resuming_an_interrupted_job_may_sample(monkeypatch):
    runs: list = []
    app = _sync_app(monkeypatch, runs)
    done, interrupted = finished_job(), finished_job()
    interrupted.state = JobState.INTERRUPTED
    for job in (done, interrupted):
        app.state.ctx.registry.add(job)
        TestClient(app).post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
        job.state = JobState.INTERRUPTED  # 桩不改状态;复位,免得影响下一单
    assert runs == [True, False]


def test_resume_clicked_twice_launches_one_runner(monkeypatch):
    """连点两下「接着跑」:执行器线程还没来得及改状态时,第二下也不能再起一个——
    同一单同时两个执行器会重复采样、重复花钱。线程换成同步执行,执行器桩不改状态(= 还没开跑)。"""

    class SyncThread:
        def __init__(self, *, target, kwargs, daemon):
            self._target, self._kwargs = target, kwargs

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr("report.launcher.threading.Thread", SyncThread)
    calls = []
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda *, job, **_: calls.append(job.job_id))
    job = finished_job()
    job.state = JobState.INTERRUPTED
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    for _ in range(2):
        assert client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False).status_code == 303
    assert calls == [job.job_id]


def test_resume_endpoint_regenerates_completed_job(client_and_job):
    """v0.4.16:已完成任务也可「重新生成报告」——追加一版快照,旧版本留档。"""
    client, job, calls = client_and_job
    resp = client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == [job.job_id]


def test_report_payload_round_trip_rehydrates_messages():
    """报告快照往返(v0.4.16):结论以码 + 参数存,嵌套 Msg 从 JSON 复活,渲染逐字一致。"""
    import json

    from diagnosis.messages import render_msg

    job = finished_job()
    payload = json.loads(json.dumps(job.report_payload()))  # 走一遍真实的 JSON 序列化
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    restored.load_report(payload)
    assert restored.class_reports == job.class_reports
    assert restored.surface_verdicts == job.surface_verdicts
    original = job.class_reports[0].diagnosis.surfaces[0]
    revived = restored.class_reports[0].diagnosis.surfaces[0]
    assert [render_msg(c, "zh") for c in revived.cautions] == [render_msg(c, "zh") for c in original.cautions]
    assert any("ACCESS" in render_msg(c, "zh") for c in revived.cautions)  # 嵌套阶段码复活后可渲染


def test_attach_latest_report_restores_report_and_spend(storage):
    """启动恢复(v0.4.16):已完成任务从库里装回最新一版报告与花费,首页直接「看报告」。"""
    from report.archive import attach_latest_report

    job = finished_job()
    job.spent_usd = 6.46
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    attach_latest_report(restored, storage)
    assert restored.class_reports == [] and restored.report_meta is None  # 没有快照就保持空,不编
    storage.insert_report_snapshot(job_id=job.job_id, fingerprint=job.fingerprint, code_version="t",
                                   spent_usd=job.spent_usd, payload=job.report_payload())
    attach_latest_report(restored, storage)
    assert restored.class_reports == job.class_reports and restored.spent_usd == 6.46
    assert restored.report_meta.versions == 1 and restored.report_meta.code_version == "t"


def test_attach_latest_report_skips_unreadable_snapshot(storage):
    """报告结构演进后旧快照读不动(v0.4.17 真实发生):不加兼容层,花费照常装回,
    报告留空让用户重新生成;不能让服务起不来。"""
    from report.archive import attach_latest_report

    job = finished_job()
    stale = job.report_payload()
    for report in stale["class_reports"]:
        for surface in report["diagnosis"]["surfaces"]:
            for rec in surface["recommendations"]:
                del rec["verify"]  # 模拟旧代码存的快照
    storage.insert_report_snapshot(job_id="stale-job", fingerprint="f", code_version="old",
                                   spent_usd=3.0, payload=stale)
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    restored.job_id = "stale-job"
    attach_latest_report(restored, storage)
    assert restored.class_reports == [] and restored.report_meta is None and restored.spent_usd == 3.0


def test_fingerprint_sensitive_to_dependency_flag():
    def build(dependency_enabled: bool) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(prompts=[Prompt(prompt_id="fixed", text="问法甲", intent=PromptIntent.CATEGORY_DISCOVERY,
                                   origin=PromptOrigin.USER, locale="zh-CN",
                                   created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=None,
                   dependency_enabled=dependency_enabled)
        return job.fingerprint

    assert build(False) != build(True)  # 闭卷对照开关是口径的一部分,必须进指纹


def test_fingerprint_stable_and_sensitive():
    def build(statement: str) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(prompts=[Prompt(prompt_id="fixed", text=statement, intent=PromptIntent.CATEGORY_DISCOVERY,
                                   origin=PromptOrigin.USER, locale="zh-CN",
                                   created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=None)
        return job.fingerprint

    assert build("问法甲") == build("问法甲")  # 同快照同指纹
    assert build("问法甲") != build("问法乙")  # 改题 = 新基线


# ── 启动恢复:已下线仪器 ────────────────────────────────────────────


def test_retired_surfaces_only_flags_surfaces_missing_from_registry():
    """v0.4.26 S1:被测对象里的仪器已不在 surfaces.yaml 上 → 任务不装回;清单上有、只是缺 key 的不算。"""
    from report.archive import retired_surfaces

    job = finished_job()  # 被测对象只有 s1
    assert retired_surfaces(job, known_surfaces={"s1", "s2"}) == []
    assert retired_surfaces(job, known_surfaces={"s2"}) == ["s1"]


def test_restore_registry_does_not_restore_jobs_with_retired_surface(storage):
    """含已下线仪器的任务:快照留库,但启动时不装回(首页不列,点不到会炸的报告)。"""
    from report.archive import restore_registry

    job = finished_job()
    storage.upsert_job_snapshot(job.to_snapshot())
    restored_without = {j.job_id for j in restore_registry(storage, known_surfaces={"s2"}).recent(limit=100)}
    restored_with = {j.job_id for j in restore_registry(storage, known_surfaces={"s1"}).recent(limit=100)}
    assert job.job_id not in restored_without
    assert job.job_id in restored_with
