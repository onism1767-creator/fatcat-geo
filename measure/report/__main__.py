"""本机启动入口:python -m report(127.0.0.1:8600)。

启动时从库中任务快照恢复登记处(v0.4.9):重启不失忆,中断任务可续跑。
数据库必须先起(环境三步曲),恢复失败即启动失败——显式报错,不带病运行。
"""

import os

import uvicorn

from engine.envfile import load_env_file
from report.app import DEFAULT_DSN, create_app
from report.jobs import restore_registry

load_env_file()
app = create_app(registry=restore_registry(os.environ.get("GEO_DATABASE_URL", DEFAULT_DSN)))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")
