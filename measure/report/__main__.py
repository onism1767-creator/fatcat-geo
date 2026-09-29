"""本机启动入口:python -m report(127.0.0.1:8600)。

启动时从库中任务快照恢复登记处(v0.4.9):重启不失忆,中断任务可续跑。
数据库必须先起(环境三步曲),恢复失败即启动失败——显式报错,不带病运行。
"""

import os
import sys

import uvicorn

from engine.envfile import load_env_file
from engine.storage import StorageUnavailable
from report.app import DEFAULT_DSN, create_app
from report.archive import restore_registry

load_env_file()
try:
    app = create_app(registry=restore_registry(os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)))
except StorageUnavailable as exc:  # v0.4.22:不再无声挂起(2026-09-25 事故)
    print(
        "[DB_UNREACHABLE] 连不上本机数据库(端口 5433),服务没有启动。\n"
        "  先运行:powershell -ExecutionPolicy Bypass -File scripts/pgdev.ps1 status\n"
        "  显示 STOPPED 就 start;显示 PG_ZOMBIE(在听但不应答)就 restart。\n"
        f"  原话:{exc}",
        file=sys.stderr,
    )
    sys.exit(2)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")
