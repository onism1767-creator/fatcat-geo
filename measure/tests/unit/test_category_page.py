"""品类核对页(S11.4;产品文档 D24、架构书设计书 C7):提交后、出题前自动核对;只有「不是一类」才拦一页三选一,
选完才出题;一致 / 没核对出来直接去确认页;没题可测时一分钱都不花;连不上库就不核对。
页面流用假仪器与桩核对器,零网络;默认核对器的账本走真库(storage 夹具)。"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from adapters.base import AdapterAccountError
from engine.category_check import CategoryCheck, CategoryVerdict
from engine.models import BrandSpec, RunErrorType
from engine.storage import StorageUnavailable
from report.app import create_app
from report.jobs import DiagnosisJob
from report.web import category_checker
from tests.unit.test_category_check import HOMEPAGE, JUDGES, ScriptedAdapter
from tests.unit.test_report_page import REGISTRY, UI_FORBIDDEN, DummyAdapter, visible_text

DIFFERENT = CategoryCheck(verdict=CategoryVerdict.DIFFERENT, site_phrase="极光CRM 帮销售团队管理客户、跟进商机。",
                          suggested="客户关系管理软件", reason="官网说的是客户管理软件")
SAME = CategoryCheck(verdict=CategoryVerdict.SAME, site_phrase="", suggested="", reason="一样")
FORM = {"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com", "category": "宠物食品",
        "questions": "问题一?", "generate": ""}


def _client(check, adapter=None) -> tuple[TestClient, list[DiagnosisJob]]:
    seen: list[DiagnosisJob] = []

    def checker(job: DiagnosisJob):
        seen.append(job)
        return check

    app = create_app(call_stats=lambda: [], adapters={"s1": adapter or DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None, category_check=checker)
    return TestClient(app), seen


def _job_of(client: TestClient, url: str) -> DiagnosisJob:
    return client.app.state.ctx.registry.get(url.split("/jobs/")[1].split("/")[0].split("?")[0])


RELATED = CategoryCheck(verdict=CategoryVerdict.RELATED, site_phrase="", suggested="客户管理软件", reason="宽窄不同")


@pytest.mark.parametrize("check", [None, SAME, RELATED])
def test_submit_goes_straight_to_confirm_unless_the_check_says_different(check):
    client, seen = _client(check)
    resp = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/confirm?lang=zh")
    (job,) = seen
    assert job.website == "https://aurora-crm.com" and job.category == "宠物食品" and job.locale == "zh-CN"
    assert _job_of(client, resp.headers["location"]) is job and job.pending_draft is None
    assert job.category == "宠物食品" and [c.text for c in job.candidates] == ["问题一?"]
    assert "宠物食品" in client.get(resp.headers["location"]).text


def test_nothing_to_test_fails_before_the_check_runs():
    client, seen = _client(DIFFERENT)
    resp = client.post("/jobs?lang=zh", data={**FORM, "questions": "", "generate": ""})
    assert resp.status_code == 200 and "请至少手填一个问题" in resp.text and seen == []


def test_different_category_blocks_with_a_plain_page_in_both_languages():
    client, _ = _client(DIFFERENT)
    resp = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/category?lang=zh")
    job = _job_of(client, resp.headers["location"])
    assert job.pending_draft is not None and job.pending_draft.questions == "问题一?" and job.candidates == []
    zh = client.get(resp.headers["location"]).text
    text = visible_text(zh)
    assert "你填的品类和官网说的好像不是一回事" in text and "宠物食品" in text
    assert "极光CRM 帮销售团队管理客户、跟进商机。" in text and "客户关系管理软件" in text
    assert zh.count('type="radio" name="choice"') == 3 and "checked" not in zh  # 三选一,不替用户预选
    assert not any(token in text for token in UI_FORBIDDEN)
    en = visible_text(client.get(f"/jobs/{job.job_id}/category?lang=en").text)
    assert "You entered" in en and "Judging by the homepage" in en and "Keep mine: 宠物食品" in en
    # 品类没定:确认页先回品类页;确认表单也不收
    assert client.get(f"/jobs/{job.job_id}/confirm?lang=zh", follow_redirects=False).headers["location"].endswith(
        "/category?lang=zh")
    posted = client.post(f"/jobs/{job.job_id}/confirm?lang=zh", data={"targets_selected": ["s1::m1"]},
                         follow_redirects=False)
    assert posted.headers["location"].endswith("/category?lang=zh")


def _blocked(client: TestClient) -> DiagnosisJob:
    resp = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    return _job_of(client, resp.headers["location"])


def test_three_choices_settle_the_category_then_draft_and_go_to_confirm():
    client, _ = _client(DIFFERENT)
    for choice, custom, expected in (("site", "", "客户关系管理软件"), ("mine", "", "宠物食品"),
                                     ("custom", "  宠物用品电商 ", "宠物用品电商")):
        job = _blocked(client)
        resp = client.post(f"/jobs/{job.job_id}/category?lang=zh", data={"choice": choice, "custom_category": custom},
                           follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].endswith("/confirm?lang=zh"), choice
        assert job.category == expected and job.pending_draft is None, choice
        assert [c.text for c in job.candidates] == ["问题一?"]  # 手填的题等到品类定了才登记
        assert f"品类:{expected}" in client.get(resp.headers["location"]).text
        # 定过了:品类页不再出现
        again = client.get(f"/jobs/{job.job_id}/category?lang=zh", follow_redirects=False)
        assert again.headers["location"].endswith("/confirm?lang=zh")


def test_empty_or_forged_choice_stays_on_the_page_with_a_plain_sentence():
    client, _ = _client(DIFFERENT)
    job = _blocked(client)
    for data in ({"choice": "custom", "custom_category": "   "}, {"choice": ""}, {"choice": "hack"}):
        resp = client.post(f"/jobs/{job.job_id}/category?lang=zh", data=data, follow_redirects=False)
        assert "error=FORM_CATEGORY_EMPTY" in resp.headers["location"] and job.pending_draft is not None
    page = visible_text(client.get(resp.headers["location"]).text)
    assert "请选一个品类" in page and not any(token in page for token in UI_FORBIDDEN)
    assert client.post("/jobs/nope/category?lang=zh", data={"choice": "mine"}).status_code == 404


def test_drafting_waits_until_the_category_is_settled(storage):
    """勾了「让系统帮我出题」且被拦:选品类之前一次都不问 AI;选完才出题,而且按定下来的品类出。"""
    from tests.unit.conftest import TEST_DSN

    class RecordingBrokeAdapter(DummyAdapter):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def ask(self, request):
            self.prompts.append(request.prompt_text)
            raise AdapterAccountError("credit", error_type=RunErrorType.ACCOUNT_CREDIT, http_status=402)

    adapter = RecordingBrokeAdapter()
    app = create_app(call_stats=lambda: [], adapters={"s1": adapter}, dsn=TEST_DSN, evidence=REGISTRY,
                     job_runner=lambda **kw: None, calibration=None, category_check=lambda job: DIFFERENT)
    client = TestClient(app)
    resp = client.post("/jobs?lang=zh", data={**FORM, "questions": "", "generate": "1"}, follow_redirects=False)
    assert resp.headers["location"].endswith("/category?lang=zh") and adapter.prompts == []
    job = _job_of(client, resp.headers["location"])
    assert job.pending_draft is not None and job.pending_draft.generate is True
    after = client.post(f"/jobs/{job.job_id}/category?lang=zh", data={"choice": "site"}, follow_redirects=False)
    assert "error=ACCOUNT_CREDIT" in after.headers["location"]  # 出题真的跑了(撞上额度问题回首页,S11.1)
    (prompt,) = adapter.prompts
    assert "客户关系管理软件" in prompt and "宠物食品" not in prompt


def test_default_checker_skips_without_touching_the_database_when_no_ai_or_db_is_down(monkeypatch):
    """免费的先判:没有能用的 AI 就不连库、不抓首页;连不上库 → 不抓、不问(钱没处记就不花)。都只记日志返回 None。"""
    import report.web as web

    def boom(dsn):
        raise StorageUnavailable("10 秒内连不上数据库")

    monkeypatch.setattr(web, "Storage", boom)
    job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com", category="C",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    monkeypatch.setattr(web, "Storage", lambda dsn: pytest.fail("没有能用的 AI 却去连库"))
    assert category_checker({"s1": DummyAdapter()}, JUDGES, "postgresql://down")(job) is None
    monkeypatch.setattr(web, "Storage", boom)
    untouched = ScriptedAdapter([{"verdict": "different"}])
    assert category_checker({"s_main": untouched}, JUDGES, "postgresql://down")(job) is None
    assert untouched.requests == []


@respx.mock
def test_default_checker_records_the_call_in_the_job_ledger(storage):
    """默认核对器:真抓首页(respx)、真走账本(真库):这一单的辅助花费里有这笔。"""
    from tests.unit.conftest import TEST_DSN

    respx.get("https://aurora-crm.com/").mock(
        return_value=httpx.Response(200, text=HOMEPAGE, headers={"content-type": "text/html; charset=utf-8"}))
    adapter = ScriptedAdapter([{"verdict": "same", "reason": "一样"}])
    check = category_checker({"s_main": adapter}, JUDGES, TEST_DSN)
    job = DiagnosisJob(brand=BrandSpec(name="极光CRM", domains=["aurora-crm.com"]), website="https://aurora-crm.com/",
                       category="CRM", locale="zh-CN", candidates=[], competitors_suggested=[])
    out = check(job)
    assert out is not None and out.verdict is CategoryVerdict.SAME
    assert storage.aux_spent_usd(job.job_id) == pytest.approx(0.0002)


# ── 还没确认的任务(审核 2026-10-27 红 2)────────────────────────────────────────────────


def test_draft_job_page_and_history_link_lead_to_where_the_job_is():
    """还没确认的任务没有档位参数:任务页不再 500,转到确认页;品类没定再转品类页;首页历史表直接链到确认页。"""
    client, _ = _client(None)
    resp = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    job = _job_of(client, resp.headers["location"])
    status = client.get(f"/jobs/{job.job_id}?lang=zh", follow_redirects=False)
    assert status.status_code == 303 and status.headers["location"].endswith(f"/jobs/{job.job_id}/confirm?lang=zh")
    followed = client.get(f"/jobs/{job.job_id}?lang=zh")
    assert followed.status_code == 200 and followed.url.path.endswith("/confirm")
    home = client.get("/?lang=zh").text
    assert f"/jobs/{job.job_id}/confirm?lang=zh" in home and "继续确认" in home

    client, _ = _client(DIFFERENT)
    job = _blocked(client)
    followed = client.get(f"/jobs/{job.job_id}?lang=zh")
    assert followed.status_code == 200 and followed.url.path.endswith("/category")


def test_a_draft_whose_drafting_failed_is_withdrawn_instead_of_lingering(monkeypatch):
    """品类页之后出题失败:这单从历史表撤掉,不留一条没题、点开就出错的空任务(花掉的钱已按编号记账)。"""
    from engine.ai_task import TaskFailed

    class FakeStorage:
        def __init__(self, dsn: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def insert_aux_call(self, **kwargs) -> None:
            pass

    def failing_draft(*args, **kwargs):
        raise TaskFailed("三轮都不合格")

    monkeypatch.setattr("report.routes.Storage", FakeStorage)
    monkeypatch.setattr("report.routes.draft_prompt_candidates", failing_draft)
    client, _ = _client(DIFFERENT, adapter=ScriptedAdapter([]))
    resp = client.post("/jobs?lang=zh", data={**FORM, "questions": "", "generate": "1"}, follow_redirects=False)
    job = _job_of(client, resp.headers["location"])
    assert client.app.state.ctx.registry.get(job.job_id) is job
    decided = client.post(f"/jobs/{job.job_id}/category?lang=zh", data={"choice": "mine"}, follow_redirects=False)
    assert decided.status_code == 303 and "error=DRAFTING_FAILED" in decided.headers["location"]
    assert client.app.state.ctx.registry.get(job.job_id) is None
    assert job.job_id not in client.get("/?lang=zh").text
