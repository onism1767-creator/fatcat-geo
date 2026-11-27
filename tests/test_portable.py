"""免安装包:自带数据库那条路怎么判断;打包脚本里几条容易错的规矩。不起真数据库、不联网。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fatcat.hub.pg import LocalPostgres
from fatcat.hub.tools import bundled_database
from fatcat.modules import load_modules

_SPEC = importlib.util.spec_from_file_location("build_portable",
                                               Path(__file__).parent.parent / "scripts" / "build_portable.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)  # type: ignore[union-attr]


def test_a_package_with_its_own_database_is_recognised(tmp_path: Path) -> None:
    (tmp_path / "measure").mkdir()
    (tmp_path / "modules.toml").write_text(build.MODULES_TOML, encoding="utf-8")
    toast = load_modules(tmp_path / "modules.toml")["toast"]
    database = bundled_database(toast)
    assert database is not None and (database.port, database.user, database.database) == (54330, "geo", "geo_toast")
    assert database.bin_dir == (tmp_path.parent / "pgsql" / "bin").resolve()   # 程序在包里的 pgsql/
    assert database.data_dir == (tmp_path.parent / "data" / "pg").resolve()    # 数据在包里的 data/pg/
    assert toast.python == (tmp_path / "measure" / ".." / ".." / "python" / "python.exe")
    assert bundled_database(load_modules(tmp_path / "modules.toml")["butter"]) is None

    plain = tmp_path / "plain.toml"  # 照说明自己装的那种:三项都不填,走原来的路
    plain.write_text('[modules.toast]\npath = "measure"\nurl = "http://127.0.0.1:8600"\n', encoding="utf-8")
    assert bundled_database(load_modules(plain)["toast"]) is None


def test_the_packaged_database_keeps_off_the_usual_ports() -> None:
    assert build.DB_PORT not in (5432, 5433)  # 这台电脑上另装着 PostgreSQL 时互不相干
    assert f":{build.DB_PORT}/geo_toast" in build.MODULES_TOML


def test_missing_programs_are_reported_not_guessed(tmp_path: Path) -> None:
    database = LocalPostgres(bin_dir=tmp_path / "pgsql" / "bin", data_dir=tmp_path / "data" / "pg",
                             url="postgresql://geo@localhost:54399/geo_toast")
    problem = database.ensure(tmp_path / "logs")
    assert problem is not None and "没有找到数据库的程序" in problem and "重新解压" in problem
    assert not (tmp_path / "data" / "pg").exists()  # 没有程序就什么都不建
    assert database.stop(tmp_path / "logs") == "这个包自己的数据库本来就没有开着。"
    assert database.initialised is False and database.ours_running is False


def test_search_path_of_the_packaged_python_covers_all_three_parts() -> None:
    lines = build.pth_lines()
    assert lines[0].endswith(".zip") and lines[-1] == "import site"
    assert {r"..\lib", r"..\app", r"..\app\measure", r"..\app\improve"} <= set(lines)


def test_only_the_headless_browser_is_left_out_of_the_dependencies() -> None:
    keep = build.keep_requirement
    assert keep("fastapi==0.165.0") and keep("psycopg-binary==3.2.3 ; sys_platform == 'win32'") and keep("lxml==6.1.3")
    assert not keep("playwright==1.49.0") and not keep("greenlet==3.1.1") and not keep("pyee==12.0.0")
    assert not keep("# comment") and not keep("") and not keep("-e ./measure")


def test_launchers_are_plain_ascii_and_clear_the_python_environment() -> None:
    for text in (build.START_BAT, build.STOP_BAT):
        text.encode("ascii")  # 不是纯英文的 .bat 在别人的电脑上会乱码
        assert 'set "PYTHONPATH="' in text and 'set "PYTHONHOME="' in text and 'cd /d "%~dp0"' in text
    assert "--open" in build.START_BAT and '--env-file ".env"' in build.START_BAT
    assert "-m fatcat stop" in build.STOP_BAT
    for readme in (build.README_EN, build.README_ZH):
        readme.format(version="0", port=1, pyver="3", pgver="18")  # 占位都填得上
