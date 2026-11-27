"""本机启动入口:python -m report(127.0.0.1:8600)。

启动时从库中任务快照恢复登记处(v0.4.9):重启不失忆,中断任务可续跑。
数据库必须先起(环境三步曲),恢复失败即启动失败——显式报错,不带病运行。
"""

import sys

import uvicorn

from engine.envfile import load_env_file, utf8_output
from engine.loader import load_surface_registry
from engine.storage import Storage, StorageUnavailable, default_dsn
from report.app import create_app
from report.archive import restore_registry

utf8_output()  # 日志被预览窗口 / 管道接走时写 UTF-8,中文不乱码(2026-10-27)
load_env_file()
try:
    with Storage(default_dsn()) as startup_storage:
        registry = restore_registry(
            startup_storage, known_surfaces={entry.name for entry in load_surface_registry()},
        )
    app = create_app(registry=registry)
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
