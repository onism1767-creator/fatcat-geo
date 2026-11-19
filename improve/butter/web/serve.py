"""启动本地服务(外壳)。只绑定 127.0.0.1;端口默认 8700,被占时依次试到 8709(设计 §12)。"""

from __future__ import annotations

import socket
import webbrowser
from pathlib import Path

import uvicorn

from butter.web.app import WebSettings, create_app

HOST = "127.0.0.1"
PORTS = range(8700, 8710)


def free_port() -> int | None:
    for port in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return port
    return None


def serve(out_root: Path, workspace: Path, *, open_browser: bool = True) -> int:
    port = free_port()
    if port is None:
        print(f"端口 {PORTS.start} 到 {PORTS.stop - 1} 都被占了,服务没有启动。")
        return 1
    app = create_app(WebSettings(out_root=out_root, workspace=workspace, port=port))
    url = f"http://{HOST}:{port}/"
    print(f"Butter 服务已启动:{url}")
    print("关掉这个窗口就停止服务。")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0
