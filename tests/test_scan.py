"""查 key 泄露的那个脚本:该认的认得出,假的不误报,查到的内容不印出来。

这里的「key」都是当场拼出来的假串,文件里没有任何一个完整的、像 key 的字面量(不然脚本会查到这个测试自己)。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("scan_secrets", Path(__file__).parent.parent / "scripts" / "scan_secrets.py")
scan = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scan)  # type: ignore[union-attr]

FAKE_ANTHROPIC = "sk-" + "ant-" + "Zq7" * 12
FAKE_GOOGLE = "AI" + "za" + "Bc9" * 11 + "xy"
HOME = "C:" + "\\Users\\" + "realperson" + "\\Desktop\\file.zip"


def test_keys_and_personal_paths_are_recognised_and_masked() -> None:
    hits = scan._find(f'ANTHROPIC = "{FAKE_ANTHROPIC}"', {})
    assert [kind for kind, _ in hits] == ["Anthropic key"]
    assert FAKE_ANTHROPIC not in hits[0][1] and "个字符" in hits[0][1]  # 只给头尾几个字符和长度
    assert [kind for kind, _ in scan._find(f"key={FAKE_GOOGLE}", {})] == ["Google key"]
    assert [kind for kind, _ in scan._find("OPENAI_" + "API_KEY=" + "m" * 40, {})] == ["key assignment"]
    (kind, shown), = scan._find(f"saved to {HOME}", {})
    assert kind == "personal path" and shown.startswith("real") and "realperson" not in shown
    assert [kind for kind, _ in scan._find("-----BEGIN RSA " + "PRIVATE KEY-----", {})] == ["private key block"]


def test_obvious_fakes_and_placeholders_are_not_reported() -> None:
    for line in ('SECRET = "sk-test-0123456789abcdef"', "OPENAI_" + "API_KEY=<your key here, placeholder>",
                 "put it in C:\\Users\\you\\project", "ANTHROPIC_API_KEY=", "see /home/runner/work"):
        assert scan._find(line, {}) == [], line


def test_named_words_are_found_whatever_the_case() -> None:
    extra = {"点名的词「acme」": scan.re.compile("acme", scan.re.I)}
    assert scan._find("the example site is ACME Kettles", extra) == [("点名的词「acme」", "ACME")]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def test_a_key_removed_later_is_still_found_in_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "config.txt").write_text(f"token: {FAKE_ANTHROPIC}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add config")
    (repo / "config.txt").write_text("token: (moved to .env)\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "remove the key")

    assert scan.scan_tree(repo, {}) == []  # 现在的文件里没有了
    found = scan.scan_history(repo, {})
    assert len(found) == 1 and found[0][1:3] == ("config.txt", "Anthropic key") and found[0][0].startswith("提交 ")

    assert scan.main([str(repo)]) == 1
    printed = capsys.readouterr().out
    assert "Anthropic key:1 处" in printed and FAKE_ANTHROPIC not in printed  # 印位置和类别,不印内容
    assert scan.main([str(repo), "--no-history"]) == 0
