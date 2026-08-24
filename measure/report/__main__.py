"""本机启动入口:python -m report(127.0.0.1:8600)。"""

import uvicorn

from engine.envfile import load_env_file
from report.app import create_app

load_env_file()
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")
