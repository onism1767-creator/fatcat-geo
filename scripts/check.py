"""发布前的自动检查,一条命令跑完:格式、测试、型号核对、证据表核对、查 key 泄露。

    python scripts/check.py            全跑
    python scripts/check.py --quick    只跑不依赖另外三个项目的(格式、测试、查本仓库)

哪一项没过都接着往下跑,最后一起报;有没过的退出码是 1。全程不联网、不花钱。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    quick = "--quick" in argv
    steps: list[tuple[str, list[str]]] = [
        ("格式(ruff)", [PY, "-m", "ruff", "check", "."]),
        ("测试(pytest)", [PY, "-m", "pytest", "-q"]),
        ("查 key 和个人路径(本仓库,含全部提交历史)", [PY, "scripts/scan_secrets.py", "."]),
    ]
    if not quick:
        steps += [
            ("统一型号清单", [PY, "-m", "fatcat", "models", "check"]),
            ("证据表", [PY, "-m", "fatcat", "evidence", "check"]),
        ]
    failed = []
    for name, command in steps:
        print(f"\n== {name}", flush=True)
        if subprocess.run(command, cwd=ROOT).returncode != 0:
            failed.append(name)
    print("\n" + ("全部通过。" if not failed else f"没过的 {len(failed)} 项:" + "、".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
