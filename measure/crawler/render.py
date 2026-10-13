"""无头浏览器渲染(v0.4.26 S6.2,用户定 2026-10-13):只给「原始 HTML 抽不出正文」的网页用——
像真实浏览器一样执行脚本后再取 HTML。

这不是绕过:同一个固定版本的 Chrome 身份(D13)、不带 cookie、不登录、不处理同意弹窗、不碰验证码、不换 IP。
Playwright 或浏览器内核没装就不渲染,调用方照旧记「抽不出正文」。
只做取回,不做任何判定;与 crawler/ 其余部分一样零项目内依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

import structlog

from crawler.fetch import BROWSER_UA

log = structlog.get_logger("crawler.render")

RENDER_TIMEOUT_S: Final = 10.0
_VIEWPORT: Final = {"width": 1280, "height": 900}


@dataclass(frozen=True)
class RenderedPage:
    html: str  # 执行脚本后的 DOM 序列化
    final_url: str
    http_status: int | None  # 主文档响应状态;浏览器没给(如 about:blank)时 None


class Renderer(Protocol):
    def render(self, url: str) -> RenderedPage | None: ...
    def close(self) -> None: ...


def playwright_available() -> bool:
    """装了 Playwright 包就算可用;浏览器内核缺失会在首次渲染时暴露为失败(调用方记原因)。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class PlaywrightRenderer:
    """同步 Playwright,一个 Chromium 实例串行渲染(Playwright 对象不可跨线程用)。调用方负责 close()。"""

    def __init__(self, *, timeout_s: float = RENDER_TIMEOUT_S) -> None:
        self._timeout_ms = int(timeout_s * 1000)
        self._playwright = None
        self._browser = None

    def _ensure_browser(self):  # type: ignore[no-untyped-def]  # Playwright 的类型随版本变,不在此处钉死
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        return self._browser

    def render(self, url: str) -> RenderedPage | None:
        try:
            browser = self._ensure_browser()
            context = browser.new_context(user_agent=BROWSER_UA, viewport=_VIEWPORT, java_script_enabled=True,
                                          locale="en-US")
            try:
                page = context.new_page()
                response = page.goto(url, wait_until="load", timeout=self._timeout_ms)
                page.wait_for_timeout(500)  # 给首屏脚本半秒把正文放进 DOM
                html = page.content()
                return RenderedPage(html=html, final_url=page.url,
                                    http_status=response.status if response is not None else None)
            finally:
                context.close()
        except Exception as exc:  # Playwright 的超时 / 导航错误 / 浏览器内核缺失:都如实返回 None,由调用方记原因
            log.warning("render_failed", url=url, error=type(exc).__name__)
            return None

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception as exc:  # 收尾失败不影响结果
            log.warning("render_close_failed", error=type(exc).__name__)
        self._browser = self._playwright = None
