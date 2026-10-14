"""执行器端到端(v0.4.19):同一台仪器的两个型号各测各的、各报各的,样本互不串。

走真实执行器 + 真实调度器 + 真实存储(测试库)+ 真实诊断,只把两处联网动作换成桩:
官网抓取审计与被引 URL 重抓(12.3:单测不许碰真实网络)。适配器是按型号给不同回答的假仪器。
"""

from datetime import UTC, datetime
from urllib.parse import urlsplit

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
from report.jobs import RUNS_PER_PROMPT, DiagnosisJob, JobState

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


def _new_job(text: str) -> tuple[DiagnosisJob, Prompt]:
    prompt = Prompt(text=text, intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER, locale="zh-CN")
    job = DiagnosisJob(brand=BRAND, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(prompts=[prompt], competitors=[],
               targets=[Target(surface="two_model", model=CITES), Target(surface="two_model", model=SILENT)],
               budget_usd=None)
    return job, prompt


def test_two_models_of_one_surface_are_sampled_and_diagnosed_apart(storage, offline):
    job, prompt = _new_job("中小团队用什么 CRM 好?")
    adapters = {"two_model": TwoModelAdapter()}
    evidence = load_evidence_registry()

    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=evidence)

    assert job.state is JobState.DONE, job.error
    assert job.total_runs == 2 * RUNS_PER_PROMPT == job.completed_runs  # 1 道题 × 3 次 × 2 个型号
    runs = storage.runs_for_prompt(prompt.prompt_id, surface="two_model")
    per_model = {m: [r for r in runs if r.model == m] for m in (CITES, SILENT)}
    assert {m: sorted(r.run_index for r in rs) for m, rs in per_model.items()} == {CITES: [1, 2, 3], SILENT: [1, 2, 3]}
    # 各报各的:两个型号各有一个诊断块、各有一句话,结论不同(3/3 → 良好;0/3 → 较弱)
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.FAIL}
    assert {(v.surface, v.model) for v in job.surface_verdicts} == {("two_model", CITES), ("two_model", SILENT)}
    assert job.spent_usd == pytest.approx(3 * 0.01 + 3 * 0.001)

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
    assert job.error_code == "job_error.account_credit"  # 充值那句,不是换 key 那句
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
    job.freeze(prompts=[prompt], competitors=[], targets=[Target(surface="redirect_ai", model="r1")], budget_usd=None)
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
