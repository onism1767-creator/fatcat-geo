"""执行器端到端(v0.4.19):同一台仪器的两个型号各测各的、各报各的,样本互不串。

走真实执行器 + 真实调度器 + 真实存储(测试库)+ 真实诊断,只把两处联网动作换成桩:
官网抓取审计与被引 URL 重抓(12.3:单测不许碰真实网络)。适配器是按型号给不同回答的假仪器。
"""

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from conftest import TEST_DSN

from adapters.base import AdapterAccountError, CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.findings import Stage, StageStatus
from diagnosis.recommender import load_evidence_registry
from engine.detectors.matching import registrable
from engine.models import (
    Answer,
    AskRequest,
    BrandDomainList,
    BrandSpec,
    Citation,
    CitationResolution,
    Outcome,
    Prompt,
    PromptIntent,
    PromptOrigin,
    ResolutionStatus,
    RunError,
    RunErrorType,
    SearchTrace,
    SurfaceRun,
    Target,
)
from engine.page_check import PageIndex
from report import runner
from report.jobs import DiagnosisJob, JobState
from tests.unit.test_report_page import LOW

BRAND = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
CITES, SILENT = "m-cites", "m-silent"


class TwoModelAdapter(SurfaceAdapter):
    """同一台仪器、两个型号:一个每次都引用官网并提到品牌,一个从不。
    broke_model 非空时,该型号的下一次调用撞「账户没钱」(模拟额度跑空),之后恢复正常。"""

    def __init__(self, broke_model: str | None = None) -> None:
        self.broke_model = broke_model

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="假 AI", api_name="假 AI", provider="test_provider", surface="two_model",
                           models=[CITES, SILENT], default_model=CITES,
                           consumer_default_model=CITES, strongest_model=CITES, models_checked="test",
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={CITES: 0.01, SILENT: 0.001})

    def ask(self, request: AskRequest) -> SurfaceRun:
        assert request.model in (CITES, SILENT)  # 执行器必须把型号显式传下来,不许落回默认型号
        if request.model == self.broke_model:
            self.broke_model = None
            raise AdapterAccountError(
                "账户额度不足或欠费(HTTP 400): credit balance is too low", error_type=RunErrorType.ACCOUNT_CREDIT,
                http_status=400, response_body='{"error":{"message":"Your credit balance is too low"}}',
            )
        cites = request.model == CITES
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id, run_index=request.run_index,
            provider="test_provider", surface="two_model",
            model=request.model, search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS, search_trace=SearchTrace(search_triggered=True),
            answer=Answer(
                answer_text="推荐极光CRM。" if cites else "推荐别家。",
                citations_available=True,
                citations=[Citation(url="https://aurora-crm.com/a", domain="aurora-crm.com", position=1)]
                if cites else [],
            ),
            raw_payload={"model": request.model},
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.01 if cites else 0.001,
        )

    def healthcheck(self) -> bool:
        return True


class _NoNetworkChecker:
    """不联网的网页核实:什么都没开(全部记「没来得及开」)。"""

    def __init__(self, storage, **_) -> None:
        pass

    def check(self, requests) -> PageIndex:
        return PageIndex()

    def close(self) -> None:
        pass


class _StubResolver:
    """不联网的还原器:表里有的链接算还原成功,其余记「没跳」。"""

    destinations: dict[str, str] = {}

    def __init__(self, storage) -> None:
        pass

    def resolve(self, urls: list[str]) -> dict[str, CitationResolution]:
        result = {}
        for url in urls:
            final = self.destinations.get(url)
            result[url] = CitationResolution(
                url=url, final_url=final, hops=1 if final else 0, resolver_version=1,
                status=ResolutionStatus.RESOLVED if final else ResolutionStatus.NOT_REDIRECTED,
            )
        return result

    def close(self) -> None:
        pass


class _StubDiscovery:
    """不联网的官网名单发现:名单只有主站;采样后的同名域名原样不加。"""

    def __init__(self, platforms, **_) -> None:
        pass

    def discover(self, primary_url: str) -> BrandDomainList:
        host = urlsplit(primary_url).hostname or ""
        return BrandDomainList(primary=registrable(host)[1], discovered_at=datetime.now(UTC), public_suffix_list="test")

    def look_alikes(self, domain_list: BrandDomainList, candidates) -> BrandDomainList:
        return domain_list

    def close(self) -> None:
        pass


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setattr(runner.access_audit, "audit", lambda website: None)
    monkeypatch.setattr(runner, "PageChecker", _NoNetworkChecker)
    monkeypatch.setattr(runner, "UrlResolver", _StubResolver)
    monkeypatch.setattr(runner, "BrandDomainDiscovery", _StubDiscovery)


def _citation_status(job: DiagnosisJob) -> dict[str, StageStatus]:
    [report] = job.class_reports
    return {
        block.model: next(f.status for f in block.findings if f.stage is Stage.CITATION)
        for block in report.diagnosis.surfaces
    }


def _new_job(text: str, tag: str | None = None) -> tuple[DiagnosisJob, Prompt]:
    """每个测试各自的题(带随机尾巴):回答缓存(S11.2)会复用同品牌同题的回答,测试之间不能串;要同题就传同一个 tag。"""
    tag = tag or uuid4().hex[:6]
    prompt = Prompt(text=f"{text}({tag})", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER,
                    locale="zh-CN")
    job = DiagnosisJob(brand=BRAND, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(prompts=[prompt], competitors=[],
               targets=[Target(surface="two_model", model=CITES), Target(surface="two_model", model=SILENT)],
               budget_usd=None, tier=LOW)
    return job, prompt


def test_two_models_of_one_surface_are_sampled_and_diagnosed_apart(storage, offline):
    job, prompt = _new_job("中小团队用什么 CRM 好?")
    adapters = {"two_model": TwoModelAdapter()}
    evidence = load_evidence_registry()

    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=evidence)

    assert job.state is JobState.DONE, job.error
    assert job.total_runs == 2 * LOW.runs_per_prompt == job.completed_runs  # 1 道题 × 3 次(低档)× 2 个型号
    runs = storage.runs_for_prompt(prompt.prompt_id, surface="two_model")
    per_model = {m: [r for r in runs if r.model == m] for m in (CITES, SILENT)}
    assert {m: sorted(r.run_index for r in rs) for m, rs in per_model.items()} == {CITES: [1, 2, 3], SILENT: [1, 2, 3]}
    # 各报各的:两个型号各有一个诊断块、各有一句话,结论不同(3/3 → 良好;0/3 → 较弱)
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.FAIL}
    assert {(v.surface, v.model) for v in job.surface_verdicts} == {("two_model", CITES), ("two_model", SILENT)}
    assert job.spent_usd == pytest.approx(3 * 0.01 + 3 * 0.001)
    from engine.method import METHOD_VERSION

    note = job.sampling  # 采样说明(S8.2):实际问了多少、有效多少、测了谁、方法版本
    assert note is not None and (note.n_prompts, note.n_targets, note.tier, note.n_runs) == (1, 2, LOW, 6)
    assert note.n_valid == 6 and note.n_offline_runs == 0 and note.method_version == METHOD_VERSION
    assert note.started_at is not None and note.finished_at is not None and note.finished_at == job.finished_at
    assert {(x.surface, x.model) for x in note.targets} == {("two_model", CITES), ("two_model", SILENT)}
    assert job.verify_started_at is not None and job.verify_total >= 1  # 核对网页阶段有开始时刻与总数(S8.5)

    # 续跑(重新生成报告):样本按「题 × 仪器 × 型号 × 模式 × 第几次」复用——零新调用,且两个型号的
    # 样本不互相充当槽位(串了的话两边都会变成 3/6,结论双双变黄)
    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=evidence, replay=True)
    assert job.state is JobState.DONE, job.error
    assert len(storage.runs_for_prompt(prompt.prompt_id, surface="two_model")) == 6
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.FAIL}


def test_account_problem_halts_job_plainly_and_resume_keeps_account_rows_out_of_metrics(storage, offline):
    """v0.4.21:账户没钱 → 任务立刻停、任务页说人话、不再花钱;充值后「接着跑」——账户问题行
    照常在库里,但不进分母、不进失败率、不触发熔断;两型号结论与正常跑完全一致。"""
    job, prompt = _new_job("小团队用什么 CRM?")
    evidence = load_evidence_registry()

    runner.execute_job(job=job, adapters={"two_model": TwoModelAdapter(broke_model=SILENT)},
                       dsn=TEST_DSN, evidence=evidence)
    assert job.state is JobState.FAILED
    assert job.error_code == "ACCOUNT_CREDIT"  # 充值那句,不是换 key 那句
    assert job.error_params["ai"] == "假 AI" and job.error_params["model"] == SILENT
    assert "credit balance" in job.error_params["detail"] and job.error is None
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())  # 出错说明进快照:去充值期间重启也不丢
    assert (restored.error_code, restored.error_params) == (job.error_code, job.error_params)
    assert [p.text for p in restored.prompts] == [prompt.text]
    rows = storage.runs_for_prompt(prompt.prompt_id, surface="two_model", model=SILENT)
    assert len(rows) == 1 and rows[0].error.error_type is RunErrorType.ACCOUNT_CREDIT  # 失败不蒸发

    # 再塞两条「v0.4.21 之前」风格的账户问题行(当时只会记成 PERMANENT):原始层不可改,
    # 续跑读出时必须按同一条规则重新解读——否则 2/5 的"失败率"会把结论压成低置信
    for i in range(2):
        storage.insert_surface_run(rows[0].model_copy(update={
            "run_id": f"legacy-account-row-{i}",
            "error": RunError(error_type=RunErrorType.PERMANENT, message="非法请求 HTTP 400", http_status=400),
            "raw_payload": {"error_response_text": '{"error":{"message":"Your credit balance is too low"}}'},
        }))

    runner.execute_job(job=job, adapters={"two_model": TwoModelAdapter()}, dsn=TEST_DSN, evidence=evidence)
    assert job.state is JobState.DONE, job.error
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.FAIL}
    [report] = job.class_reports
    silent_block = next(b for b in report.diagnosis.surfaces if b.model == SILENT)
    citation = next(f for f in silent_block.findings if f.stage is Stage.CITATION)
    assert not citation.low_confidence  # 三条账户行都没有被算成失败率
    rows = storage.runs_for_prompt(prompt.prompt_id, surface="two_model", model=SILENT)
    assert sum(r.outcome is Outcome.SUCCESS for r in rows) == 3 and len(rows) == 6  # 3 次有效 + 3 条账户行


class RedirectAdapter(SurfaceAdapter):
    """给跳转链当引用的假仪器(能力 citation_urls_redirect):两条引用,一条能还原到官网、一条还原不出去向。"""

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="假跳转 AI", api_name="假跳转 AI", provider="test_provider",
                           surface="redirect_ai",
                           models=["r1"], default_model="r1",
                           consumer_default_model="r1", strongest_model="r1", models_checked="test",
                           capabilities=CapabilityRegistry(supports_citations=True, citation_urls_redirect=True),
                           est_cost_per_call_usd={"r1": 0.01})

    def ask(self, request: AskRequest) -> SurfaceRun:
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id, run_index=request.run_index,
            provider="test_provider", surface="redirect_ai", model="r1",
            search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS, search_trace=SearchTrace(search_triggered=True),
            answer=Answer(answer_text="推荐极光CRM。", citations_available=True, citations=[
                Citation(url="https://redir.test/ok", domain="redir.test", title="aurora-crm.com", position=1),
                Citation(url="https://redir.test/lost", domain="redir.test", title="aurora-crm.com", position=2),
            ]),
            raw_payload={}, timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.01,
        )

    def healthcheck(self) -> bool:
        return True


def test_redirect_citations_are_judged_by_resolved_destination_not_title(storage, offline):
    """S4:跳转仪器的引用按还原后的终点判——能还原到官网的算被引,还原不出的记看不到;标题不作依据。"""
    _StubResolver.destinations = {"https://redir.test/ok": "https://www.aurora-crm.com/landing"}
    prompt = Prompt(text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY,
                    origin=PromptOrigin.USER, locale="zh-CN")
    job = DiagnosisJob(brand=BRAND, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(prompts=[prompt], competitors=[], targets=[Target(surface="redirect_ai", model="r1")], budget_usd=None,
               tier=LOW)
    try:
        runner.execute_job(job=job, adapters={"redirect_ai": RedirectAdapter()}, dsn=TEST_DSN,
                           evidence=load_evidence_registry())
    finally:
        _StubResolver.destinations = {}
    assert job.state is JobState.DONE, job.error
    [report] = job.class_reports
    [block] = report.diagnosis.surfaces
    citation = next(f for f in block.findings if f.stage is Stage.CITATION)
    assert citation.status is StageStatus.PASS  # 3/3 回答都有一条还原到官网的引用
    codes = {line.code for line in citation.evidence_lines}
    assert {"citation.primary_rate", "citation.by_domain", "citation.unresolved"} <= codes
    unresolved = next(line for line in citation.evidence_lines if line.code == "citation.unresolved")
    assert unresolved.params["n"] == 3  # 每条回答一条还原不出去向的引用,记看不到
    by_domain = next(line for line in citation.evidence_lines if line.code == "citation.by_domain")
    assert [(item.params["domain"], item.params["k"]) for item in by_domain.params["items"]] == [("aurora-crm.com", 3)]


def test_high_tier_asks_a_judge_once_per_answer_and_reuses_it_on_replay(storage, offline):
    """S9.3 高档:另一家 AI 逐条复核;一条回答只判一次,费用进账本;重新生成报告时装回库里的判定,不再花钱。"""
    from engine.judge import JudgeConfig, JudgeModel
    from engine.tiers import MentionMethod, TierChoice
    from tests.unit.test_judge import ScriptedJudge

    job, prompt = _new_job("中小团队用什么 CRM 好?")
    job.tier = TierChoice(name="high", runs_per_prompt=3, mention_method=MentionMethod.RULES_FUZZY_JUDGE, custom=False)
    judges = {"judge_a": ScriptedJudge("judge_a", "vendor_a", "ja"),
              "judge_b": ScriptedJudge("judge_b", "vendor_b", "jb")}
    config = JudgeConfig(primary=JudgeModel(surface="judge_a", model="ja", est_cost_per_call_usd=0.001),
                         backup=JudgeModel(surface="judge_b", model="jb", est_cost_per_call_usd=0.001))
    adapters = {"two_model": TwoModelAdapter(), **judges}
    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=load_evidence_registry(), judges=config)
    assert job.state is JobState.DONE, job.error
    assert len(judges["judge_a"].requests) == 6 and judges["judge_b"].requests == []  # 6 条各判一次;被测不是裁判自家
    assert job.spent_usd == pytest.approx(3 * 0.01 + 3 * 0.001 + 6 * 0.001)  # 裁判费用进账本
    assert job.judge_total == 6 and job.judge_done == 6
    note = job.sampling
    assert note is not None and [t.key for t in note.judge_targets] == ["judge_a::ja", "judge_b::jb"]
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.FAIL}
    run = storage.runs_for_prompt(prompt.prompt_id, surface="two_model", model=CITES)[0]
    rows = {r["detector_name"]: r for r in storage.observations_for_run(run.run_id)}
    assert rows["brand_mention_judge"]["mentioned"] is True and rows["brand_mention_judge"]["match_kind"] == "judge"
    assert rows["sentiment_judge"]["sentiment"] == "positive" and "brand_mention_fuzzy" in rows  # 三个判定器并排存档

    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=load_evidence_registry(), replay=True,
                       judges=config)
    assert job.state is JobState.DONE, job.error
    assert len(judges["judge_a"].requests) == 6  # 重新生成报告:裁判判定从库里装回,一次都没多问


class SickAdapter(TwoModelAdapter):
    """开跑前预检用:healthcheck 抛账户问题或报不可用;ask 一旦被调就是预检没拦住。"""

    def __init__(self, problem) -> None:
        super().__init__()
        self._problem = problem
        self.asks = 0

    def healthcheck(self) -> bool:
        if isinstance(self._problem, Exception):
            raise self._problem
        return bool(self._problem)

    def ask(self, request: AskRequest) -> SurfaceRun:
        self.asks += 1
        return super().ask(request)


def test_precheck_stops_the_job_before_any_sampling(storage, offline):
    """S11.1(用户定「开跑前自动查余额」):各家先打最小请求——额度问题按充值那句停单、接口不通给 PRECHECK_FAILED,
    一次采样都没发;重算(replay)不预检。"""
    broke = SickAdapter(AdapterAccountError("credit balance too low", error_type=RunErrorType.ACCOUNT_CREDIT,
                                            http_status=402, response_body="{}"))
    job, prompt = _new_job("中小团队用什么 CRM 好?")
    runner.execute_job(job=job, adapters={"two_model": broke}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert job.state is JobState.FAILED and job.error_code == "ACCOUNT_CREDIT" and job.error_params["ai"] == "假 AI"
    assert broke.asks == 0 and storage.runs_for_prompt(prompt.prompt_id, surface="two_model") == []
    assert job.spent_usd == 0.0

    down = SickAdapter(False)
    job2, prompt2 = _new_job("中小团队用什么 CRM 好?")
    runner.execute_job(job=job2, adapters={"two_model": down}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert job2.state is JobState.FAILED and job2.error_code == "PRECHECK_FAILED"
    assert job2.error_params == {"names": "假 AI"} and down.asks == 0

    runner.execute_job(job=job2, adapters={"two_model": down}, dsn=TEST_DSN, evidence=load_evidence_registry(),
                       replay=True)  # 重算不提问,也就不预检
    assert job2.state is JobState.DONE, job2.error


class CountingAdapter(TwoModelAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.asks = 0

    def ask(self, request: AskRequest) -> SurfaceRun:
        self.asks += 1
        return super().ask(request)


def test_answers_are_reused_from_cache_for_the_same_brand_question_and_model(storage, offline):
    """S11.2(C8):同品牌、同题、同型号 12 小时内别的单问过的回答直接复用——一次都不再问、不算本单花费、采样说明写明;
    别的品牌不复用;「修好后再测」(S11.3,`DiagnosisJob.retest`)不复用——题换了新编号,6 条全部重新问。"""
    first = CountingAdapter()
    tag = uuid4().hex[:6]
    job_a, prompt_a = _new_job("中小团队用什么 CRM 好?", tag=tag)
    runner.execute_job(job=job_a, adapters={"two_model": first}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert job_a.state is JobState.DONE and first.asks == 6 and job_a.sampling.n_cached == 0

    second = CountingAdapter()
    job_b, prompt_b = _new_job("中小团队用什么  CRM 好?", tag=tag)  # 空格不一样:规范化后同一道题
    runner.execute_job(job=job_b, adapters={"two_model": second}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert job_b.state is JobState.DONE, job_b.error
    assert second.asks == 0 and job_b.spent_usd == 0.0  # 6 条全部复用,一分钱没花
    note = job_b.sampling
    assert note is not None and (note.n_cached, note.cache_hours, note.fresh, note.n_valid) == (6, 12, False, 6)
    copies = storage.runs_for_prompt(prompt_b.prompt_id, surface="two_model")
    assert len(copies) == 6 and all(r.cost_usd == 0.0 for r in copies) and {r.run_index for r in copies} == {1, 2, 3}
    assert storage.cache_reuse_count(job_b.job_id) == 6

    third = CountingAdapter()
    job_c, _ = _new_job("中小团队用什么 CRM 好?", tag=tag)
    job_c.brand = BrandSpec(name="别家CRM", domains=["other.test"])
    runner.execute_job(job=job_c, adapters={"two_model": third}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert third.asks == 6 and job_c.sampling.n_cached == 0  # 别的品牌:不复用

    fourth = CountingAdapter()
    job_d = job_a.retest()  # 「修好后再测」:同一份口径新开一单,题换新编号,官网名单沿用
    runner.execute_job(job=job_d, adapters={"two_model": fourth}, dsn=TEST_DSN, evidence=load_evidence_registry())
    assert job_d.state is JobState.DONE, job_d.error
    assert fourth.asks == 6 and job_d.sampling.n_cached == 0 and job_d.sampling.fresh is True  # 再测:一律重新提问
    assert job_d.spent_usd == job_a.spent_usd and job_d.parent_job_id == job_a.job_id  # 真花钱,和上一单一样多
    assert len(storage.runs_for_prompt(job_d.prompts[0].prompt_id, surface="two_model")) == 6  # 新编号下 6 条新回答
    assert job_d.domain_list == job_a.domain_list
