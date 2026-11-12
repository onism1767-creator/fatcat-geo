"""抓网页的公共部件:统一的请求头、超时、跳转策略,可见文字与正文提取,网页自我介绍(标题 / H1 / 简介 / 结构化数据),
robots.txt 读取,疑似机器人防护的状态码。

只抓回、只提取,不做任何判定。正文提取用 trafilatura(S6.1,用户定;Apache-2.0)。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlsplit

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


def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """流式读响应正文,最多 max_bytes(超出截断);读完关流。返回 (正文, 是否截断)。"""
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
    return b"".join(chunks)[:max_bytes], truncated


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.IGNORECASE)
# 已经解压过的正文重建成响应时要去掉的头:留着 Content-Encoding,httpx 会把解压好的正文再解压一次、直接报错
# (审核 2026-10-28 第二轮红 1:真实网站几乎都压缩回包,官网审计 / 官网名单 / 第三方 robots 一起失效)
_DECODED_BODY_DROP = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def page_encoding(raw: bytes, header_charset: str | None) -> str:
    """网页文字的编码:响应头写了就按它;没写看开头的 BOM 与网页里的 <meta charset>;都没有按 UTF-8。
    只按 UTF-8 硬解时,没写编码的 GBK 中文页会变乱码、被误判「没写到你」(审核 2026-10-28 第二轮)。"""
    if header_charset:
        return header_charset
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    match = _META_CHARSET.search(raw[:4096])
    return match.group(1).decode("ascii", "ignore").lower() if match else "utf-8"


def decode_page(raw: bytes, header_charset: str | None) -> str:
    try:
        return raw.decode(page_encoding(raw, header_charset), errors="replace")
    except LookupError:  # 网页写了个不认识的编码名
        return raw.decode("utf-8", errors="replace")


def _capped_get(
    client: httpx.Client, url: str, headers: dict[str, str], max_bytes: int, *, follow_redirects: bool | None = None,
) -> httpx.Response:
    """GET 一次,正文最多读 max_bytes(超出截断,response.extensions["truncated"] 为 True);状态码、响应头、最终网址、
    跳转链照旧,文字编码按 page_encoding 定。follow_redirects 不给就按客户端的设置。网络错误照常抛 httpx.HTTPError。"""
    request = client.build_request("GET", url, headers=headers)
    follow = client.follow_redirects if follow_redirects is None else follow_redirects
    streamed = client.send(request, stream=True, follow_redirects=follow)
    raw, truncated = _read_capped(streamed, max_bytes)
    kept = {k: v for k, v in streamed.extensions.items() if k in ("http_version", "reason_phrase")}
    headers_out = [(k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _DECODED_BODY_DROP]
    rebuilt = httpx.Response(
        streamed.status_code, headers=headers_out, content=raw, request=streamed.request,
        history=streamed.history, extensions={**kept, "truncated": truncated},
    )
    rebuilt.encoding = page_encoding(raw, streamed.charset_encoding)
    return rebuilt


def get_as_browser(client: httpx.Client, url: str, *, max_bytes: int = MAX_PAGE_BYTES) -> httpx.Response:
    """以普通浏览器的身份请求,正文最多读 max_bytes(见 _capped_get)。
    网络错误照常抛 httpx.HTTPError,由调用方按自己的口径处理。
    原来整体读入没有上限,官网审计一页要抓十来次、网站地图整文件下载(审核 2026-10-27 黄 20)。"""
    return _capped_get(client, url, {"User-Agent": BROWSER_UA}, max_bytes)


def get_as(client: httpx.Client, url: str, user_agent: str, *, max_bytes: int = MAX_PAGE_BYTES) -> httpx.Response:
    """以指定身份请求(官网审计的逐爬虫探测用),正文同样封顶;原来整页读入、一页探测十来次(审核 2026-10-28 第二轮)。"""
    return _capped_get(client, url, {"User-Agent": user_agent}, max_bytes)


class VisitRefused(Exception):
    """跟随跳转时,调用方不让去下一站(比如目的站 robots 不让抓):没发请求。url 是被拒的那一站。
    故意不继承 httpx.HTTPError——它不是网络出错,调用方要按自己的口径记(看不到的原因不同)。"""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def fetch_page(
    client: httpx.Client, url: str, *, max_bytes: int = MAX_PAGE_BYTES, may_visit: Callable[[str], bool] | None = None,
) -> tuple[httpx.Response, str, bool]:
    """以浏览器身份 GET,正文最多读 max_bytes(超出截断并告知);返回 (响应, 解码后的文本, 是否截断)。
    网络错误照常抛 httpx.HTTPError。

    may_visit:给了就一跳一跳自己跟随跳转,每到一个新网址先问它能不能去,不能 → 抛 VisitRefused、不发那次请求
    (第三方站跳到另一个站时也要守目的站的 robots——第二轮审核 B2;原来交给 httpx 自动跟随,中途没机会查)。
    最多跳客户端的 max_redirects 次,超了抛 httpx.TooManyRedirects,与自动跟随一致。不给就照客户端设置自动跟随。"""
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"}
    request = client.build_request("GET", url, headers=headers)
    if may_visit is None:
        response = client.send(request, stream=True)
    else:
        hops = 0
        while True:
            response = client.send(request, stream=True, follow_redirects=False)
            # 3xx 没带 Location(300 多选、配置错的站)当终态,与自动跟随一致;
            # 原来硬取 headers["location"] 会 KeyError(第三轮审核 A16)
            if not response.has_redirect_location:
                break
            target = str(response.url.join(response.headers["location"]))
            response.close()
            hops += 1
            if hops > client.max_redirects:
                raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
            if not may_visit(target):
                raise VisitRefused(target)
            request = client.build_request("GET", target, headers=headers)
    raw, truncated = _read_capped(response, max_bytes)
    return response, decode_page(raw, response.charset_encoding), truncated


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html: str) -> str | None:
    match = _TITLE.search(html[:MAX_PARSED_BYTES])
    if match is None:
        return None
    title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()
    return title or None


_META_KEYS = r"og:title|og:description|twitter:title|twitter:description|description"
_META = re.compile(
    r"""<meta\s+[^>]*?(?:property|name)\s*=\s*["'](?P<key>""" + _META_KEYS + r""")["'][^>]*?"""
    r"""content\s*=\s*["'](?P<value>[^"']*)["']"""
    r"""|<meta\s+[^>]*?content\s*=\s*["'](?P<value2>[^"']*)["'][^>]*?"""
    r"""(?:property|name)\s*=\s*["'](?P<key2>""" + _META_KEYS + r""")["']""",
    re.IGNORECASE | re.DOTALL,
)
MIN_VISIBLE_CHARS: Final = 200  # 可见文字至少这么多才算「页面有正文」(产品文档 C02 同一阈值)


def _meta_fields(html: str) -> dict[str, str]:
    """标题 / 简介类元信息(og:/twitter:/description),同名只取第一条。"""
    found: dict[str, str] = {}
    for match in _META.finditer(html[:MAX_PARSED_BYTES]):
        key = (match.group("key") or match.group("key2") or "").lower()
        value = html_lib.unescape(match.group("value") or match.group("value2") or "").strip()
        if value and key not in found:
            found[key] = value
    return found


def extract_meta_summary(html: str) -> str | None:
    """网页自带的摘要:标题 + 简介(og:/twitter:/description 元信息)。没有简介就不算(光有标题太弱)。
    这是网页自己公开写给搜索引擎和分享卡片看的,脚本页的原始 HTML 里通常也有。"""
    found = _meta_fields(html)
    description = found.get("og:description") or found.get("description") or found.get("twitter:description")
    if not description:
        return None
    title = found.get("og:title") or found.get("twitter:title") or page_title(html)
    return f"{title}\n\n{description}" if title else description


def text_from_html(html: str, url: str | None = None) -> tuple[str, str] | None:
    """一个页面拿什么文字去判、来源标什么:
    足量正文(main)→ 足量可见文字(visible)→ 网页摘要(meta)→ 少量正文 / 可见文字 → 没有。"""
    main = extract_main_text(html, url)
    if main and len(main) >= MIN_VISIBLE_CHARS:
        return main, "main"
    visible = extract_visible_text(html)
    if len(visible) >= MIN_VISIBLE_CHARS:
        return visible, "visible"
    summary = extract_meta_summary(html)
    if summary:
        return summary, "meta"  # 正文太短(「Loading…」之类碎字)而网页自带摘要:摘要更可信
    if main:
        return main, "main"
    if visible:
        return visible, "visible"
    return None


def fetch_json(client: httpx.Client, url: str) -> object | None:
    """以浏览器身份 GET 一个 JSON 接口(如平台的公开嵌入接口);非 200 或不是 JSON → None。网络错误照常抛。"""
    response = get_as_browser(client, url)
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def extract_main_text(html: str, url: str | None = None) -> str | None:
    """正文(去掉导航、页脚、侧栏、评论):trafilatura;抽不出返回 None,由调用方决定要不要回退到全文。"""
    text = trafilatura.extract(
        html[:MAX_PARSED_BYTES], url=url, include_comments=False, include_tables=True, favor_recall=True,
    )
    return text.strip() if text and text.strip() else None


_UNRESERVED: Final = frozenset(string.ascii_letters + string.digits + "-._~")
_HEX: Final = frozenset(string.hexdigits)


def _normalize_path(value: str) -> str:
    """RFC 9309 2.2.2:比较前统一编码——非 ASCII 字符按 UTF-8 百分号编码,已编码的 %xx 里普通字符(字母数字 -._~)解开、
    其余十六进制统一大写。规则和网址两边都这样规整,「/产品/」才能对上「/%E4%BA%A7…/」(审核 2026-10-28 第二轮)。"""
    raw = value.encode("utf-8")
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x25 and i + 3 <= len(raw) and all(chr(b) in _HEX for b in raw[i + 1:i + 3]):
            decoded = chr(int(raw[i + 1:i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else "%" + raw[i + 1:i + 3].decode("ascii").upper())
            i += 3
            continue
        out.append(f"%{c:02X}" if c >= 0x80 or c <= 0x20 or c == 0x7F else chr(c))
        i += 1
    return "".join(out)


def _pattern_matches(pattern: str, path: str) -> bool:
    """robots 规则的路径模式:* 匹配任意串,结尾的 $ 锚定到路径末尾,其余按前缀;大小写敏感(RFC 9309)。
    双指针通配匹配,最坏 O(规则长 × 路径长)——不用正则:对方的 robots 是不可信输入,恶意的多个 * 能让正则回溯拖死整个服务
    (审核 2026-10-28 第二轮实测 12 个 * 配 40 字符路径要 52 秒)。"""
    anchored = pattern.endswith("$")
    p = pattern[:-1] if anchored else pattern + "*"
    i = j = 0
    star, mark = -1, 0
    while i < len(path):
        if j < len(p) and p[j] == "*":
            star, mark = j, i
            j += 1
        elif j < len(p) and p[j] == path[i]:
            i += 1
            j += 1
        elif star != -1:
            mark += 1
            i, j = mark, star + 1
        else:
            return False
    while j < len(p) and p[j] == "*":
        j += 1
    return j == len(p)


def _product_token(value: str) -> str:
    """User-agent 行的产品名:取到第一个 / 或空白为止、小写(「Googlebot/2.1」→ googlebot)。"""
    return re.split(r"[/\s]", value.strip(), maxsplit=1)[0].lower()


class RobotsRules:
    """robots.txt 按 RFC 9309 的口径判放行:
    - 连续的 User-agent 行是一组;产品名取到 / 或空白为止、大小写不敏感、**相等**才算对上
      (「Claude」组管不到 Claude-User);
    - 对上的组全部合并,一个都没对上才用全部 * 组合并;
    - 规则认 * 通配与结尾的 $,路径比较前统一百分号编码;最长匹配优先、等长 Allow 赢;空 Disallow 等于放行;
    - Sitemap 行随处收;开头的 BOM 跳过。
    标准库 urllib.robotparser 不认通配符(审核 2026-10-27 黄 12);第一版自写解析器按前缀认产品名、只取第一个组、
    不规整编码、BOM 丢组(审核 2026-10-28 第二轮)。
    unreadable() = robots 读不到(服务器出错 / 网络错误):RFC 9309 2.3.1.4 要求当全禁。"""

    def __init__(self, text: str, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self._groups: list[tuple[list[str], list[tuple[bool, str]]]] = []  # ([产品名], [(放行?, 规整后的模式)])
        self._sitemaps: list[str] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] | None = None
        for raw in text.lstrip("\ufeff").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if rules is not None:  # 上一组已经有规则:这行开新的一组
                    self._groups.append((agents, rules))
                    agents, rules = [], None
                token = _product_token(value)
                if token:  # 空的 User-agent 行不对任何爬虫
                    agents.append(token)
            elif key in ("allow", "disallow"):
                if not agents:
                    continue  # 不属于任何组的规则:忽略
                if rules is None:
                    rules = []
                rules.append((key == "allow", _normalize_path(value)))
            elif key == "sitemap" and value:
                self._sitemaps.append(value)
        if agents:
            self._groups.append((agents, rules or []))

    @classmethod
    def unreadable(cls) -> RobotsRules:
        """robots 读不到(服务器出错 / 网络错误):全禁(RFC 9309 2.3.1.4)。"""
        return cls("", unavailable=True)

    def site_maps(self) -> list[str]:
        return list(self._sitemaps)

    def _rules_for(self, token: str) -> list[tuple[bool, str]]:
        wanted = _product_token(token)
        matched = [rule for agents, rules in self._groups if wanted != "*" and wanted in agents for rule in rules]
        if matched:
            return matched
        return [rule for agents, rules in self._groups if "*" in agents for rule in rules]

    def can_fetch(self, token: str, url: str) -> bool:
        if self.unavailable:
            return False
        parts = urlsplit(url)
        path = _normalize_path((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: tuple[int, bool] | None = None  # (模式长度, 放行?)
        for allow, pattern in self._rules_for(token):
            if not pattern:
                continue  # 空 Disallow = 放行;空 Allow 没有意义
            if _pattern_matches(pattern, path) and (
                best is None or len(pattern) > best[0] or (len(pattern) == best[0] and allow)
            ):
                best = (len(pattern), allow)
        return True if best is None else best[1]


def robots_parser(client: httpx.Client, origin: str) -> RobotsRules:
    """读一个站的 robots.txt(以浏览器身份),按 RFC 9309 2.3.1:
    拿到 2xx 就解析;4xx(没有这个文件)= 没有规则、全放行;5xx、网络错误等读不到 = 全禁(unavailable)。
    原来读不到也当「没有规则」,对方 robots 一出错就照抓(审核 2026-10-28 第二轮)。
    不管客户端设没设跟随跳转,读 robots 一律跟随(RFC 9309 2.3.1.2 要求至少跟 5 次):跳转还原用的客户端不自动跟随,
    原来 robots.txt 一跳(比如 http 跳 https)就成了「读不到」、被当成全禁。"""
    try:
        response = _capped_get(client, f"{origin}/robots.txt", {"User-Agent": BROWSER_UA}, 512_000,
                               follow_redirects=True)
    except httpx.HTTPError:
        return RobotsRules.unreadable()
    if response.is_success:
        return RobotsRules(response.text)
    if 400 <= response.status_code < 500:
        return RobotsRules("")
    return RobotsRules.unreadable()


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


# ── 网页的自我介绍(S11.4 品类自动核对):只提取,不判定 ────────────────────────────────────────


@dataclass(frozen=True)
class PageIntro:
    """一个网页自己怎么介绍自己:标题、第一个 H1、简介(description 元信息)、结构化数据里组织 / 产品的名字与一句介绍。
    四样都没有 = 这页没说自己是干什么的;怎么办由调用方定,这里不猜。"""

    title: str | None
    h1: str | None
    description: str | None
    schema_names: tuple[str, ...]  # Organization / Product 节点的 name 与 description,按出现顺序去重

    @property
    def empty(self) -> bool:
        return not (self.title or self.h1 or self.description or self.schema_names)


INTRO_FIELD_MAX_CHARS: Final = 400  # 每一样最多留这么多字:自我介绍是几句话,不是整页正文
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_INTRO_SCHEMA_TYPES: Final = frozenset({"Organization", "Corporation", "LocalBusiness", "Product"})


def page_h1(html: str) -> str | None:
    """第一个 <h1> 的文字(去掉里面的标签与多余空白)。"""
    match = _H1.search(html[:MAX_PARSED_BYTES])
    if match is None:
        return None
    text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))).strip()
    return text or None


def page_description(html: str) -> str | None:
    """网页简介:description 元信息优先,其次 og:description / twitter:description。"""
    found = _meta_fields(html)
    return found.get("description") or found.get("og:description") or found.get("twitter:description") or None


class _JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blobs: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.blobs.append(data)


def _schema_intro(node: object) -> list[str]:
    """Organization / Product 节点(含 @graph、列表嵌套)的 name 与 description。"""
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += _schema_intro(item)
        return found
    if not isinstance(node, dict):
        return found
    types = node.get("@type")
    type_set = set(types) if isinstance(types, list) else {types} if isinstance(types, str) else set()
    if type_set & _INTRO_SCHEMA_TYPES:
        for field in ("name", "description"):
            value = node.get(field)
            if isinstance(value, str) and value.strip():
                found.append(value)
    for key in ("@graph", "publisher", "brand"):
        if key in node:
            found += _schema_intro(node[key])
    return found


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", html_lib.unescape(text)).strip()
    return text[:INTRO_FIELD_MAX_CHARS]


def extract_page_intro(html: str) -> PageIntro:
    """网页自我介绍:标题 / 第一个 H1 / 简介 / 结构化数据里组织与产品的名字和介绍。坏 JSON-LD 跳过,不猜。"""
    collector = _JsonLdCollector()
    collector.feed(html[:MAX_PARSED_BYTES])
    names: list[str] = []
    for blob in collector.blobs:
        try:
            names += _schema_intro(json.loads(blob))
        except ValueError:
            continue
    clipped = [_clip(name) for name in names]
    return PageIntro(
        title=_clip(title) if (title := page_title(html)) else None,
        h1=_clip(h1) if (h1 := page_h1(html)) else None,
        description=_clip(desc) if (desc := page_description(html)) else None,
        schema_names=tuple(dict.fromkeys(name for name in clipped if name)),
    )
