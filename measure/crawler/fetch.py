"""抓网页的公共部件:统一的请求头、超时、跳转策略,可见文字提取,疑似机器人防护的状态码。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

import httpx

BROWSER_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_S: Final = 15.0
# 疑似机器人防护的拒绝状态(401/403/429/503):被拒只说明「这个客户端此刻被拒」,不能判定页面不可达
PROTECTION_SUSPECT_STATUSES: Final = (401, 403, 429, 503)
MAX_PARSED_BYTES: Final = 2_000_000


def new_client(*, follow_redirects: bool = True) -> httpx.Client:
    """抓网页用的客户端:统一超时;默认跟随跳转,要自己数跳(看跳转去向)时传 follow_redirects=False。调用方负责关闭。"""
    return httpx.Client(timeout=TIMEOUT_S, follow_redirects=follow_redirects)


def get_as_browser(client: httpx.Client, url: str) -> httpx.Response:
    """以普通浏览器的身份请求(网络错误照常抛 httpx.HTTPError,由调用方按自己的口径处理)。"""
    return client.get(url, headers={"User-Agent": BROWSER_UA})


def fetch_headers(client: httpx.Client, url: str) -> httpx.Response:
    """以浏览器身份请求、只取响应头不读正文(看状态码与 Location 用)。网络错误照常抛 httpx.HTTPError。"""
    request = client.build_request("GET", url, headers={"User-Agent": BROWSER_UA})
    response = client.send(request, stream=True)
    response.close()
    return response


def is_html(response: httpx.Response) -> bool:
    return "html" in response.headers.get("content-type", "")


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def extract_visible_text(html: str) -> str:
    """去掉 script / style 等之后的可见文字(空白压成一个空格)。"""
    extractor = _TextExtractor()
    extractor.feed(html[:MAX_PARSED_BYTES])
    return re.sub(r"\s+", " ", "".join(extractor.parts)).strip()
