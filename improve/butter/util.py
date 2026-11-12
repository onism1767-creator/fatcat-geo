"""小工具:文本归一、主机名比较、片段截取。全是纯函数。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

_WS = re.compile(r"\s+")

# 抓取结束原因的中文说法(报告和命令行用;机器可读的文件里仍写英文代码)
STOP_REASON_LABEL = {
    "completed": "正常走完", "challenge": "遇到人机验证,已停", "refused": "被站点拒绝,已停",
    "time_budget": "到了总时长上限", "off_host": "跳去了别的主机", "error": "网络或读取出错",
}


def stop_label(reason: str) -> str:
    return f"{STOP_REASON_LABEL.get(reason, reason)}({reason})"


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def norm_text(text: str) -> str:
    """比较用的归一:全半角统一、去多余空白、不分大小写。只用于比对,不改写存下来的值。"""
    return collapse_ws(unicodedata.normalize("NFKC", text)).casefold()


def snippet(text: str, limit: int = 160) -> str:
    text = collapse_ws(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def md_cell(text: str | None, limit: int = 140) -> str:
    """把外来文字放进 Markdown 之前先中和:压成一行,竖线、方括号、尖括号、反引号都转义。
    网页里的文字是不可信数据,不能让它在报告里变成链接、图片或 HTML。"""
    if not text:
        return ""
    text = collapse_ws(str(text))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    for ch in ("\\", "|", "[", "]", "`"):
        text = text.replace(ch, "\\" + ch)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def strip_www(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def same_site(host_a: str, host_b: str) -> bool:
    """同一主机(www. 有无算同一个)。不做注册域名推断:子域一律当别的主机。"""
    return bool(host_a) and strip_www(host_a) == strip_www(host_b)


def host_matches(host: str, domain: str) -> bool:
    """host 等于 domain 或是它的子域。"""
    host, domain = strip_www(host), strip_www(domain)
    return bool(domain) and (host == domain or host.endswith("." + domain))


def safe_http_url(url: str, base: str | None = None) -> str | None:
    """只认 http / https 的绝对地址;其余(javascript:、data:、mailto: …)一律丢弃。"""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        joined = urljoin(base, url) if base else url
        parts = urlsplit(joined)
        _ = parts.port  # 端口写得不对会在这里抛错
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def normalize_site_url(url: str) -> str:
    """用户给的网址:没写协议就补 https,不是 http / https 就拒收。"""
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        # 没写协议就补 https;但「javascript:…」「mailto:…」这类不是网址(example.com:8080 这种主机加端口的放行)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:(?!\d+(?:/|$))", url):
            raise ValueError(f"不是可用的 http / https 网址:{url!r}")
        url = "https://" + url
    safe = safe_http_url(url)
    if safe is None:
        raise ValueError(f"不是可用的 http / https 网址:{url!r}")
    return safe


_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w-]+)", re.IGNORECASE)


def decode_html(raw: bytes, content_type: str = "") -> str:
    """字节 → 文字:先信响应头里的 charset,再信页面里的 <meta charset>,再试 utf-8、gb18030。"""
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.IGNORECASE)
    declared = match.group(1) if match else None
    if declared is None and (meta := _META_CHARSET.search(raw[:4096])):
        declared = meta.group(1).decode("ascii", "replace")
    for encoding in (declared, "utf-8", "gb18030"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def page_key(url: str) -> str:
    """判断两个地址是不是同一页:不看协议、www、末尾斜杠和片段。"""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{strip_www(parts.hostname or '')}{path}{query}"


def slugify(text: str, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    slug = re.sub(r"[^a-z0-9一-鿿]+", "-", text).strip("-")
    return slug[:60] or fallback
