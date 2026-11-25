"""统一入口的网页:填一次、按步骤走、出报告。三个项目那一层整个换成假的,不联网、不花钱。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fatcat import project as projects
from fatcat.hub.app import BUNDLE_NAME, EXPORT_NAME, create_app
from fatcat.modules import ModuleError
from fatcat.sources import weblite
from tests.test_report import _butter, _toast


class FakeBackend:
    """记下被叫了什么;该返回什么由各项测试自己设。"""

    def __init__(self) -> None:
        self.jobs: list[dict] = [
            {"job_id": "job1", "state": "DONE", "brand": "Brand", "website": "https://brand.example",
             "created_at": "2026-11-03T19:50:23", "report_versions": 2, "parent": None, "spent_usd": 19.2337,
             "budget_usd": 30.0},
            {"job_id": "job2", "state": "DONE", "brand": "Other", "website": "https://other.example",
             "created_at": "2026-11-04T10:00:00", "report_versions": 1},
            {"job_id": "job3", "state": "RUNNING", "brand": "Brand", "website": "https://www.brand.example",
             "created_at": "2026-11-05T10:00:00", "report_versions": 0},
        ]
        self.toast_url = "http://127.0.0.1:8600"
        self.jobs_error: str | None = None
        self.started: list[str] = []
        self.start_error: str | None = None
        self.export_error: str | None = None
        self.read_calls: list[tuple[str, Path, Path | None]] = []
        self.read_result: tuple[bool, str, str | None] = (True, "", "RUN_DIR")
        self.tool: dict = {"state": "running", "ours": False, "note": ""}
        self.tool_calls: list[tuple] = []
        self.stop_error: Exception | None = None
        self.key_names = {"alpha": "ALPHA_API_KEY", "beta": "BETA_API_KEY"}
        self.typed: dict[str, str] = {}
        self.retests: list[str] = []
        self.retest_error: str | None = None
        self.compare_calls: list[tuple] = []
        self.compare_error: str | None = None

    def toast_retest(self, job_id: str) -> str:
        if self.retest_error:
            raise ModuleError(self.retest_error)
        self.retests.append(job_id)
        return "http://127.0.0.1:8600/jobs/feed00000001"

    def butter_compare(self, before_zip: Path, after_zip: Path, deploy_day, out_json: Path) -> dict:
        if self.compare_error:
            raise ModuleError(self.compare_error)
        self.compare_calls.append((before_zip.read_bytes(), after_zip.read_bytes(), deploy_day))
        out_json.write_text(json.dumps(COMPARE), encoding="utf-8")
        return COMPARE

    def tool_status(self) -> dict:
        return self.tool

    def tool_start(self) -> None:
        self.tool_calls.append(("start",))
        self.tool = {"state": "starting", "ours": True, "note": ""}

    def tool_stop(self, drafts: list[str], *, force: bool = False) -> None:
        self.tool_calls.append(("stop", tuple(drafts), force))
        if self.stop_error and not force:
            raise self.stop_error
        self.tool = {"state": "stopped", "ours": False, "note": ""}

    def key_status(self) -> dict:
        return {name: ("page" if name in self.typed else None) for name in self.key_names}

    def key_remember(self, provider: str, value: str) -> None:
        if provider not in self.key_names:
            raise ModuleError(f"不认识 {provider} 这一家")
        self.typed[provider] = value

    def key_forget(self, provider: str) -> None:
        self.typed.pop(provider, None)

    def toast_jobs(self) -> list[dict]:
        if self.jobs_error:
            raise ModuleError(self.jobs_error)
        return self.jobs

    def toast_view(self, job_id: str) -> dict:
        return _toast()

    def toast_start(self, project) -> str:
        if self.start_error:
            raise ModuleError(self.start_error)
        self.started.append(project.brand)
        return "http://127.0.0.1:8600/jobs/abc123def456/confirm?lang=en"

    def toast_export(self, job_id: str) -> bytes:
        if self.export_error:
            raise ModuleError(self.export_error)
        return b"PK-export-of-" + job_id.encode()

    def check_bundle(self, path: Path) -> dict:
        return weblite.load_bundle(path)

    def butter_read(self, project, out_dir: Path, export_zip: Path | None):
        self.read_calls.append((project.website, out_dir, export_zip))
        return self.read_result

    def butter_load(self, run_dir: str) -> dict:
        assert run_dir == "RUN_DIR"
        return _butter()


def _leg(ai: str, metric: str, pre: list[int], post: list[int], reason: str = "没有这条腿的波动数据",
         label_text: str = "不可比:样本不够 / 该腿不比") -> dict:
    return {"ai": ai, "metric": metric, "label": "not_comparable", "label_text": label_text, "reason": reason,
            "counts": {"pre": pre, "post": post}}


# 改进工具 compare-toast 给出的结果的样子(schema butter.compare-toast/1),只留报告用得上的几项
COMPARE = {
    "schema": "butter.compare-toast/1", "before": {"file": "a.zip", "day": "2026-11-03", "questions": 24},
    "after": {"file": "b.zip", "day": "2026-11-24", "questions": 24}, "deploy_day": "2026-11-05",
    "groups": {"basis": "all_unprompted", "treated": [str(n) for n in range(1, 21)], "control": [], "sitewide": True},
    "noise": "none",
    "legs": [_leg("Alpha API · a-one", "mention", [16, 53], [20, 55]),
             _leg("Alpha API · a-one", "citation", [0, 60], [3, 60]),
             _leg("Beta API · b-two", "mention", [32, 46], [30, 44]),
             _leg("Beta API · b-two", "citation", [0, 60], [0, 60],
                  reason="基线里搜索过的回答只占 24%,这条腿不比被引率")],
    "notes": ["Gamma API · g-three:改之前测了,再测里没有这个型号,比不了。"], "errors": [],
}


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def client(tmp_path: Path, backend: FakeBackend) -> TestClient:
    return TestClient(create_app(tmp_path / "runs", backend, inline_jobs=True), base_url="http://127.0.0.1:8500",
                      follow_redirects=False)


FORM = {"brand": "Brand", "aliases": "Brand Inc", "website": "brand.example", "category": "widgets",
        "locale": "en-US"}


def _create(client: TestClient, **changes: str) -> str:
    answer = client.post("/projects", data={**FORM, **changes})
    assert answer.status_code == 303, answer.text
    return answer.headers["location"].split("/projects/")[1].split("?")[0]


def _bundle_bytes(findings: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fatcat-FC-1/manifest.json", json.dumps(
            {"ruleset_version": "1.0.0", "checked_url": "https://brand.example/", "final_url": "https://brand.example/"}))
        if findings:
            archive.writestr("fatcat-FC-1/findings.json", json.dumps({"findings": [], "informational": []}))
    return buffer.getvalue()


def test_home_shows_the_form_and_details_are_entered_once(client: TestClient, tmp_path: Path) -> None:
    home = client.get("/")
    assert home.status_code == 200 and 'name="brand"' in home.text and "<script" not in home.text
    project_id = _create(client)
    saved = projects.load(tmp_path / "runs", project_id)
    assert (saved.brand, saved.website, saved.category, saved.locale) == ("Brand", "https://brand.example",
                                                                         "widgets", "en-US")
    page = client.get(f"/projects/{project_id}")
    assert page.text.count("<li><h3>") == 6  # 六步都在一页上
    assert "Brand Inc" in page.text and "Nothing to report yet" in page.text
    assert project_id in client.get("/").text


def test_bad_input_is_explained_and_nothing_is_created(client: TestClient, tmp_path: Path) -> None:
    for changes in ({"brand": "   "}, {"website": "not a site"}, {"website": ""}, {"locale": "fr-FR"}):
        answer = client.post("/projects", data={**FORM, **changes})
        assert answer.status_code == 400 and 'role="alert"' in answer.text
    assert projects.list_all(tmp_path / "runs") == []
    assert client.get("/projects/no-such-project").status_code == 404
    assert client.get("/projects/..%2F..%2Fetc").status_code == 404


def test_chinese_pages_on_request(client: TestClient) -> None:
    project_id = _create(client)
    page = client.get(f"/projects/{project_id}?lang=zh").text
    assert "测量与诊断" in page and 'lang="zh"' in page and f"/projects/{project_id}/measure/start?lang=zh" in page


def test_evidence_bundle_is_checked_before_it_is_kept(client: TestClient, tmp_path: Path) -> None:
    project_id = _create(client)
    folder = tmp_path / "runs" / project_id
    bad = client.post(f"/projects/{project_id}/quick-check", files={"bundle": ("x.zip", b"not a zip")})
    assert bad.status_code == 400 and not (folder / BUNDLE_NAME).exists() and not list(folder.glob("*.incoming"))
    half = client.post(f"/projects/{project_id}/quick-check", files={"bundle": ("x.zip", _bundle_bytes(False))})
    assert half.status_code == 400 and "findings.json" in half.text
    good = client.post(f"/projects/{project_id}/quick-check", files={"bundle": ("x.zip", _bundle_bytes())})
    assert good.status_code == 303 and (folder / BUNDLE_NAME).is_file()
    assert projects.load(tmp_path / "runs", project_id).quick_check == BUNDLE_NAME
    assert "Evidence bundle added." in client.get(f"/projects/{project_id}").text


def test_measure_step_hands_the_user_to_the_measurement_tool(client: TestClient, backend: FakeBackend) -> None:
    project_id = _create(client)
    answer = client.post(f"/projects/{project_id}/measure/start")
    assert answer.status_code == 303 and answer.headers["location"] == "http://127.0.0.1:8600/jobs/abc123def456/confirm?lang=en"
    assert backend.started == ["Brand"]
    page = client.get(f"/projects/{project_id}").text  # 浏览器离开本页了:回来时有一条路回到那一单
    assert 'href="http://127.0.0.1:8600/jobs/abc123def456/confirm"' in page and "waiting in the measurement tool" in page
    backend.jobs.append({"job_id": "abc123def456", "state": "DONE", "brand": "Brand",
                         "website": "https://brand.example", "created_at": "2026-11-24T16:00:00", "report_versions": 1})
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "abc123def456"}).status_code == 303
    assert "waiting in the measurement tool" not in client.get(f"/projects/{project_id}").text  # 做完挂上了,提示收走
    backend.start_error = "测量工具没有打开"
    failed = client.post(f"/projects/{project_id}/measure/start")
    assert failed.status_code == 502 and "测量工具没有打开" in failed.text


def test_only_finished_runs_for_this_site_can_be_attached(client: TestClient, backend: FakeBackend,
                                                         tmp_path: Path) -> None:
    project_id = _create(client)
    page = client.get(f"/projects/{project_id}").text
    assert 'value="job1"' in page and 'value="job2"' not in page and 'value="job3"' not in page
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job2"}).status_code == 400
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job3"}).status_code == 400
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "nope"}).status_code == 400
    assert projects.load(tmp_path / "runs", project_id).toast_job is None
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job1"}).status_code == 303
    assert projects.load(tmp_path / "runs", project_id).toast_job == "job1"
    assert "Using run job1." in client.get(f"/projects/{project_id}").text


def test_page_still_opens_when_the_measurement_data_cannot_be_read(client: TestClient, backend: FakeBackend) -> None:
    project_id = _create(client)
    backend.jobs_error = "Toast 的数据库没有开"
    page = client.get(f"/projects/{project_id}")
    assert page.status_code == 200 and "Toast 的数据库没有开" in page.text and page.text.count("<li><h3>") == 6


def test_improve_step_reads_the_site_and_passes_the_measurement_export(client: TestClient, backend: FakeBackend,
                                                                        tmp_path: Path) -> None:
    project_id = _create(client)
    folder = tmp_path / "runs" / project_id
    client.post(f"/projects/{project_id}/improve/start")
    website, out_dir, export = backend.read_calls[-1]
    assert (website, out_dir, export) == ("https://brand.example", folder / "improve", None)  # 没有测量结果就不递
    saved = projects.load(tmp_path / "runs", project_id)
    assert (saved.improve_state, saved.improve_run) == ("done", "RUN_DIR")

    client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job1"})
    client.post(f"/projects/{project_id}/improve/start")
    assert backend.read_calls[-1][2] == folder / EXPORT_NAME
    assert (folder / EXPORT_NAME).read_bytes() == b"PK-export-of-job1"

    backend.export_error = "测量工具没开着"  # 拿不到下载包不拦着读网站
    client.post(f"/projects/{project_id}/improve/start")
    assert backend.read_calls[-1][2] is None and projects.load(tmp_path / "runs", project_id).improve_state == "done"
    page = client.get(f"/projects/{project_id}").text  # 但要说明少了这一份,不悄悄略过
    assert "without the measurement results" in page
    backend.export_error = None
    client.post(f"/projects/{project_id}/improve/start")
    assert "without the measurement results" not in client.get(f"/projects/{project_id}").text


def test_failed_site_reading_keeps_the_reason(client: TestClient, backend: FakeBackend, tmp_path: Path) -> None:
    project_id = _create(client)
    backend.read_result = (False, "遇到验证码,停了", None)
    client.post(f"/projects/{project_id}/improve/start")
    saved = projects.load(tmp_path / "runs", project_id)
    assert (saved.improve_state, saved.improve_note) == ("failed", "遇到验证码,停了")
    page = client.get(f"/projects/{project_id}").text
    assert "遇到验证码,停了" in page and "Failed" in page and "Read the site" in page


def test_report_is_built_from_whatever_steps_are_done(client: TestClient, tmp_path: Path) -> None:
    project_id = _create(client)
    empty = client.get(f"/projects/{project_id}/report")
    assert empty.status_code == 400 and "出不了报告" in empty.text
    client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job1"})
    client.post(f"/projects/{project_id}/improve/start")
    client.post(f"/projects/{project_id}/quick-check", files={"bundle": ("x.zip", _bundle_bytes())})
    english = client.get(f"/projects/{project_id}/report")
    assert english.status_code == 200 and english.text.count("<h2 ") == 12 and "At a glance" in english.text
    assert "sampling-line-24-questions" in english.text and "add-organization-jsonld" in english.text
    chinese = client.get(f"/projects/{project_id}/report?lang=zh")
    assert "一页结论" in chinese.text
    folder = tmp_path / "runs" / project_id
    assert (folder / "report-en.html").read_text(encoding="utf-8") == english.text
    assert (folder / "report-zh.html").is_file()
    assert "Open report (English)" in client.get(f"/projects/{project_id}").text


def test_only_this_computer_and_this_tools_own_pages_may_act(client: TestClient, backend: FakeBackend) -> None:
    project_id = _create(client)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    foreign = client.post(f"/projects/{project_id}/measure/start", headers={"origin": "http://evil.example"})
    assert foreign.status_code == 403 and backend.started == []
    own = client.post(f"/projects/{project_id}/measure/start", headers={"origin": "http://127.0.0.1:8500"})
    assert own.status_code == 303


def test_a_reading_cut_off_by_a_restart_is_not_left_running(tmp_path: Path, backend: FakeBackend) -> None:
    root = tmp_path / "runs"
    project = projects.create(root, brand="Brand", aliases="", website="brand.example", category="w", locale="en-US")
    project.improve_state = "running"
    projects.save(root, project)
    create_app(root, backend)
    assert projects.load(root, project.id).improve_state == "failed"


def test_two_actions_at_once_do_not_overwrite_each_other(client: TestClient, backend: FakeBackend,
                                                         tmp_path: Path) -> None:
    """真跑时丢过一次:一个操作读了项目、还没写回去,另一个操作写了别的项,前一个再整份写回去就把它盖掉了。"""
    root = tmp_path / "runs"
    project_id = _create(client)
    original = backend.toast_jobs

    def jobs_while_something_else_writes() -> list[dict]:
        projects.update(root, project_id, quick_check=BUNDLE_NAME)  # 「挂上测量结果」处理到一半时,证据包那边写了一笔
        return original()

    backend.toast_jobs = jobs_while_something_else_writes  # type: ignore[method-assign]
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job1"}).status_code == 303
    saved = projects.load(root, project_id)
    assert (saved.toast_job, saved.quick_check) == ("job1", BUNDLE_NAME)

    def read_while_something_else_writes(project, out_dir, export_zip):
        projects.update(root, project_id, quick_check=None)  # 读网站的那几十秒里,别处又改了一笔
        return True, "", "RUN_DIR"

    backend.butter_read = read_while_something_else_writes  # type: ignore[method-assign]
    client.post(f"/projects/{project_id}/improve/start")
    saved = projects.load(root, project_id)
    assert (saved.improve_state, saved.toast_job, saved.quick_check) == ("done", "job1", None)


def test_update_changes_only_the_named_fields(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    project = projects.create(root, brand="Brand", aliases="", website="brand.example", category="w", locale="en-US")
    changed = projects.update(root, project.id, toast_job="job1")
    assert (changed.toast_job, changed.brand, changed.created_at) == ("job1", "Brand", project.created_at)
    with pytest.raises(ValueError):
        projects.update(root, project.id, id="other")
    with pytest.raises(ValueError):
        projects.update(root, project.id, nonsense=1)
