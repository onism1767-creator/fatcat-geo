"""本地导入:读一个文件夹里站长自己存下的 HTML 文件,拼成站点快照。不联网。

为什么有这条路:有的站对程序请求下发人机验证。Butter 遇验证码即停,不绕、不伪装成真人。
站长是站的主人:在自己的浏览器里正常打开页面,存成 HTML 放进一个文件夹,Butter 只读这些文件。
后面的事实库、审计、生成器吃的是同一种快照,不用改。

文件夹里认这些(只看这一层,不进子文件夹):
  *.html / *.htm    页面,最多 30 个,每个最多 2 MB
  robots.txt        可选:有就按它生成 robots 三段式;没有就不生成(不猜原有规则)
  sitemap.xml       可选:有就读其中的 lastmod
  urls.txt          可选:每行「文件名 网址」,说明哪个文件对应哪个网址;# 开头是注释

每个页面对应哪个网址,按这个顺序认,认到就停:
  1. urls.txt 里写的(写了就以它为准;写错了这个文件就不导入)
  2. 文件开头的「saved from url」注释(浏览器「网页,全部」方式保存时会写)
  3. 页面里的 <link rel="canonical">
  4. 页面里的 og:url
  5. 文件名是 index.html / index.htm,或者文件夹里只有这一个页面:当作你给的那个网址
认不出的不导入(不猜地址),并在备注里写明。认出的地址必须和你给的网址是同一个主机。

文件内容和联网抓到的一样,是不可信数据:只解析,不执行,不跟随里面的任何链接。本模块只读文件,不写。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from butter.contracts import FetchedPage, PageRole, RobotsFetch, SitemapEntry, SiteSnapshot
from butter.extract.challenge import looks_like_challenge
from butter.extract.sitemap import parse_sitemap
from butter.util import decode_html, host_of, normalize_site_url, page_key, safe_http_url, same_site

MAX_FILES = 30
MAX_PAGE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 512_000
MAX_SITEMAP_BYTES = 5_000_000
MAX_MANIFEST_BYTES = 200_000
MANIFEST_NAME = "urls.txt"
_PAGE_EXT = (".html", ".htm")
_UNSUPPORTED_EXT = (".mhtml", ".mht", ".webarchive")
_INDEX_NAMES = ("index.html", "index.htm")
# Chromium 用「网页,全部」保存时写在文件开头的注释;它同时说明这份文件是脚本运行后的页面
_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.IGNORECASE)
_HEAD_CHARS = 300_000


def _read_capped(path: Path, cap: int) -> tuple[bytes, bool]:
    with open(path, "rb") as handle:
        raw = handle.read(cap + 1)
    return raw[:cap], len(raw) > cap


def _read_text(path: Path, cap: int) -> tuple[str, bool]:
    raw, cut = _read_capped(path, cap)
    return decode_html(raw).lstrip("﻿"), cut


def _read_manifest(path: Path) -> tuple[dict[str, str], list[str]]:
    """urls.txt:每行「文件名 网址」。网址里没有空白,所以取最后一段当网址,前面的都是文件名。"""
    mapping: dict[str, str] = {}
    notes: list[str] = []
    text, cut = _read_text(path, MAX_MANIFEST_BYTES)
    if cut:
        notes.append(f"{MANIFEST_NAME} 太大,只读了前 {MAX_MANIFEST_BYTES // 1000} KB。")
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            notes.append(f"{MANIFEST_NAME} 第 {number} 行看不懂(要写成「文件名 网址」),已跳过。")
            continue
        mapping[parts[0].strip().casefold()] = parts[1].strip()
    return mapping, notes


def _declared_urls(html: str, base: str) -> list[tuple[str, str]]:
    """页面自己声明的地址:(怎么声明的, 地址)。相对地址按站点根补全。"""
    found: list[tuple[str, str]] = []
    soup = BeautifulSoup(html[:_HEAD_CHARS], "lxml")
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        rel_words = [r.lower() for r in (rel if isinstance(rel, list) else str(rel).split())]
        if "canonical" in rel_words and (url := safe_http_url(str(link.get("href")), base)):
            found.append(("页面里的 canonical", url))
            break
    for meta in soup.find_all("meta"):
        if str(meta.get("property") or meta.get("name") or "").strip().lower() == "og:url":
            if url := safe_http_url(str(meta.get("content") or ""), base):
                found.append(("页面里的 og:url", url))
            break
    return found


def _resolve_url(name: str, html: str, manifest: dict[str, str], site: str, origin: str, host: str,
                 only_page: bool) -> tuple[str | None, str, str]:
    """返回 (网址, 地址来自哪里, 认不出时的原因)。"""
    def on_site(url: str | None) -> bool:
        return bool(url) and same_site(host_of(url or ""), host)

    if name.casefold() in manifest:  # 站长明确写了:以它为准,写错了就不导入,不退回去猜
        url = safe_http_url(manifest[name.casefold()])
        if url is None:
            return None, "", f"{MANIFEST_NAME} 里给它写的不是 http / https 网址"
        if not on_site(url):
            return None, "", f"{MANIFEST_NAME} 里给它写的网址在别的主机({host_of(url)}),和你给的 {host} 不是同一个站"
        return url, MANIFEST_NAME, ""

    off_site: list[str] = []
    candidates: list[tuple[str, str]] = []
    if match := _SAVED_FROM.search(html[:4000]):
        if url := safe_http_url(match.group(1)):
            candidates.append(("文件开头的 saved from url 注释", url))
    candidates += _declared_urls(html, f"{origin}/")
    for how, url in candidates:
        if on_site(url):
            return url, how, ""
        off_site.append(f"{how}写的是别的主机 {host_of(url)}")
    if name.casefold() in _INDEX_NAMES:
        return site, "文件名是 index,当作你给的网址", ""
    if only_page:
        return site, "文件夹里只有这一个页面,当作你给的网址", ""
    return None, "", ";".join(off_site) or "页面里没有 canonical 或 og:url"


def import_site(site_url: str, html_dir: Path, *, max_files: int = MAX_FILES) -> SiteSnapshot:
    """读文件夹 → 站点快照。一个页面都导不进来就报错,并说明每个文件为什么没导入。"""
    site = normalize_site_url(site_url)
    parts = urlsplit(site)
    host = parts.hostname or ""
    origin = f"{parts.scheme}://{parts.netloc}"
    if not html_dir.is_dir():
        raise ValueError(f"找不到文件夹:{html_dir}")

    entries = sorted((p for p in html_dir.iterdir() if p.is_file() and not p.is_symlink()),
                     key=lambda p: p.name.casefold())
    by_name = {p.name.casefold(): p for p in entries}
    skipped: list[str] = []  # 没导入的文件和原因
    notes: list[str] = []

    manifest: dict[str, str] = {}
    if MANIFEST_NAME in by_name:
        manifest, manifest_notes = _read_manifest(by_name[MANIFEST_NAME])
        notes += manifest_notes
    for path in entries:
        if path.suffix.lower() in _UNSUPPORTED_EXT:
            skipped.append(f"{path.name}:不支持这种格式,没有导入。请在浏览器里另存为「网页,仅 HTML」。")
    page_files = [p for p in entries if p.suffix.lower() in _PAGE_EXT]
    if len(page_files) > max_files:
        skipped.append(f"文件夹里有 {len(page_files)} 个页面文件,只读了按文件名排在前面的 {max_files} 个。")
        page_files = page_files[:max_files]

    pages: list[FetchedPage] = []
    mapped: list[str] = []
    seen: dict[str, str] = {}
    for path in page_files:
        raw, cut = _read_capped(path, MAX_PAGE_BYTES)
        html = decode_html(raw).lstrip("﻿")
        if not html.strip():
            skipped.append(f"{path.name}:文件是空的,没有导入。")
            continue
        if marker := looks_like_challenge(None, {}, html):
            skipped.append(f"{path.name}:像是人机验证页(标记:{marker}),不是真正的页面,没有导入。"
                           "请等页面正常显示之后再保存。")
            continue
        url, how, why_not = _resolve_url(path.name, html, manifest, site, origin, host, len(page_files) == 1)
        if url is None:
            skipped.append(f"{path.name}:认不出它对应哪个网址,没有导入({why_not})。"
                           f"在 {MANIFEST_NAME} 里加一行「{path.name} 网址」即可。")
            continue
        key = page_key(url)
        if key in seen:
            skipped.append(f"{path.name}:和 {seen[key]} 对应同一个网址 {url},只导入了前一个。")
            continue
        seen[key] = path.name
        role: PageRole = "given" if key == page_key(site) else ("home" if key == page_key(f"{origin}/") else "discovered")
        pages.append(FetchedPage(
            url=url, final_url=url, role=role, status=None, content_type="text/html", html=html, truncated=cut,
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, UTC), local_file=path.name,
            rendered_dom=bool(_SAVED_FROM.search(html[:4000]))))
        mapped.append(f"{path.name} → {url}(地址来自:{how})" + (f";文件超过 {MAX_PAGE_BYTES // 1_000_000} MB,只读了前面一段" if cut else ""))

    if not pages:
        reasons = "\n".join(f"  - {line}" for line in skipped) or "  - 文件夹里没有 .html / .htm 文件"
        raise ValueError(f"没有导入任何页面(文件夹:{html_dir}):\n{reasons}")
    pages.sort(key=lambda p: {"given": 0, "home": 1, "discovered": 2}[p.role])

    robots_url = f"{origin}/robots.txt"
    if "robots.txt" in by_name:
        text, cut = _read_text(by_name["robots.txt"], MAX_ROBOTS_BYTES)
        if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")) or looks_like_challenge(None, {}, text):
            robots = RobotsFetch(url=robots_url, state="unreadable",
                                 note="文件夹里的 robots.txt 是一个网页,不是规则文件;请打开 /robots.txt 后把文字原样存成 robots.txt")
        elif cut:
            robots = RobotsFetch(url=robots_url, state="unreadable", note="文件夹里的 robots.txt 太大,没有读完")
        else:
            robots = RobotsFetch(url=robots_url, state="ok", text=text, note="读自本机文件 robots.txt")
    else:
        robots = RobotsFetch(url=robots_url, state="unreadable", note="本地导入没有联网,文件夹里也没有 robots.txt")

    sitemap_urls: list[str] = []
    sitemap_entries: list[SitemapEntry] = []
    if "sitemap.xml" in by_name:
        text, cut = _read_text(by_name["sitemap.xml"], MAX_SITEMAP_BYTES)
        parsed = parse_sitemap(text)
        sitemap_url = f"{origin}/sitemap.xml"
        if parsed.error:
            notes.append(f"文件夹里的 sitemap.xml:{parsed.error},没有用。")
        else:
            sitemap_urls.append(sitemap_url)
            for loc, lastmod in parsed.entries:
                safe = safe_http_url(loc)
                if safe and same_site(host_of(safe), host):
                    sitemap_entries.append(SitemapEntry(loc=safe, lastmod=lastmod, source_url=sitemap_url))
            if cut or parsed.truncated:
                notes.append(f"文件夹里的 sitemap.xml 太大,只读了前 {len(parsed.entries)} 条。")
            if parsed.children and not parsed.entries:
                notes.append("文件夹里的 sitemap.xml 是一个索引(里面列的是子 sitemap),没有 lastmod 可读。")

    saved = max(p.fetched_at for p in pages)
    head = [f"本次没有联网:页面来自本机文件夹「{html_dir.name}」里的 {len(pages)} 个 HTML 文件。"
            f"读取时间按文件的修改时间记(最新的一个是 {saved.date().isoformat()})。"]
    rendered = [p.local_file for p in pages if p.rendered_dom]
    if rendered:
        head.append(f"{len(rendered)} 个文件像是浏览器「网页,全部」方式保存的({'、'.join(rendered[:5])}"
                    f"{' 等' if len(rendered) > 5 else ''}):内容是脚本运行之后的页面,不是服务器返回的原始 HTML,"
                    "爬虫看到的可能比这少。想按原始 HTML 检查,请另存为「网页,仅 HTML」后重跑。")
    return SiteSnapshot(
        site_url=site, host=host, origin=origin, fetched_at=saved, user_agent="", source="local_files", pages=pages,
        robots=robots, sitemap_urls=sitemap_urls, sitemap_entries=sitemap_entries, stop_reason="completed",
        notes=head + mapped + skipped + notes)
