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
from diagnosis.diagnose import access_finding, diagnose_need, diagnose_surface
from diagnosis.findings import Stage
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.recommender import load_evidence_registry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_dependency import diagnose_retrieval_dependency
from diagnosis.retrieval_diag import diagnose_retrieval
from diagnosis.verdict import SurfaceVerdict
from engine.aggregate import NeedSurfaceMetric
from engine.metrics import account_outcomes, estimate_proportion
from engine.models import (
    BrandSpec,
    InformationNeed,
    NeedIntent,
    Outcome,
    Sentiment,
    StopReason,
    Target,
)
from engine.needs import NeedCandidate
from report.app import create_app
from report.jobs import TIERS, DiagnosisJob, JobState, NeedReport

REGISTRY = load_evidence_registry()


class DummyAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="S1", provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1",
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={"m1": 0.005})

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def make_metric(k: int, n: int) -> NeedSurfaceMetric:
    return NeedSurfaceMetric(
        information_need_id="need-1", provider="test_provider", surface="s1", model="m1",
        metric_label="test", k_hits=k,
        estimate=estimate_proportion(k, n),
        accounting=account_outcomes([Outcome.SUCCESS] * n),
        stop_reasons=[StopReason.CI_CONVERGED, StopReason.MAX_N_REACHED],
        per_variant=[],
        variant_estimates=[estimate_proportion(k, n)],
    )


def finished_job() -> DiagnosisJob:
    brand = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
    need = InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)
    access = access_finding(None)
    # v0.4.11 分家:两家 surface 各自分诊——s1 借裸检索视角,s2 自带生成端链路
    bare_block = diagnose_surface(
        provider="test_provider", surface="s1", model="m1",
        access=access,
        retrieval=diagnose_retrieval(make_metric(0, 30), []),
        citation=diagnose_citation(
            make_metric(0, 30),
            [("竞品甲", estimate_proportion(20, 30))],
            verification=VerificationSummary(
                attempted=2, verified_match=0, content_mismatch=1,
                fetch_blocked=1, unparsed=0, unreachable=0,
            ),
        ),
        representation=diagnose_representation(
            NeedIntent.DISCOVERY, make_metric(20, 30), {Sentiment.POSITIVE: 5}
        ),
        intent=NeedIntent.DISCOVERY, registry=REGISTRY,
        # 开卷 20/30 vs 闭卷 2/30:区间分离且闭卷上界不足开卷下界一半 → HIGH
        retrieval_dependency=diagnose_retrieval_dependency(
            make_metric(20, 30), make_metric(2, 30)
        ),
    )
    generative_block = diagnose_surface(
        provider="test_provider", surface="s2", model="m2",
        access=access,
        # 生成端链路作为该家自己的 RETRIEVAL:在场 0/30 → FAIL
        retrieval=diagnose_generative_retrieval(
            make_metric(0, 30), [],
            estimate_proportion(1, 30), estimate_proportion(0, 30), estimate_proportion(25, 30),
        ),
        citation=diagnose_citation(make_metric(0, 30), [], verification=None),
        representation=diagnose_representation(NeedIntent.DISCOVERY, make_metric(20, 30), {}),
        intent=NeedIntent.DISCOVERY, registry=REGISTRY,
    )
    diagnosis = diagnose_need(need, access=access, surfaces=[bare_block, generative_block])
    job = DiagnosisJob(brand=brand, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(needs=[need], competitors=[], targets=[Target(surface="s1", model="m1")], tier_id="quick",
               variants_per_need=2, budget_usd=1.0)
    job.state = JobState.DONE
    job.phase = "完成"
    job.finished_at = datetime(2026, 8, 24, tzinfo=UTC)
    job.need_reports = [NeedReport(need=need, variant_texts=["中小团队用什么 CRM 好?"],
                                   diagnosis=diagnosis)]
    job.surface_verdicts = [  # 每家 AI 一句话(v0.4.14):s2 缺「直接问起」场景,句尾说明
        SurfaceVerdict(surface="s1", model="m1", code="verdict.only_when_named", missing=[]),
        SurfaceVerdict(surface="s2", model="m2", code="verdict.known_sometimes_site_rare",
                       missing=["verdict.missing.probe"]),
    ]
    return job


@pytest.fixture()
def client_and_job():
    calls = []

    def stub_runner(*, job, adapters, dsn, evidence):
        calls.append(job.job_id)
        job.state = JobState.DONE
        job.phase = "完成"

    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=stub_runner)
    job = finished_job()
    app.state.registry.add(job)
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
    assert "95% 可信区间(Jeffreys 先验)" in html  # v0.4.13:可信区间,不冒充频率学派置信区间
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
    "NOT_OBSERVABLE", "N_valid", "stop_reasons", "Jeffreys", "冻结问法集", "候选在场率", "入场率",
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
    assert "用户主动问起时 S1 能介绍你,但用户不提你时 S1 几乎不会想到你" in html  # 显示名代入
    assert "用户直接问起你时的表现本次没测" in html  # 缺项句尾说明
    assert "🔴 较弱" in html and "🟡 不够稳定" in html  # 红绿灯 + 定性词(夹具无 PASS 项)
    assert "有 2 处检测结果支持这条建议" in html
    assert html.count('class="rec"') == 3  # 去重:两家 × 三条 → 三条动作
    assert "用户没有点名品牌时(品类推荐)" in html  # 场景人话
    assert "AI 搜索时有没有找到你(RETRIEVAL)" in html  # 人话(技术词)双写
    assert "本次 30 次搜索里,0 次结果中出现了你的官网" in html  # 比例用原始计数
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


def test_job_page_shows_remaining_time_from_actual_pace(client_and_job):
    """v0.4.17:运行中的任务页按实际速度算剩余时间(已用时 ÷ 已完成 × 未完成)。"""
    from datetime import UTC, datetime, timedelta

    client, job, _ = client_and_job
    job.state = JobState.RUNNING
    job.phase = "phase_sampling"
    job.started_at = datetime.now(UTC) - timedelta(minutes=10)
    job.total_plans, job.completed_plans = 16, 4
    html = client.get(f"/jobs/{job.job_id}").text
    assert "预计还需约 30." in html  # 10 分钟完成 4/16 → 还需 30 分钟
    job.completed_plans = 0
    assert "第一批结果还没回来" in client.get(f"/jobs/{job.job_id}").text
    job.state, job.phase = JobState.DONE, "phase_done"


def test_tier_labels_match_policy_arithmetic():
    """v0.4.15 档位按次数定义:宽度阈值在实际参数下等价于「前 N 次完全一样才提前停」,
    混合结果永不提前停;标签里的数字必须与策略算出的一致。"""
    from engine.metrics import jeffreys_interval
    from report.i18n import MESSAGES

    def width(k: int, n: int) -> float:
        lo, hi = jeffreys_interval(k, n)
        return hi - lo

    for tier_id, expected in (("quick", (8, 6)), ("standard", (20, 11))):
        policy = TIERS[tier_id].policy
        unanimous = next(
            n for n in range(policy.min_n, policy.max_n + 1) if width(0, n) <= policy.ci_width_threshold
        )
        assert (policy.max_n, unanimous) == expected
        assert all(
            width(k, n) > policy.ci_width_threshold
            for n in range(policy.min_n, policy.max_n + 1)
            for k in range(1, n)
        ), tier_id  # 混合结果永不提前停
        for lang in ("zh", "en"):
            label = MESSAGES[lang][f"tier_{tier_id}"]
            assert str(policy.max_n) in label and str(unanimous) in label, (tier_id, lang)


def test_tier_table_matches_policy_arithmetic():
    """v0.4.18 两档对照表:能精确算的部分用算术机械核对(其余数字来自 scripts/mc_tier_lights.py)。
    表头写的「每个问题、每家 AI 一共问多少次」= 3 种说法 × [全一致即停的次数, 上限];
    快扫档「从来不提才亮红灯」= 3 种说法全不中(18 次)够红,只要中 1 次(那种说法就得问满)就不够红。"""
    from diagnosis.findings import DiagnosisThresholds
    from engine.metrics import jeffreys_interval
    from report.i18n import MESSAGES, TIER_TABLE

    red_line = DiagnosisThresholds().mention_fail_ci_high
    for lang in ("zh", "en"):
        caption = MESSAGES[lang]["tier_table_caption"]
        for tier_id, unanimous in (("quick", 6), ("standard", 11)):
            assert f"{3 * unanimous}–{3 * TIERS[tier_id].policy.max_n}" in caption, (tier_id, lang)
        assert all(len(row) == 3 for row in TIER_TABLE[lang])
    assert len(TIER_TABLE["zh"]) == len(TIER_TABLE["en"])
    quick = TIERS["quick"].policy
    assert jeffreys_interval(0, 3 * 6)[1] <= red_line
    assert jeffreys_interval(1, 2 * 6 + quick.max_n)[1] > red_line
    assert round(0.95 ** 18, 1) == 0.4  # 「20 次里提到 1 次」快扫档亮红灯的把握:约 4 成


def test_manual_question_naming_the_brand_is_a_brand_question():
    """用户手填的问题:点了品牌名 / 别名 → 「直接询问品牌」;没点 → 「没有点名品牌」。
    点名的问题提及率天然接近 100%,归错场景会把「AI 会主动推荐你」的结论虚高(2026-09-21 真实页面暴露)。"""
    from engine.models import NeedIntent
    from report.jobs import manual_intent

    brand = BrandSpec(name="始祖鸟", aliases=["Arc'teryx"], domains=["arcteryx.com"])
    assert manual_intent("始祖鸟的整体口碑和做工质量怎么样?", brand) is NeedIntent.BRAND_PROBE
    assert manual_intent("is ARC'TERYX worth the price?", brand) is NeedIntent.BRAND_PROBE  # 别名,不分大小写
    assert manual_intent("预算有限但想一步到位,哪些更值得先买?", brand) is NeedIntent.DISCOVERY


def test_preselect_candidates_defaults_to_two_or_all_manual():
    """v0.4.18 默认报价要便宜:一道没手填 → 只勾第一个不点名品牌的题 + 第一个品牌题;
    手填了 → 手填的全勾、系统出的不勾。"""
    from engine.models import NeedIntent
    from engine.needs import NeedCandidate
    from report.jobs import preselect_candidates

    drafted = [NeedCandidate(intent=NeedIntent.DISCOVERY, text="a"),
               NeedCandidate(intent=NeedIntent.COMPARISON, text="b"),
               NeedCandidate(intent=NeedIntent.BRAND_PROBE, text="c"),
               NeedCandidate(intent=NeedIntent.BRAND_PROBE, text="d")]
    assert preselect_candidates(drafted, n_manual=0) == [0, 2]
    manual = [NeedCandidate(intent=NeedIntent.DISCOVERY, text="m1"),
              NeedCandidate(intent=NeedIntent.DISCOVERY, text="m2")]
    assert preselect_candidates(drafted + manual, n_manual=2) == [4, 5]
    assert preselect_candidates(manual, n_manual=2) == [0, 1]


def test_summarize_orders_by_evidence_level_then_support():
    from report.summary import summarize

    job = finished_job()
    summary = summarize(job.need_reports, job.surface_verdicts, lambda surface, model: surface.upper())
    levels = [a.recommendation.evidence_level for a in summary.actions]
    assert levels == sorted(levels)
    assert all(a.support_count == 2 for a in summary.actions)
    (brief_s1, brief_s2) = summary.engines[job.needs[0].need_id]
    assert brief_s1.primary is brief_s2.primary is Stage.RETRIEVAL
    assert brief_s1.display == "S1" and brief_s1.strongest_evidence is not None
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
    app.state.registry.add(job)
    html = TestClient(app).get(f"/jobs/{job.job_id}/report").text
    assert "S1 · m1" in html and "s1 · m1" not in html


def test_report_shows_upstream_caution_and_competitor_gap(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "不建议】进行全页" in html
    assert "显著落后于竞品 竞品甲" in html


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
        "needs_selected": ["0", "1"], "targets_selected": ["s1::m1"],
        "tier": "quick", "variants": "2", "budget": "1.0", "custom": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    assert calls == [job_id]  # 执行器被触发
    job = client.app.state.registry.get(job_id)
    assert len(job.needs) == 2 and job.fingerprint


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
            "needs_selected": ["0"], "targets_selected": ["s1::m1"], "tier": "quick", "variants": "3"}).json()
        return page, estimate

    def stat(n: int) -> CallStat:
        return CallStat(surface="s1", model="m1", search_mode="default", n=n,
                        avg_cost_usd=0.02, avg_latency_ms=15000)

    page, est = run([stat(12)])
    assert "预估按过去实测的平均费用和耗时算:S1" in page
    assert est["calls"] == 23  # 1 题 × 3 问法 × 7.5 = 22.5,四舍五入
    assert est["cost_usd"] == pytest.approx(22.5 * 0.02) and est["minutes"] == pytest.approx(22.5 * 15 / 60 / 3)
    assert est["breakdown"] == [{"display": "S1", "cost_usd": pytest.approx(22.5 * 0.02)}]
    page, est = run([stat(3)])  # 历史不足:声明单价 + 兜底时长
    assert "S1 还没有足够的历史记录" in page
    assert est["cost_usd"] == pytest.approx(22.5 * 0.005) and est["minutes"] == pytest.approx(22.5 * 20 / 60 / 3)


def test_estimate_job_arithmetic():
    from report.estimate import UnitPrices, estimate_job

    cheap, pricey = Target(surface="a", model="x"), Target(surface="b", model="y")
    prices = UnitPrices(default={cheap.key: 0.005, pricey.key: 0.17}, search_disabled={pricey.key: 0.04},
                        seconds_per_call=20.0, measured=[], declared=[cheap, pricey])
    # 按对象分价(v0.4.6):便宜通道与贵通道混选,成本逐个计价求和
    est = estimate_job(n_needs=3, variants_per_need=2, targets=[cheap, pricey], tier_id="quick",
                       dependency=False, prices=prices)
    assert est.calls == 90  # 3 题 × 2 问法 × 2 对象 × 7.5 期望采样
    assert est.cost_usd == pytest.approx(3 * 2 * 7.5 * (0.005 + 0.17))
    # 检索依赖对照:只有支持闭卷的对象多一条闭卷腿,按闭卷单价另计(每腿采样策略与开卷相同)
    est_dep = estimate_job(n_needs=3, variants_per_need=2, targets=[cheap, pricey], tier_id="quick",
                           dependency=True, prices=prices)
    assert est_dep.calls == 135
    assert est_dep.cost_usd == pytest.approx(3 * 2 * 7.5 * (0.005 + 0.17 + 0.04))
    assert est_dep.per_target == [(cheap, pytest.approx(45 * 0.005)), (pricey, pytest.approx(45 * (0.17 + 0.04)))]
    assert est_dep.minutes == pytest.approx(135 * 20 / 60 / 3)


def test_job_snapshot_round_trip():
    """快照往返(v0.4.9):冻结配置全保留;报告不入快照,续跑/重算时重建。"""
    job = finished_job()
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    assert restored.job_id == job.job_id
    assert restored.fingerprint == job.fingerprint  # 同快照同指纹,续跑不换基线
    assert restored.state is JobState.DONE and restored.phase == "phase_done"
    assert [n.need_id for n in restored.needs] == [n.need_id for n in job.needs]
    assert restored.targets == job.targets and restored.tier_id == job.tier_id
    assert restored.dependency_enabled == job.dependency_enabled
    assert restored.need_reports == []


def test_resume_endpoint_relaunches_interrupted_job(client_and_job):
    client, job, calls = client_and_job
    job.state = JobState.INTERRUPTED
    resp = client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == [job.job_id]  # 执行器被再次触发(可重入,只补缺口)


def test_resume_clicked_twice_launches_one_runner(monkeypatch):
    """连点两下「接着跑」:执行器线程还没来得及改状态时,第二下也不能再起一个——
    同一单同时两个执行器会重复采样、重复花钱。线程换成同步执行,执行器桩不改状态(= 还没开跑)。"""

    class SyncThread:
        def __init__(self, *, target, kwargs, daemon):
            self._target, self._kwargs = target, kwargs

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr("report.app.threading.Thread", SyncThread)
    calls = []
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda *, job, **_: calls.append(job.job_id))
    job = finished_job()
    job.state = JobState.INTERRUPTED
    app.state.registry.add(job)
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
    assert restored.need_reports == job.need_reports
    assert restored.surface_verdicts == job.surface_verdicts
    original = job.need_reports[0].diagnosis.surfaces[0]
    revived = restored.need_reports[0].diagnosis.surfaces[0]
    assert [render_msg(c, "zh") for c in revived.cautions] == [render_msg(c, "zh") for c in original.cautions]
    assert any("ACCESS" in render_msg(c, "zh") for c in revived.cautions)  # 嵌套阶段码复活后可渲染


def test_attach_latest_report_restores_report_and_spend(storage):
    """启动恢复(v0.4.16):已完成任务从库里装回最新一版报告与花费,首页直接「看报告」。"""
    from report.jobs import attach_latest_report

    job = finished_job()
    job.spent_usd = 6.46
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    attach_latest_report(restored, storage)
    assert restored.need_reports == [] and restored.report_meta is None  # 没有快照就保持空,不编
    storage.insert_report_snapshot(job_id=job.job_id, fingerprint=job.fingerprint, code_version="t",
                                   spent_usd=job.spent_usd, payload=job.report_payload())
    attach_latest_report(restored, storage)
    assert restored.need_reports == job.need_reports and restored.spent_usd == 6.46
    assert restored.report_meta.versions == 1 and restored.report_meta.code_version == "t"


def test_attach_latest_report_skips_unreadable_snapshot(storage):
    """报告结构演进后旧快照读不动(v0.4.17 真实发生):不加兼容层,花费照常装回,
    报告留空让用户重新生成;不能让服务起不来。"""
    from report.jobs import attach_latest_report

    job = finished_job()
    stale = job.report_payload()
    for report in stale["need_reports"]:
        for surface in report["diagnosis"]["surfaces"]:
            for rec in surface["recommendations"]:
                del rec["verify"]  # 模拟旧代码存的快照
    storage.insert_report_snapshot(job_id="stale-job", fingerprint="f", code_version="old",
                                   spent_usd=3.0, payload=stale)
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    restored.job_id = "stale-job"
    attach_latest_report(restored, storage)
    assert restored.need_reports == [] and restored.report_meta is None and restored.spent_usd == 3.0


def test_fingerprint_sensitive_to_dependency_flag():
    def build(dependency_enabled: bool) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(needs=[InformationNeed(need_id="fixed", statement="问法甲",
                                          intent=NeedIntent.DISCOVERY,
                                          created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], targets=[Target(surface="s1", model="m1")], tier_id="quick",
                   variants_per_need=2, budget_usd=None,
                   dependency_enabled=dependency_enabled)
        return job.fingerprint

    assert build(False) != build(True)  # 闭卷对照开关是口径的一部分,必须进指纹


def test_fingerprint_stable_and_sensitive():
    def build(statement: str) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(needs=[InformationNeed(need_id="fixed", statement=statement,
                                          intent=NeedIntent.DISCOVERY,
                                          created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], targets=[Target(surface="s1", model="m1")], tier_id="quick",
                   variants_per_need=2, budget_usd=None)
        return job.fingerprint

    assert build("问法甲") == build("问法甲")  # 同快照同指纹
    assert build("问法甲") != build("问法乙")  # 改题 = 新基线


def test_candidate_default_check_logic():
    # 确认页默认勾选前 4 条与 brand_probe(模板逻辑的输入侧约束:candidates 顺序稳定)
    cand = NeedCandidate(intent=NeedIntent.BRAND_PROBE, text="X 怎么样?")
    assert cand.intent is NeedIntent.BRAND_PROBE
