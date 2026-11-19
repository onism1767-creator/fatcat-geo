"""模块边界(机械把守):谁能联网、谁能写盘、纯层不碰 I/O 与时钟、不 import 兄弟模块。"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "butter"
FILES = sorted(p for p in PKG.rglob("*.py"))
# registry/ 里只有 load.py 读文件(外壳),其余是纯层
# config/ 里只有 load.py 读文件,core.py 是纯层;compare/、check/ 全是纯层(随机数自己实现,不 import random)
# cards/ 里只有 service.py 是外壳(经 store.py 落盘),其余是纯层;proposals/ 全是纯层(只出文本,由调用方落盘)
PURE = [p for p in FILES if p.parent.name in ("extract", "audit", "generate", "contracts", "compare", "check", "draft",
                                              "proposals", "questions")
        or p.name in ("factbase.py", "render.py", "util.py")
        or (p.parent.name == "cards" and p.name != "service.py")
        or (p.parent.name in ("registry", "config") and p.name not in ("load.py", "__init__.py"))
        or (p.parent.name == "registry" and p.name == "__init__.py")
        or (p.parent.name == "llm" and p.name in ("wire.py", "pricing.py"))
        or (p.parent.name == "measure" and p.name == "plan.py")]
NETWORK = {"httpx", "requests", "urllib3", "aiohttp", "socket", "http", "ftplib", "smtplib", "websockets", "playwright"}
SIBLINGS = {"aivis", "engine", "diagnosis", "report", "adapters", "crawler", "trafilatura", "psycopg", "psycopg2",
            "sqlalchemy"}
MODEL_SDKS = {"anthropic", "openai", "google", "perplexity", "litellm", "langchain"}
WRITE_CALLS = {"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "rename", "replace", "touch", "rmtree", "makedirs"}
CLOCK_CALLS = {"now", "today", "utcnow", "time", "monotonic"}


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
            if node.module == "urllib":
                roots |= {f"urllib.{a.name}" for a in node.names}
            elif node.module.startswith("urllib."):
                roots.add(node.module)
    return roots


def calls(path: Path) -> list[tuple[str, ast.Call]]:
    out = []
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            out.append((name, node))
    return out


def test_only_fetch_touches_the_network():
    assert len(FILES) > 20
    for path in FILES:
        roots = imported_roots(path)
        hit = roots & NETWORK | {r for r in roots if r == "urllib.request"}
        if path.name == "fetch.py" or (path.parent.name == "llm" and path.name == "client.py"):
            assert hit == {"httpx"}  # 抓站,和调模型:全仓库只有这两处联网
        elif path.parent.name == "web" and path.name == "serve.py":
            assert hit == {"socket"}  # 只为了在本机 127.0.0.1 上找一个空端口
        else:
            assert not hit, f"{path.name} 不该联网:{hit}"


def test_no_sibling_modules_and_no_model_sdks():
    for path in FILES:
        roots = imported_roots(path)
        if path.parent.name == "bridges" and path.name == "potato_read.py":
            # 读取桥:由 Potato 自己的解释器执行的脚本,Butter 不 import 它(ADR-B03)。它只许碰 aivis.storage。
            assert roots & SIBLINGS == {"aivis"} and "butter" not in roots
            assert "aivis.storage" in path.read_text(encoding="utf-8")
            continue
        assert not roots & SIBLINGS, f"{path.name} import 了兄弟模块:{roots & SIBLINGS}"
        assert not roots & MODEL_SDKS, f"{path.name} import 了模型 SDK:{roots & MODEL_SDKS}"


def test_only_store_writes_files():
    for path in FILES:
        writes = []
        for name, node in calls(path):
            if name in WRITE_CALLS and isinstance(node.func, ast.Attribute):
                target = ast.unparse(node.func.value)
                if name == "replace" and not target.endswith(("path", "Path", "dir")):
                    continue  # str.replace
                writes.append(name)
            if name == "open":
                mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and any(ch in str(mode.value) for ch in "wax+"):
                    writes.append(f"open({mode.value})")
        if path.name == "store.py":
            assert writes, "store.py 应当是写盘的地方"
        else:
            assert not writes, f"{path.name} 不该写盘:{writes}"


def test_pure_layers_do_no_io_and_read_no_clock():
    assert len(PURE) >= 15
    for path in PURE:
        roots = imported_roots(path)
        assert not roots & {"pathlib", "os", "shutil", "sqlite3", "subprocess", "time", "random", "secrets"}, (path.name, roots)
        for name, node in calls(path):
            assert name != "open", f"{path.name} 调用了 open()"
            if name in CLOCK_CALLS and isinstance(node.func, ast.Attribute):
                owner = ast.unparse(node.func.value)
                assert owner not in ("datetime", "date", "time", "datetime.datetime", "datetime.date"), \
                    f"{path.name} 读了系统时钟:{owner}.{name}()"


def test_cards_and_proposals_are_pure_and_only_store_touches_sqlite():
    """卡片状态机、投影、提案都在纯层名单里;卡片库的外壳只有 service.py;SQLite 只有 store.py 碰。"""
    pure = {(p.parent.name, p.name) for p in PURE}
    for name in ("machine.py", "judge.py", "project.py", "methods.py", "build.py", "index.py", "text.py"):
        assert ("cards", name) in pure, name
    assert ("cards", "service.py") not in pure
    for name in ("render.py", "notes.py"):
        assert ("proposals", name) in pure, name
    for path in FILES:
        if path.name != "store.py":
            assert "sqlite3" not in imported_roots(path), f"{path.name} 不该直接碰 SQLite"


def test_secrets_only_via_environment():
    """密钥只从环境变量来:只有 llm/keys.py 知道变量名;任何文件里都不该有像密钥的字面量,也不该有别的读密钥的地方。"""
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text and "api_key" not in text, path.name
        if not (path.parent.name == "llm" and path.name == "keys.py"):
            assert "API_KEY" not in text, path.name
            if path.parent.name != "llm" and path.name not in ("cli.py",):  # cli 只把整份环境交给 llm,自己不认变量名
                assert "os.environ" not in text and "getenv" not in text, f"{path.name} 不该读环境变量"
