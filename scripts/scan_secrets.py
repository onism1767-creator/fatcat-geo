"""查一个 git 仓库里有没有不该公开的东西:现在的文件和全部提交历史。只读。

    python scripts/scan_secrets.py [仓库文件夹 ...] [--no-history] [--names 名字,名字]

查三类:
- 像 key 的字符串(各家 AI 的 key 前缀、私钥块、写着 `XXX_API_KEY=` 而且后面跟着一长串的行);
- 这台电脑上的个人路径(`C:\\Users\\某人\\`、`/home/某人/`、`/Users/某人/`);
- 你用 --names 点名的词(比如示例网站的真名、邮箱)。

查到的内容**不印出来**:只印在哪个提交、哪个文件、哪一类,和遮住中间的几个字符。查到就退出码 1。
测试里编的假 key(`sk-test-…`)、文档里的占位写法(`sk-…`、`<your key>`)不算。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI key": re.compile(r"sk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}"),  # sk-ant- 开头的归上一行
    "Google key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "xAI key": re.compile(r"xai-[A-Za-z0-9]{40,}"),
    "Perplexity key": re.compile(r"pplx-[A-Za-z0-9]{32,}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "key assignment": re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN)\s*[=:]\s*[\"']?([A-Za-z0-9_\-]{24,})"),
    "personal path": re.compile(r"(?:[A-Za-z]:\\+Users\\+|/home/|/Users/)(?!Public|Default|runner|someone|you|name|user)"
                                r"([A-Za-z0-9_.\-]{2,})"),
}
# 明摆着是假的、占位的:测试里的编造值、文档里的省略写法
_FAKE = re.compile(r"sk-test-|placeholder|example|your[-_ ]?key|xxxx|0123456789abcdef|not-a-key|REDACTED", re.I)
_TEXT_SUFFIXES_SKIP = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".ico", ".woff", ".woff2", ".lock")


def _mask(text: str) -> str:
    return text if len(text) <= 8 else f"{text[:4]}…{text[-2:]}({len(text)} 个字符)"


def _find(line: str, extra: dict[str, re.Pattern[str]]) -> list[tuple[str, str]]:
    hits = []
    for name, pattern in {**PATTERNS, **extra}.items():
        for match in pattern.finditer(line):
            if name not in extra and _FAKE.search(line):
                continue
            hits.append((name, _mask(match.group(match.lastindex or 0))))
    return hits


def scan_tree(repo: Path, extra: dict[str, re.Pattern[str]]) -> list[tuple[str, str, str, str]]:
    """现在仓库里跟踪着的文件。"""
    listed = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True).stdout
    found = []
    for raw in listed.split(b"\0"):
        name = raw.decode("utf-8", errors="replace")
        if not name or name.lower().endswith(_TEXT_SUFFIXES_SKIP):
            continue
        try:
            text = (repo / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            found += [("现在的文件", name, kind, shown) for kind, shown in _find(line, extra)]
    return found


def scan_history(repo: Path, extra: dict[str, re.Pattern[str]]) -> list[tuple[str, str, str, str]]:
    """全部提交加进来过的每一行(删掉的文件也在历史里)。"""
    log = subprocess.Popen(["git", "-C", str(repo), "log", "--all", "-p", "--no-color", "--format=%x01%h"],
                           stdout=subprocess.PIPE)
    found, commit, name = [], "?", "?"
    assert log.stdout is not None
    for raw in log.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line.startswith("\x01"):
            commit = line[1:]
        elif line.startswith("+++ "):
            name = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not name.lower().endswith(_TEXT_SUFFIXES_SKIP):
            found += [(f"提交 {commit}", name, kind, shown) for kind, shown in _find(line[1:], extra)]
    log.wait()
    return found


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="查仓库里有没有 key、个人路径和点名的词。只读,查到的内容不印出来。")
    parser.add_argument("repos", nargs="*", default=["."], help="仓库文件夹(默认当前文件夹)")
    parser.add_argument("--no-history", action="store_true", help="只查现在的文件,不查提交历史")
    parser.add_argument("--names", default="", help="另外要查的词,逗号隔开(不分大小写)")
    parser.add_argument("--limit", type=int, default=12, help="每个仓库最多列几处(统计照全量)")
    args = parser.parse_args(argv)
    extra = {f"点名的词「{word}」": re.compile(re.escape(word), re.I)
             for word in (w.strip() for w in args.names.split(",")) if word}
    total = 0
    for repo in map(Path, args.repos):
        found = scan_tree(repo, extra) + ([] if args.no_history else scan_history(repo, extra))
        unique = sorted(set(found))
        total += len(unique)
        print(f"{repo.resolve().name}:{len(unique)} 处" + ("" if unique else "(现在的文件"
              + ("" if args.no_history else "和全部提交历史") + "里都没查到)"))
        for kind, count in Counter(item[2] for item in unique).most_common():
            print(f"  {kind}:{count} 处")
        for where, name, kind, shown in unique[: args.limit]:
            print(f"    {where} · {name} · {kind} · {shown}")
        if len(unique) > args.limit:
            print(f"    ……还有 {len(unique) - args.limit} 处没列")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
