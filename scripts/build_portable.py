"""打 Windows 免安装包:解压、双击就能用,不用装 Python,不用装数据库。

    python scripts/build_portable.py [--pg-home <PostgreSQL 的安装文件夹>]

在发布用的仓库里跑(要有 measure/ 和 improve/ 两个文件夹),用这个仓库自己环境里的 Python。要联网、要装着 uv。

包里有什么:
    python/     python.org 官方的免安装版 Python(版本和打包时用的一样)。能运行的只有这个官方签名的 python.exe,
                我们的代码只是它读的文件,所以不会被 Windows 的「智能应用控制」拦,也不用代码签名
    lib/        全部依赖(照仓库根的 uv.lock;不带开发用的,不带无头浏览器)
    app/        入口、测量、改进三部分的代码
    pgsql/      PostgreSQL 的程序文件(从这台电脑上装的那一份复制;只复制程序,不碰任何数据)
    data/       用户自己的东西:项目、报告、数据库。第一次启动时才建
    FatCat-GEO.bat、Stop-FatCat-GEO.bat、两份说明

和照说明自己装的版本比,少一样:不带无头浏览器(Chromium,一百多 MB)。少数要运行脚本才显示内容的网页,
免安装包会照实记成「看不到」。

产物:dist/FatCat-GEO-<版本>-windows-x64.zip
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "FatCat-GEO"
BUILD = ROOT / "build" / "portable"
STAGE = BUILD / NAME
PYVER = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
EMBED_URL = f"https://www.python.org/ftp/python/{PYVER}/python-{PYVER}-embed-amd64.zip"
DB_PORT = 54330  # 包自己的数据库用的端口。特意不用 5432 / 5433:这台电脑上要是另装着 PostgreSQL,互不相干
LEFT_OUT = ("playwright", "greenlet", "pyee")  # 无头浏览器和只有它用的两个包
PG_EXES = ("postgres.exe", "initdb.exe", "pg_ctl.exe", "createdb.exe")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests", ".gitignore", ".gitattributes", ".env*")

PTH = ["python{v}.zip", ".", r"..\lib", r"..\app", r"..\app\measure", r"..\app\improve", "import site"]

MODULES_TOML = f"""# 免安装包里各部分的位置,相对本文件所在的文件夹(app/)。
# 数据库是包里自带的:程序在 pgsql/,数据在 data/pg/,用自己的端口 {DB_PORT}。

[modules.toast]
role = "measure: ask the AIs, judge the answers, diagnose"
path = "measure"
python = "../../python/python.exe"
url = "http://127.0.0.1:8600"
db_url = "postgresql://geo@localhost:{DB_PORT}/geo_toast"
pg_bin = "../pgsql/bin"
pg_data = "../data/pg"

[modules.butter]
role = "improve: read the site, check content, prepare changes, compare before and after"
path = "improve"
python = "../../python/python.exe"
"""

_ENV = 'set "PYTHONHOME="\r\nset "PYTHONPATH="\r\nset "PYTHONNOUSERSITE=1"\r\nset "PYTHONUTF8=1"\r\n'
START_BAT = (
    '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n' + _ENV + "title FatCat GEO\r\n"
    "echo FatCat GEO is starting. Your browser will open in a moment.\r\n"
    "echo Keep this window open while you use it. Closing it stops the entry page only:\r\n"
    "echo a measurement that is already running keeps going.\r\n"
    "echo To stop everything, run Stop-FatCat-GEO.bat.\r\n"
    "echo.\r\n"
    '"python\\python.exe" -B -m fatcat serve --port 8500 --runs "data\\runs" --env-file ".env" --open\r\n'
    "echo.\r\npause\r\n"
)
STOP_BAT = (
    '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n' + _ENV + "title FatCat GEO - stop\r\n"
    '"python\\python.exe" -B -m fatcat stop --runs "data\\runs"\r\n'
    "echo.\r\npause\r\n"
)

README_EN = """FatCat GEO Full Toolkit {version} - portable package for Windows

HOW TO USE
1. Unzip the whole folder first. Do not run it from inside the .zip.
   A short path with plain letters works best, for example C:\\FatCat-GEO.
2. Double-click FatCat-GEO.bat. A black window opens and your browser shows the entry page.
3. Follow the six steps on the page. The first time you start the measurement tool, the package
   creates its own database; that takes about half a minute.
4. Closing the black window stops the entry page only. To stop everything (the measurement tool
   and the database), double-click Stop-FatCat-GEO.bat.

NOTHING TO INSTALL
Python and the database (PostgreSQL) are inside this folder. Nothing is written outside it,
except small caches some libraries keep in your user profile.

YOUR DATA
Everything you create is under the data folder: your projects and reports in data\\runs, the
measurement results in data\\pg. To move to another computer or a newer package, copy the data folder.

COST AND KEYS
- Only two steps cost money: measuring (step 3) and measuring again (step 5). They use your own
  AI keys and you pay the AI providers directly. Step 3 shows an estimate before it starts.
  Step 5 starts at once; its button states roughly how much.
- Enter your keys at the bottom of the entry page. They stay in memory only. To keep them, put
  them in a file named .env in this folder, using the variable names shown on the page.
- Keys are never written to logs, reports or result files. Nothing is sent to a FatCat server.

WHAT IS DIFFERENT FROM A FULL INSTALL
This package has no headless browser. A few cited web pages that only show their content after
running scripts are recorded as "could not be read".

IF SOMETHING GOES WRONG
- The browser did not open: open http://127.0.0.1:8500/ yourself.
- The database does not start and the page mentions VCRUNTIME140.dll or MSVCP140.dll: install the
  Microsoft Visual C++ Redistributable (x64) and try again.
- Port 8500, 8600 or {port} is used by another program: close that program and try again.

WHAT IS INSIDE (third-party software)
- Python {pyver}, from python.org (PSF License).
- PostgreSQL {pgver}, program files as built by EDB (PostgreSQL License; see pgsql\\server_license.txt
  and pgsql\\commandlinetools_3rd_party_licenses.txt).
- Microsoft Visual C++ runtime files next to the programs that need them.
- The Python packages under lib, each with its own license in its *.dist-info folder.
FatCat GEO itself is MIT licensed; see LICENSE. Source: https://github.com/onism1767-creator/fatcat-geo
"""

README_ZH = """FatCat GEO 完整工具 {version} —— Windows 免安装包

怎么用
1. 先把整个文件夹解压出来,不要在压缩包里直接运行。
   路径短一点、只用英文字母最稳,比如 C:\\FatCat-GEO。
2. 双击 FatCat-GEO.bat。会出现一个黑色窗口,浏览器自动打开入口页面。
3. 照页面上的六步走。第一次启动测量工具时,包会给自己建一个数据库,大约半分钟。
4. 关掉黑色窗口只是停了入口页面。要全部停掉(测量工具和数据库),双击 Stop-FatCat-GEO.bat。

什么都不用装
Python 和数据库(PostgreSQL)都在这个文件夹里。除了个别依赖库在你的用户目录下留一点缓存,不往别处写东西。

你的数据
你做出来的东西都在 data 文件夹里:项目和报告在 data\\runs,测量结果在 data\\pg。
换电脑或者换新版本的包,把 data 文件夹拷过去就行。

花钱和 key
- 只有两步会花钱:第 3 步的测量、第 5 步的再测。用的是你自己的 AI key,钱直接付给各家 AI。
  第 3 步开始前会先给你看预估;第 5 步一点就开始,按钮上写着大约多少。
- key 在入口页面最下面填,只放在内存里。想长期留着,在这个文件夹里建一个叫 .env 的文件,变量名页面上有。
- key 不写进日志、报告和任何结果文件。什么都不发到 FatCat 的服务器。

和完整安装的差别
这个包不带无头浏览器。少数要运行脚本才显示内容的被引网页,会照实记成「看不到」。

出问题的时候
- 浏览器没有自己打开:手动打开 http://127.0.0.1:8500/ 。
- 数据库起不来,页面上提到 VCRUNTIME140.dll 或 MSVCP140.dll:装微软的 Visual C++ 运行库(x64)再试。
- 8500、8600 或 {port} 端口被别的程序占着:关掉那个程序再试。

包里带的第三方软件
- Python {pyver},来自 python.org(PSF 许可证)。
- PostgreSQL {pgver},EDB 编译的程序文件(PostgreSQL 许可证;见 pgsql\\server_license.txt 和
  pgsql\\commandlinetools_3rd_party_licenses.txt)。
- 微软 Visual C++ 运行库文件,放在用到它的程序旁边。
- lib 下面的 Python 包,各自的许可证在各自的 *.dist-info 文件夹里。
FatCat GEO 自己是 MIT 许可证,见 LICENSE。源代码:https://github.com/onism1767-creator/fatcat-geo
"""


def run(*command: str, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def pth_lines() -> list[str]:
    """免安装版 Python 的搜索路径(它只认这张表,不看环境变量,也不看当前文件夹)。"""
    return [line.format(v=f"{sys.version_info.major}{sys.version_info.minor}") for line in PTH]


def keep_requirement(line: str) -> bool:
    """依赖清单里的一行要不要装进包。注释、空行、不带进包的那几个包不要。"""
    name = re.split(r"[=<>!~;\s\[]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")
    return bool(name) and not line.lstrip().startswith(("#", "-")) and name not in LEFT_OUT


def find_postgres(given: str | None) -> Path:
    if given:
        home = Path(given)
    else:
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        found = sorted((p for p in root.glob("*") if (p / "bin" / "pg_ctl.exe").is_file()),
                       key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)] or [0])
        if not found:
            raise SystemExit("这台电脑上没有找到 PostgreSQL。装一个,或者用 --pg-home 指给我它在哪。")
        home = found[-1]
    if not (home / "bin" / "pg_ctl.exe").is_file():
        raise SystemExit(f"{home} 里没有 bin\\pg_ctl.exe,不像 PostgreSQL 的安装文件夹")
    return home


def fetch_python() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    cache = BUILD / f"python-{PYVER}-embed-amd64.zip"
    if not cache.exists():
        print(f"下载官方免安装版 Python:{EMBED_URL}", flush=True)
        urllib.request.urlretrieve(EMBED_URL, cache)  # noqa: S310 (python.org 官方地址,写死的)
    if not zipfile.is_zipfile(cache):
        cache.unlink()
        raise SystemExit("下载下来的不是一个 zip,已经删掉,请再跑一次")
    return cache


def stage_python() -> None:
    target = STAGE / "python"
    with zipfile.ZipFile(fetch_python()) as archive:
        archive.extractall(target)
    pth = next(target.glob("python*._pth"))
    pth.write_text("\n".join(pth_lines()) + "\n", encoding="utf-8")


def stage_dependencies() -> None:
    exported = BUILD / "requirements-all.txt"
    run("uv", "export", "--quiet", "--locked", "--all-packages", "--no-dev", "--no-emit-workspace", "--no-hashes",
        "--format", "requirements-txt", "-o", str(exported), cwd=ROOT)
    wanted = [line for line in exported.read_text(encoding="utf-8").splitlines() if keep_requirement(line)]
    requirements = BUILD / "requirements.txt"
    requirements.write_text("\n".join(wanted) + "\n", encoding="utf-8")
    # 清单已经是锁文件里解好的全集,所以不再让它自己去找依赖(不然会把去掉的那几个又拉回来)
    run("uv", "pip", "install", "--quiet", "--no-deps", "--python", sys.executable, "--target", str(STAGE / "lib"),
        "-r", str(requirements))
    shutil.rmtree(STAGE / "lib" / "bin", ignore_errors=True)  # 里面的 .exe 写死了打包这台电脑的路径,带过去也不能用
    print(f"   依赖 {len(wanted)} 个", flush=True)


def stage_app() -> None:
    app = STAGE / "app"
    for name in ("measure", "improve"):
        if not (ROOT / name).is_dir():
            raise SystemExit(f"这里没有 {name}/ 文件夹。免安装包要在发布用的仓库里打(三部分代码都在一起的那个)。")
    shutil.copytree(ROOT / "fatcat", app / "fatcat", ignore=SKIP)
    shutil.copytree(ROOT / "catalog", app / "catalog", ignore=SKIP)
    shutil.copytree(ROOT / "measure", app / "measure",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "scripts", ".env*", ".git*", "uv.lock"))
    shutil.copytree(ROOT / "improve", app / "improve", ignore=SKIP)
    (app / "modules.toml").write_text(MODULES_TOML, encoding="utf-8")


def stage_postgres(home: Path) -> str:
    target = STAGE / "pgsql"
    (target / "bin").mkdir(parents=True)
    for item in (home / "bin").iterdir():  # 全部 .dll,加上要用的四个程序;别的工具(备份、命令行客户端等)不带
        if item.suffix.lower() == ".dll" or item.name.lower() in PG_EXES:
            shutil.copy2(item, target / "bin" / item.name)
    shutil.copytree(home / "lib", target / "lib",
                    ignore=shutil.ignore_patterns("*.lib", "*.a", "*.pdb", "pgxs", "pkgconfig", "cmake"))
    shutil.copytree(home / "share", target / "share", ignore=shutil.ignore_patterns("doc", "man", "locale"))
    for name in ("server_license.txt", "commandlinetools_3rd_party_licenses.txt"):
        if (home / name).is_file():
            shutil.copy2(home / name, target / name)
    # 微软的 C++ 运行库:PostgreSQL 的程序要它。干净的电脑上不一定有,所以放一份在程序旁边
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
        source = next((p for p in (STAGE / "python" / name, system32 / name) if p.is_file()), None)
        if source is not None:
            shutil.copy2(source, target / "bin" / name)
    shown = subprocess.run([str(target / "bin" / "postgres.exe"), "--version"], capture_output=True, text=True, check=True)
    return shown.stdout.strip().rsplit(" ", 1)[-1]


def stage_launchers(pg_version: str) -> None:
    (STAGE / "FatCat-GEO.bat").write_bytes(START_BAT.encode("ascii"))
    (STAGE / "Stop-FatCat-GEO.bat").write_bytes(STOP_BAT.encode("ascii"))
    words = {"version": version(), "port": DB_PORT, "pyver": PYVER, "pgver": pg_version}
    (STAGE / "README-English.txt").write_text(README_EN.format(**words), encoding="utf-8-sig", newline="\r\n")
    (STAGE / "README-简体中文.txt").write_text(README_ZH.format(**words), encoding="utf-8-sig", newline="\r\n")
    for name in ("LICENSE", "CHANGELOG.md"):
        shutil.copy2(ROOT / name, STAGE / name)


def precompile() -> None:
    """先把 .pyc 编好。入口每开一个页面都要另起几个 Python 进程,没有编好的要现编,慢好几秒。
    用「不核对」的 .pyc:解压以后文件时间会变,按时间核对的 .pyc 会全部作废。"""
    run(str(STAGE / "python" / "python.exe"), "-m", "compileall", "-q", "--invalidation-mode", "unchecked-hash",
        str(STAGE / "lib"), str(STAGE / "app"))


def pack() -> Path:
    out = ROOT / "dist" / f"{NAME}-{version()}-windows-x64.zip"
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                archive.write(path, Path(NAME) / path.relative_to(STAGE))
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="打 Windows 免安装包")
    parser.add_argument("--pg-home", help="PostgreSQL 的安装文件夹;不给就用这台电脑上装的最新的那个")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("免安装包只能在 Windows 上打")
    home = find_postgres(args.pg_home)
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    print(f"== 官方免安装版 Python {PYVER}", flush=True)
    stage_python()
    print("== 依赖(照锁文件)", flush=True)
    stage_dependencies()
    print("== 三部分的代码", flush=True)
    stage_app()
    print(f"== PostgreSQL 的程序文件(从 {home} 复制,不带数据)", flush=True)
    pg_version = stage_postgres(home)
    stage_launchers(pg_version)
    print("== 预先编好 .pyc", flush=True)
    precompile()
    print("== 压成 zip", flush=True)
    out = pack()
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    files = sum(1 for p in STAGE.rglob("*") if p.is_file())
    print(f"打好了:{out}\n  {out.stat().st_size / 1024 / 1024:.0f} MB,{files} 个文件,PostgreSQL {pg_version}\n  SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
