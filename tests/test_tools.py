"""开关测量工具、key 只填一次。进程、端口、数据库全换成假的:不起真进程、不联网、不碰真 key。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fatcat import project as projects
from fatcat.hub.backend import Backend, NeedsConfirm
from fatcat.hub.tools import FAILED, RUNNING, STOPPED, MeasureTool
from fatcat.keys import FROM_FILE, FROM_PAGE, KeyStore
from fatcat.modules import Module, ModuleError
from tests.test_hub import FakeBackend, _create, backend, client  # noqa: F401  (client、backend 两个夹具从 conftest 以外借来)  # noqa: F401

NAMES = {"alpha": "ALPHA_API_KEY", "gemini": "GOOGLE_API_KEY"}
SECRET = "sk-test-0123456789abcdef"  # 编的,不是任何一家的 key


# ---------------------------------------------------------------- key 只填一次


def test_keys_come_from_the_page_first_then_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('﻿export ALPHA_API_KEY="from-file-alpha"\nGEMINI_API_KEY=from-file-gemini # 注释\n'
                        "OTHER=ignored\n", encoding="utf-8")
    keys = KeyStore(NAMES, env_file)
    assert keys.where() == {"alpha": FROM_FILE, "gemini": FROM_FILE}
    env = keys.child_env()
    assert env == {"ALPHA_API_KEY": "from-file-alpha",  # 引号、export、开头的 BOM 都去掉了
                   "GOOGLE_API_KEY": "from-file-gemini", "GEMINI_API_KEY": "from-file-gemini"}  # 两个名字一起给
    keys.remember("alpha", f"  {SECRET}  ")
    assert keys.where()["alpha"] == FROM_PAGE and keys.child_env()["ALPHA_API_KEY"] == SECRET
    keys.forget("alpha")
    assert keys.where()["alpha"] == FROM_FILE and keys.child_env()["ALPHA_API_KEY"] == "from-file-alpha"
    assert "OTHER" not in keys.child_env()  # 不认识的变量不往外递


def test_nothing_here_means_nothing_is_passed_on(tmp_path: Path) -> None:
    keys = KeyStore(NAMES, tmp_path / "no-such.env")
    assert keys.where() == {"alpha": None, "gemini": None} and keys.child_env() == {}
    keys.remember("alpha", "   ")  # 空的不动原来的
    assert keys.where()["alpha"] is None


def test_a_key_that_does_not_look_like_one_is_refused_without_echoing_it(tmp_path: Path) -> None:
    keys = KeyStore(NAMES, None)
    for bad in ("two words", "line\nbreak", "带中文的", "x" * 401):
        with pytest.raises(ModuleError) as caught:
            keys.remember("alpha", bad)
        assert bad not in str(caught.value)  # 报错的话里不带填的内容
    with pytest.raises(ModuleError):
        keys.remember("nobody", SECRET)
    keys.remember("alpha", SECRET)
    assert SECRET not in repr(keys) and SECRET not in str(keys.where())


def test_key_page_never_shows_a_value(client: TestClient, backend: FakeBackend) -> None:  # noqa: F811
    home = client.get("/").text
    assert 'type="password"' in home and "ALPHA_API_KEY" in home and "none here" in home
    saved = client.post("/keys", data={"key_alpha": SECRET, "key_beta": ""})
    assert saved.status_code == 303 and backend.typed == {"alpha": SECRET}
    home = client.get("/").text
    assert SECRET not in home and "entered here (memory only)" in home and "Forget" in home
    refused = client.post("/keys", data={"key_nobody": SECRET})
    assert refused.status_code == 400 and SECRET not in refused.text
    assert client.post("/keys/forget", data={"provider": "alpha"}).status_code == 303 and backend.typed == {}
    assert client.post("/keys", data={"key_alpha": SECRET}, headers={"origin": "http://evil.example"}).status_code == 403


# ---------------------------------------------------------------- 开、关测量工具


class FakeProcess:
    def __init__(self, pid: int = 4321, exit_code: int | None = None) -> None:
        self.pid, self.exit_code = pid, exit_code

    def poll(self) -> int | None:
        return self.exit_code


class World:
    """假的电脑:测量工具开没开、端口是谁在听、数据库起没起得来。"""

    def __init__(self) -> None:
        self.up = False
        self.listener: int | None = None
        self.db_problem: str | None = None
        self.process = FakeProcess()
        self.comes_up = True
        self.spawned: list[dict] = []
        self.killed: list[int] = []

    def alive(self, url: str) -> bool:
        return self.up

    def port_owner(self, port: int) -> int | None:
        return self.listener if self.up else None

    def ensure_db(self) -> str | None:
        return self.db_problem

    def spawn(self, extra_env: dict) -> FakeProcess:
        self.spawned.append(extra_env)
        if self.comes_up and self.process.exit_code is None:
            self.up, self.listener = True, 9999  # 壳进程 4321 起了真正干活的 9999
        return self.process

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        if pid in (self.process.pid, self.listener):
            self.up = False


def _tool(tmp_path: Path, world: World, env: dict | None = None) -> MeasureTool:
    module = Module(name="toast", role="measure", path=tmp_path, python=None, url="http://127.0.0.1:8600")
    return MeasureTool(module, tmp_path / ".tools", (lambda: env or {}), alive=world.alive,
                       port_owner=world.port_owner, kill=world.kill, spawn=world.spawn, ensure_db=world.ensure_db,
                       inline=True, boot_seconds=0.2, poll_seconds=0.01)


def test_start_brings_up_the_database_then_the_tool_and_hands_over_the_keys(tmp_path: Path) -> None:
    world = World()
    tool = _tool(tmp_path, world, {"ALPHA_API_KEY": SECRET})
    assert tool.status()["state"] == STOPPED
    tool.start()
    assert tool.status() == {"state": RUNNING, "ours": True, "note": ""}
    assert world.spawned == [{"ALPHA_API_KEY": SECRET}]
    record = tool.record_path.read_text(encoding="utf-8")
    assert '"pid": 4321' in record and '"listener": 9999' in record and SECRET not in record  # 记进程号,不记 key
    tool.start()  # 已经开着:不起第二个
    assert len(world.spawned) == 1


def test_start_reports_why_it_failed(tmp_path: Path) -> None:
    world = World()
    world.db_problem = "本机数据库启动失败(PG_START_FAILED)。测量工具没有启动。"
    tool = _tool(tmp_path, world)
    tool.start()
    assert tool.status()["state"] == FAILED and "PG_START_FAILED" in tool.status()["note"] and world.spawned == []

    world = World()
    world.process = FakeProcess(exit_code=2)  # 一启动就退出
    tool = _tool(tmp_path, world)
    tool.state_dir.mkdir(parents=True, exist_ok=True)
    tool.log_path.write_text("[DB_UNREACHABLE] 连不上本机数据库\n", encoding="utf-8")
    tool.start()
    status = tool.status()
    assert status["state"] == FAILED and "退出码 2" in status["note"] and "DB_UNREACHABLE" in status["note"]

    world = World()
    world.comes_up = False  # 进程在,网页一直不应答
    tool = _tool(tmp_path, world)
    tool.start()
    assert tool.status()["state"] == FAILED and world.killed == [4321]  # 不留一个半死的进程


def test_a_tool_started_elsewhere_is_used_but_never_stopped(tmp_path: Path) -> None:
    world = World()
    world.up, world.listener = True, 7777
    tool = _tool(tmp_path, world)
    assert tool.status() == {"state": RUNNING, "ours": False, "note": ""}
    tool.start()
    assert world.spawned == []  # 已经有一个开着,用它
    with pytest.raises(ModuleError, match="不是从这里启动的"):
        tool.stop()
    assert world.killed == []


def test_stop_checks_it_is_the_same_process_first(tmp_path: Path) -> None:
    world = World()
    tool = _tool(tmp_path, world)
    tool.start()
    again = _tool(tmp_path, world)  # 入口重启了:靠记下的进程号还认得出、还关得掉
    assert again.status() == {"state": RUNNING, "ours": True, "note": ""}
    world.listener = 5555  # 端口换了别的进程在听(原来那个早没了,别人占了这个端口)
    with pytest.raises(ModuleError, match="进程对不上"):
        again.stop()
    assert world.killed == []
    world.listener = 9999
    again.stop()
    assert world.killed == [4321, 9999] and not again.record_path.exists()
    assert again.status()["state"] == STOPPED


def test_stop_only_clears_the_record_when_the_tool_is_already_gone(tmp_path: Path) -> None:
    world = World()
    tool = _tool(tmp_path, world)
    tool.start()
    world.up = False
    tool.stop()
    assert world.killed == [] and not tool.record_path.exists()


def test_database_is_created_on_first_use_and_never_replaced() -> None:
    from fatcat.hub.tools import NO_POSTGRES, db_action

    # 端口上已经有数据库在听:什么都不做(不管这个文件夹下建没建过库,都不另起一个把它顶掉)
    assert db_action(listening=True, installed=True, initialised=False) == "skip"
    assert db_action(listening=True, installed=False, initialised=False) == "skip"
    # 第一次用的电脑:建库;建过的:启动
    assert db_action(listening=False, installed=True, initialised=False) == "init"
    assert db_action(listening=False, installed=True, initialised=True) == "start"
    # 没装 PostgreSQL:说清楚要装什么,不去跑注定失败的脚本
    assert db_action(listening=False, installed=False, initialised=False) == NO_POSTGRES
    assert "postgresql.org" in NO_POSTGRES


class ToolBackend(Backend):
    def __init__(self, tool: MeasureTool, jobs: list[dict]) -> None:
        super().__init__({}, tool=tool)
        self.jobs, self.jobs_error = jobs, None

    def toast_jobs(self) -> list[dict]:
        if self.jobs_error:
            raise ModuleError(self.jobs_error)
        return self.jobs


def test_never_stopped_while_measuring_and_drafts_need_a_second_yes(tmp_path: Path) -> None:
    world = World()
    tool = _tool(tmp_path, world)
    tool.start()
    hub = ToolBackend(tool, [{"job_id": "run1", "state": "RUNNING"}, {"job_id": "old", "state": "DONE"}])
    with pytest.raises(ModuleError, match="正在测量"):
        hub.tool_stop([], force=True)  # 正在花钱:再确认也不关
    hub.jobs = [{"job_id": "old", "state": "DONE"}]
    hub.jobs_error = "数据库没开"
    with pytest.raises(ModuleError, match="读不到"):
        hub.tool_stop([])  # 不知道有没有在测:不关
    hub.jobs_error = None
    with pytest.raises(NeedsConfirm, match="draft1"):
        hub.tool_stop(["draft1"])
    assert world.killed == []
    hub.tool_stop(["draft1"], force=True)
    assert world.killed == [4321, 9999]


def test_measure_step_follows_the_tool(client: TestClient, backend: FakeBackend, tmp_path: Path) -> None:  # noqa: F811
    project_id = _create(client)
    backend.tool = {"state": STOPPED, "ours": False, "note": ""}
    page = client.get(f"/projects/{project_id}").text
    assert "Start the measurement tool" in page and "/measure/start" not in page.replace("/tools/measure/start", "")
    started = client.post("/tools/measure/start", data={"back": project_id})
    assert started.status_code == 303 and started.headers["location"].startswith(f"/projects/{project_id}")
    page = client.get(f"/projects/{project_id}").text
    assert backend.tool_calls == [("start",)] and "Starting the measurement tool" in page
    assert 'http-equiv="refresh"' in page  # 等的时候页面自己刷新
    backend.tool = {"state": FAILED, "ours": False, "note": "本机数据库启动失败"}
    assert "did not start: 本机数据库启动失败" in client.get(f"/projects/{project_id}").text
    backend.tool = {"state": RUNNING, "ours": True, "note": ""}
    page = client.get(f"/projects/{project_id}").text
    assert "Open the measurement tool" in page and "Stop the measurement tool" in page
    backend.tool = {"state": RUNNING, "ours": False, "note": ""}
    page = client.get(f"/projects/{project_id}").text
    assert "Stop the measurement tool" not in page and "not started from here" in page


def test_stopping_with_a_draft_waiting_asks_again_and_then_drops_the_link(
        client: TestClient, backend: FakeBackend, tmp_path: Path) -> None:  # noqa: F811
    project_id = _create(client)
    backend.tool = {"state": RUNNING, "ours": True, "note": ""}
    client.post(f"/projects/{project_id}/measure/start")  # 交过去一单,停在确认页
    backend.stop_error = NeedsConfirm("有 1 单还停在测量工具的确认页")
    asked = client.post("/tools/measure/stop", data={"back": project_id})
    assert asked.status_code == 409 and "Stop it anyway" in asked.text and 'name="force" value="1"' in asked.text
    assert backend.tool_calls[-1] == ("stop", ("abc123def456",), False)
    assert projects.load(tmp_path / "runs", project_id).toast_pending == "abc123def456"  # 没关成:链接还留着
    done = client.post("/tools/measure/stop", data={"back": project_id, "force": "1"})
    assert done.status_code == 303 and backend.tool_calls[-1] == ("stop", ("abc123def456",), True)
    assert projects.load(tmp_path / "runs", project_id).toast_pending is None
    backend.tool = {"state": RUNNING, "ours": True, "note": ""}
    backend.stop_error = ModuleError("测量工具正在测量(run1)")
    refused = client.post("/tools/measure/stop", data={"back": project_id})
    assert refused.status_code == 409 and "正在测量" in refused.text and "Stop it anyway" not in refused.text
