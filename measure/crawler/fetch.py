"""抓网页的公共部件:统一的请求头、超时、跳转策略,可见文字与正文提取,robots.txt 读取,疑似机器人防护的状态码。

只抓回、只提取,不做任何判定。正文提取用 trafilatura(S6.1,用户定;Apache-2.0)。
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Final
from urllib import robotparser

import httpx
import trafilatura

BROWSER_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_S: Final = 15.0
# 核实第三方网页的超时与体积(产品文档 10.4 / D23):连接 5 秒、总计 10 秒;单页 5 MB,超出截断并告知
FETCH_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
MAX_PAGE_BYTES: Final = 5_000_000
# 疑似机器人防护的拒绝状态(401/403/429/503):被拒只说明「这个客户端此刻被拒」,不能判定页面不可达
PROTECTION_SUSPECT_STATUSES: Final = (401, 403, 429, 503)
MAX_PARSED_BYTES: Final = 2_000_000


def new_client(*, follow_redirects: bool = True, timeout: httpx.Timeout | float = TIMEOUT_S) -> httpx.Client:
    """抓网页用的客户端:统一超时;默认跟随跳转,要自己数跳(看跳转去向)时传 follow_redirects=False。调用方负责关闭。"""
    return httpx.Client(timeout=timeout, follow_redirects=follow_redirects)


def get_as_browser(client: httpx.Client, url: str) -> httpx.Response:
    """以普通浏览器的身份请求(网络错误照常抛 httpx.HTTPError,由调用方按自己的口径处理)。"""
    return client.get(url, headers={"User-Agent": BROWSER_UA})


def fetch_page(client: httpx.Client, url: str, *, max_bytes: int = MAX_PAGE_BYTES) -> tuple[httpx.Response, str, bool]:
    """以浏览器身份 GET,正文最多读 max_bytes(超出截断并告知);返回 (响应, 解码后的文本, 是否截断)。
    网络错误照常抛 httpx.HTTPError。"""
    request = client.build_request(
        "GET", url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"},
    )
    response = client.send(request, stream=True)
    chunks: list[bytes] = []
    size, truncated = 0, False
    try:
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                truncated = True
                break
    finally:
        response.close()
    raw = b"".join(chunks)[:max_bytes]
    try:
        text = raw.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return response, text, truncated


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html: str) -> str | None:
    match = _TITLE.search(html[:MAX_PARSED_BYTES])
    if match is None:
        return None
    title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()
    return title or None


def extract_main_text(html: str, url: str | None = None) -> str | None:
    """正文(去掉导航、页脚、侧栏、评论):trafilatura;抽不出返回 None,由调用方决定要不要回退到全文。"""
    text = trafilatura.extract(
        html[:MAX_PARSED_BYTES], url=url, include_comments=False, include_tables=True, favor_recall=True,
    )
    return text.strip() if text and text.strip() else None


def robots_parser(client: httpx.Client, origin: str) -> robotparser.RobotFileParser | None:
    """读一个站的 robots.txt(以浏览器身份);拿到 200 才解析,其余(没有 / 出错)当作没有规则,返回 None。"""
    try:
        response = get_as_browser(client, f"{origin}/robots.txt")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    parser = robotparser.RobotFileParser()
    parser.parse(response.text[:512_000].splitlines())
    return parser


def fetch_headers(client: httpx.Client, url: str) -> httpx.Response:
    """以浏览器身份请求、只取响应头不读正文(看状态码与 Location 用)。网络错误照常抛 httpx.HTTPError。"""
    request = client.build_request("GET", url, headers={"User-Agent": BROWSER_UA})
    response = client.send(request, stream=True)
    response.close()
    return response


def is_html(response: httpx.Response) -> bool:
    return "html" in response.headers.get("content-type", "")


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "title"}  # 标题不是正文可见文字

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
