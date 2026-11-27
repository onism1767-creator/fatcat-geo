"""开、关测量工具,连它要的本机数据库一起。用户只用开统一入口这一个东西。

测量工具要比入口活得长:入口重启的时候它可能正在测量(花着钱),不能跟着停。所以:
- 启动时隔一层:入口起一个马上就退出的中间进程,由它起测量工具。这样测量工具不在入口的进程树里,
  有人「连子进程一起结束」入口时带不走它(2026-11-24 实测:直接起的话,入口一被这样结束,测量工具跟着没了)。
  它也单开一个进程组、不挂在入口的终端窗口上,入口按 Ctrl+C 或关窗口不会带走它。
- 启动后把进程号记在文件里。入口重启后还认得出它、还关得掉它。
- 关之前先认人:看它的端口现在是谁在听,和当初记下的是不是同一个进程。不是就当成别处开的,不去关。

数据库只管开,不管关:别的窗口可能也在用,而且它存着测量结果。
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from fatcat.hub.pg import LocalPostgres
from fatcat.modules import Module, ModuleError

STOPPED, STARTING, RUNNING, FAILED = "stopped", "starting", "running", "failed"
BOOT_SECONDS = 90  # 进程起来以后,等它的网页应答最多这么久
DB_SECONDS = 240  # 数据库脚本自己最多等 180 秒
_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# 数据库脚本(测量工具自带的 scripts/pgdev.ps1)的退出码,照它文件头上写的
_DB_EXIT = {
    10: "等了 180 秒,本机数据库还是连不上(PG_START_TIMEOUT)",
    11: "本机数据库的端口在听但不应答(PG_ZOMBIE);要在测量工具的文件夹里运行 scripts\\pgdev.ps1 restart",
    13: "本机数据库启动失败(PG_START_FAILED)",
}


DB_PORT = 5433  # 测量工具的本机数据库固定用这个端口(它的 engine/storage.py 里写着)
NO_POSTGRES = ("这台电脑上没有找到 PostgreSQL。测量的结果存在它里面,要先装:到 postgresql.org 下载 Windows 版,"
               "一路用默认选项装完,再回来点一次。测量工具没有启动。")


def db_action(*, listening: bool, installed: bool, initialised: bool) -> str:
    """启动测量工具之前,数据库这边该做什么。返回 skip / init / start,或者一句给用户看的话(做不了的原因)。

    - 端口上已经有数据库在听:什么都不做。别处(比如原来的文件夹)开着的那一个照用,绝不另起一个空库把它顶掉。
    - 没在听、这个文件夹下也没有建过库:第一次用,建一个(init:建库目录、启动、建好要用的库)。
    - 没在听、建过:启动(start)。
    """
    if listening:
        return "skip"
    if not installed:
        return NO_POSTGRES
    return "start" if initialised else "init"


def bundled_database(module: Module) -> LocalPostgres | None:
    """这份安装是不是自带数据库的(modules.toml 里三项都填了)。是就返回它,不是返回 None。"""
    if module.db_url and module.pg_bin and module.pg_data:
        return LocalPostgres(bin_dir=module.pg_bin, data_dir=module.pg_data, url=module.db_url)
    return None


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _postgres_installed() -> bool:
    """和测量工具的脚本找法一样:C:\\Program Files\\PostgreSQL\\<版本>\\bin 里有没有 pg_ctl。"""
    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
    return any(root.glob("*/bin/pg_ctl.exe"))


def _port_owner(port: int) -> int | None:
    """这个端口现在是哪个进程在听。查不出来(没人在听,或者这台电脑上没有查的办法)返回 None。"""
    if not _WINDOWS:
        return None
    command = (f"(Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue | "
               "Select-Object -First 1).OwningProcess")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True,
                              timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return int(text) if text.isdigit() else None


def _kill_tree(pid: int) -> None:
    """结束一个进程和它起的子进程(虚拟环境的 python.exe 是个壳,真正干活的是它的子进程)。"""
    if _WINDOWS:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# 中间进程跑的代码:起测量工具,印出它的进程号,自己马上退出。key 在环境变量里,原样传下去,不经过命令行和文件。
# 0x01000000 = 脱离入口所在的作业对象(入口要是被放在「关掉时连带结束」的作业里,不脱离就还是会被带走);不让脱离就退一步不带它。
_LAUNCHER = """
import json, subprocess, sys
spec = json.loads(sys.stdin.read())
with open(spec["log"], "ab") as log:
    for extra in (0x01000000, 0):
        try:
            child = subprocess.Popen(spec["command"], cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, creationflags=spec["flags"] | extra)
            break
        except OSError:
            if not extra:
                raise
print(child.pid)
"""


def _exit_code(pid: int) -> int | None:
    """这个进程还在就返回 None;不在了返回它的退出码(查不到是多少就返回 -1)。"""
    if not _WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError:
            return -1
        return None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return -1
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return -1
        return None if code.value == 259 else int(code.value)  # 259 = 还在跑
    finally:
        kernel32.CloseHandle(handle)


class _Detached:
    """一个不归入口管生死的进程:只记得它的进程号,问得出它还在不在。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return _exit_code(self.pid)


def nobody_listening(url: str, seconds: float = 0.5) -> bool:
    """本机这个端口是不是没人在听。只对 127.0.0.1 的地址下结论,别的地址一律返回 False(网络慢不等于没开)。

    为什么要先敲一下门:Windows 上连一个没人听的本机端口,要等两秒左右才回「拒绝」。页面每打开一次都要问
    一遍测量工具在不在,它没开的时候每一页就都慢两秒。有人在听的本机端口几毫秒内就接上,所以半秒够判断。
    """
    parts = urlsplit(url)
    if parts.hostname != "127.0.0.1":
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(seconds)
        return probe.connect_ex(("127.0.0.1", parts.port or 80)) != 0


def _alive(url: str) -> bool:
    if nobody_listening(url):
        return False
    try:
        return httpx.get(url + "/", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


class MeasureTool:
    """module:测量工具在哪。state_dir:记进程号和启动日志的文件夹(在本项目的 runs/ 里)。
    extra_env:启动那一刻取一次要递过去的环境变量(key)。后面几个参数只给测试换成假的。"""

    def __init__(self, module: Module, state_dir: Path, extra_env: Callable[[], Mapping[str, str]] | None = None, *,
                 alive: Callable[[str], bool] = _alive, port_owner: Callable[[int], int | None] = _port_owner,
                 kill: Callable[[int], None] = _kill_tree, spawn: Callable[..., Any] | None = None,
                 ensure_db: Callable[[], str | None] | None = None, inline: bool = False,
                 boot_seconds: float = BOOT_SECONDS, poll_seconds: float = 0.5) -> None:
        self.module = module
        self.url = module.url
        self.port = urlsplit(module.url).port or 80
        self.state_dir = Path(state_dir)
        self.record_path = self.state_dir / "measure-tool.json"
        self.log_path = self.state_dir / "measure-tool.log"
        self._extra_env = extra_env or (lambda: {})
        self._is_alive, self._port_owner, self._kill = alive, port_owner, kill
        self._spawn = spawn or self._spawn_real
        self._ensure_db = ensure_db or self._ensure_db_real
        self._inline, self._boot_seconds, self._poll = inline, boot_seconds, poll_seconds
        self._lock = threading.Lock()
        self._phase, self._note = STOPPED, ""

    # ------------------------------------------------------------ 现在什么样

    def alive(self) -> bool:
        return self._is_alive(self.url)

    def status(self) -> dict[str, Any]:
        """state:stopped / starting / running / failed。ours:是不是从这里启动的(只有这样的这里才关得了)。"""
        with self._lock:
            phase, note = self._phase, self._note
        if phase == STARTING:
            return {"state": STARTING, "ours": True, "note": ""}
        if self.alive():
            return {"state": RUNNING, "ours": self._record() is not None, "note": ""}
        if phase == FAILED:
            return {"state": FAILED, "ours": False, "note": note}
        return {"state": STOPPED, "ours": False, "note": ""}

    def _record(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None

    # ------------------------------------------------------------ 开

    def start(self) -> None:
        """开始启动;已经在启动就不重复。启动过程在后台,页面自己刷新看结果。"""
        with self._lock:
            if self._phase == STARTING:
                return
            self._phase, self._note = STARTING, ""
        if self._inline:
            self._boot()
        else:
            threading.Thread(target=self._boot, daemon=True).start()

    def _finish(self, phase: str, note: str = "") -> None:
        with self._lock:
            self._phase, self._note = phase, note

    def _boot(self) -> None:
        try:
            if self.alive():  # 已经开着(别处开的,或者上一次从这里开的):不再起第二个
                return self._finish(STOPPED)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            problem = self._ensure_db()
            if problem:
                return self._finish(FAILED, problem)
            process = self._spawn(dict(self._extra_env()))
            deadline = time.monotonic() + self._boot_seconds
            while time.monotonic() < deadline:
                if self.alive():
                    record = {"pid": int(process.pid), "listener": self._port_owner(self.port),
                              "started_at": datetime.now().astimezone().isoformat(timespec="seconds"), "url": self.url}
                    self.record_path.write_text(json.dumps(record), encoding="utf-8")
                    return self._finish(STOPPED)
                code = process.poll()
                if code is not None:
                    said = f"(退出码 {code})" if code >= 0 else ""
                    return self._finish(FAILED, f"测量工具一启动就退出了{said}。{self._log_tail()}")
                time.sleep(self._poll)
            self._kill(int(process.pid))
            self._finish(FAILED, f"等了 {self._boot_seconds:.0f} 秒,测量工具的网页还是没有应答,已经把它停掉。{self._log_tail()}")
        except (OSError, subprocess.SubprocessError, ModuleError) as exc:
            self._finish(FAILED, f"测量工具没启动起来:{exc}")

    def _log_tail(self) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-4:]
        except OSError:
            return ""
        return ("它最后说的是:" + " / ".join(line.strip() for line in lines if line.strip())) if lines else ""

    def _ensure_db_real(self) -> str | None:
        """测量工具的结果存在本机的 PostgreSQL 里,要先开着。用它自带的脚本开;第一次用的电脑顺手建好。"""
        bundled = bundled_database(self.module)
        if bundled is not None:  # 免安装包:数据库的程序和数据都在包里,用它自己的端口
            return bundled.ensure(self.state_dir)
        script = self.module.require_path() / "scripts" / "pgdev.ps1"
        if not _WINDOWS or not script.is_file():
            return None  # 没有这个脚本的装法:数据库由用户自己管,连不上时测量工具会自己说
        action = db_action(listening=_port_open(DB_PORT), installed=_postgres_installed(),
                           initialised=(self.module.path / ".pgdata" / "PG_VERSION").is_file())
        if action == "skip":
            return None
        if action not in ("init", "start"):
            return action
        with open(self.state_dir / "database.log", "ab") as log:
            # 输出进文件、不接管道:数据库进程会继承输出句柄,接了管道这里要等到数据库停掉才读得完
            done = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), action],
                cwd=self.module.path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=DB_SECONDS, creationflags=_NO_WINDOW)
        if done.returncode == 0:
            return None
        return _DB_EXIT.get(done.returncode, f"本机数据库没开起来(脚本退出码 {done.returncode})") + "。测量工具没有启动。"

    def _spawn_real(self, extra_env: dict[str, str]) -> Any:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", **extra_env)
        if self.module.db_url:  # 自带数据库的装法:告诉测量工具连哪一个库
            env["GEO_DATABASE_URL"] = self.module.db_url
        command = [str(self.module.require_python()), "-B", "-m", "report"]
        cwd = str(self.module.require_path())
        if not _WINDOWS:  # 自成一个会话就够了:入口退出、终端关掉都带不走它
            with open(self.log_path, "ab") as log:
                return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        spec = {"command": command, "cwd": cwd, "log": str(self.log_path), "flags": _NO_WINDOW | _NEW_GROUP}
        done = subprocess.run([sys.executable, "-c", _LAUNCHER], input=json.dumps(spec), env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60, creationflags=_NO_WINDOW)
        pid = done.stdout.strip()
        if done.returncode != 0 or not pid.isdigit():
            raise ModuleError("起测量工具的那一步没成:" + (done.stderr.strip().splitlines() or ["没有说原因"])[-1])
        return _Detached(int(pid))

    # ------------------------------------------------------------ 关

    def stop(self) -> None:
        """关掉从这里启动的测量工具。不是从这里启动的、或者认不出是不是同一个进程的,不关,说明原因。

        「它是不是正在测量、有没有还没确认的草稿」不在这里判断,由调用的一方先查好(backend.tool_stop)。
        """
        record = self._record()
        if record is None:
            raise ModuleError("这个测量工具不是从这里启动的,这里不去关它。它是在哪开的,就在哪关。")
        owner = self._port_owner(self.port)
        if owner is None:
            if self.alive():
                raise ModuleError("查不出现在是哪个进程在提供测量工具的网页,这里不去关它。")
            self.record_path.unlink(missing_ok=True)  # 已经不在了:只收掉记录
            return self._finish(STOPPED)
        if owner != record.get("listener"):
            raise ModuleError("现在开着的测量工具不是这里启动的那一个(进程对不上),这里不去关它。")
        self._kill(int(record["pid"]))
        if owner != record["pid"]:
            self._kill(owner)
        deadline = time.monotonic() + 15
        while self.alive() and time.monotonic() < deadline:
            time.sleep(self._poll)
        if self.alive():
            raise ModuleError("已经让测量工具结束,但它的网页还在应答。稍等再看,或者在任务管理器里结束它。")
        self.record_path.unlink(missing_ok=True)
        self._finish(STOPPED)
