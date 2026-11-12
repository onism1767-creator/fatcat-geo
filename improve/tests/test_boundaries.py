"""模块边界(机械把守):谁能联网、谁能写盘、纯层不碰 I/O 与时钟、不 import 兄弟模块。"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "butter"
FILES = sorted(p for p in PKG.rglob("*.py"))
PURE = [p for p in FILES if p.parent.name in ("extract", "audit", "generate")
        or p.name in ("factbase.py", "render.py", "contracts.py", "util.py", "registry_ids.py")]
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
        if path.name == "fetch.py":
            assert hit == {"httpx"}
        else:
            assert not hit, f"{path.name} 不该联网:{hit}"


def test_no_sibling_modules_and_no_model_sdks():
    for path in FILES:
        roots = imported_roots(path)
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


def test_secrets_only_via_environment():
    """本周不调用任何模型、不需要任何密钥:代码里不该出现读密钥的地方,也不该有像密钥的字面量。"""
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert "API_KEY" not in text and "api_key" not in text and "sk-" not in text, path.name
