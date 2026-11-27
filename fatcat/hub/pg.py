"""免安装包自带的那个数据库:第一次用的时候建好,每次启动测量工具之前保证它开着,用完能停。

免安装包里带着 PostgreSQL 的程序文件(不用用户另装)。数据放在包自己的文件夹里,用一个自己的端口,
和这台电脑上别的 PostgreSQL 互不相干:不抢它的端口,不碰它的数据。

只管这一个数据目录:开的是它,停的也只是它(看数据目录里的 postmaster.pid,别的库一概不动)。
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_EXE = ".exe" if os.name == "nt" else ""
START_SECONDS = 120


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@dataclass(frozen=True)
class LocalPostgres:
    """bin_dir:PostgreSQL 的程序文件夹。data_dir:这个包自己的数据目录。url:测量工具连它用的地址。"""

    bin_dir: Path
    data_dir: Path
    url: str

    @property
    def port(self) -> int:
        return urlsplit(self.url).port or 5432

    @property
    def user(self) -> str:
        return urlsplit(self.url).username or "geo"

    @property
    def database(self) -> str:
        return urlsplit(self.url).path.lstrip("/") or "geo_toast"

    @property
    def initialised(self) -> bool:
        return (self.data_dir / "PG_VERSION").is_file()

    @property
    def ours_running(self) -> bool:
        """这个数据目录的数据库是不是开着(它开着的时候目录里有 postmaster.pid,端口也在听)。"""
        return (self.data_dir / "postmaster.pid").is_file() and port_open(self.port)

    def _run(self, exe: str, *args: str, log: Path, timeout: int) -> int:
        # 输出进文件、不接管道:数据库进程会继承输出句柄,接了管道这里要等到数据库停掉才读得完
        with open(log, "ab") as out:
            return subprocess.run([str(self.bin_dir / (exe + _EXE)), *args], stdin=subprocess.DEVNULL, stdout=out,
                                  stderr=subprocess.STDOUT, timeout=timeout, creationflags=_NO_WINDOW).returncode

    def ensure(self, log_dir: Path) -> str | None:
        """保证数据库开着、要用的库建好了。成了返回 None;不成返回一句给用户看的话。"""
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "database.log"
        if not (self.bin_dir / ("pg_ctl" + _EXE)).is_file():
            return f"包里没有找到数据库的程序({self.bin_dir})。这个文件夹可能没有解压完整,请重新解压一遍。测量工具没有启动。"
        if port_open(self.port) and not (self.data_dir / "postmaster.pid").is_file():
            return (f"端口 {self.port} 已经被别的程序占着,它不是这个包自己的数据库。关掉占着它的程序再试。测量工具没有启动。")
        try:
            if not self.initialised:
                self.data_dir.parent.mkdir(parents=True, exist_ok=True)
                code = self._run("initdb", "-D", str(self.data_dir), "-U", self.user, "-E", "UTF8", "--locale=C",
                                 "-A", "trust", log=log, timeout=300)
                if code != 0 or not self.initialised:
                    return f"第一次建数据库没有成功(退出码 {code})。原因记在 {log}。测量工具没有启动。"
            if not port_open(self.port):
                code = self._run("pg_ctl", "-D", str(self.data_dir), "-o", f"-p {self.port} -c listen_addresses=127.0.0.1",
                                 "-l", str(log_dir / "database-server.log"), "-w", "-t", str(START_SECONDS), "start",
                                 log=log, timeout=START_SECONDS + 30)
                if code != 0 or not port_open(self.port):
                    return (f"数据库没有开起来(退出码 {code})。原因记在 {log_dir / 'database-server.log'}。"
                            "如果里面说缺 VCRUNTIME140.dll 或 MSVCP140.dll,请装微软的 Visual C++ 运行库后再试。测量工具没有启动。")
            # 要用的那个库:有了就什么都不做(createdb 会说「已经存在」,这不算错)
            self._run("createdb", "-h", "127.0.0.1", "-p", str(self.port), "-U", self.user, self.database, log=log, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"开数据库的时候出错了:{type(exc).__name__}。原因记在 {log}。测量工具没有启动。"
        return None

    def stop(self, log_dir: Path) -> str:
        """停掉这个数据目录的数据库。返回一句结果。别的数据库不动。"""
        if not self.initialised or not (self.data_dir / "postmaster.pid").is_file():
            return "The database was not running. / 这个包自己的数据库本来就没有开着。"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            code = self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "-w", "-t", "60", "stop",
                             log=log_dir / "database.log", timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Could not stop the database ({type(exc).__name__}). / 停数据库的时候出错了。"
        if code == 0:
            return "Database stopped. / 数据库停了。"
        return (f"The database did not stop (exit code {code}); see {log_dir / 'database.log'}. / "
                f"数据库没有停下来(退出码 {code}),原因记在 {log_dir / 'database.log'}。")
