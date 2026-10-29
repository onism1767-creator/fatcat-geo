"""手动冒烟(S6.2):用无头浏览器渲染一个网址,打印状态、标题、正文抽取结果——确认 Playwright 与浏览器内核装好了。

用法:python scripts/smoke_render.py https://example.com
只联网一次、不落库、不做判定。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.fetch import extract_main_text, page_title  # noqa: E402
from crawler.render import PlaywrightRenderer, playwright_available  # noqa: E402


def main(url: str) -> int:
    sys.stdout.reconfigure(errors="replace")  # Windows 控制台是 GBK,网页里的生僻字符别把脚本打崩
    if not playwright_available():
        print(r"Playwright 没装:uv sync --locked,再 .venv\Scripts\python -m playwright install chromium")
        return 2
    renderer = PlaywrightRenderer()
    try:
        page = renderer.render(url)
    finally:
        renderer.close()
    if page is None:
        print("渲染失败(超时 / 导航错误 / 浏览器内核缺失),见日志")
        return 1
    text = extract_main_text(page.html, page.final_url)
    print(f"状态 {page.http_status}  终点 {page.final_url}")
    print(f"标题 {page_title(page.html)!r}")
    print(f"HTML {len(page.html)} 字符;正文 {len(text) if text else 0} 字符")
    if text:
        print("正文开头:", text[:200].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "https://example.com"))
