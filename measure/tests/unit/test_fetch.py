"""crawler/fetch 公共部件:robots.txt 按 RFC 9309 的口径判(通配符、结尾 $、最长匹配、等长 Allow 赢)——标准库不认通配符
(审核 2026-10-27 黄 12);以浏览器身份整页取回有体积上限(黄 20)。respx 拦截,零真实网络。"""

from __future__ import annotations

import httpx
import pytest
import respx

from crawler.fetch import MAX_PAGE_BYTES, RobotsRules, fetch_page, get_as_browser, robots_parser


@pytest.mark.parametrize("robots, token, path, allowed", [
    ("User-agent: *\nDisallow: /*\n", "AnyBot", "/", False),  # 标准库会把它当成放行
    ("User-agent: *\nDisallow: /$\n", "AnyBot", "/", False),
    ("User-agent: *\nDisallow: /$\n", "AnyBot", "/page", True),
    ("User-agent: *\nDisallow: /\nAllow: /\n", "AnyBot", "/x", True),  # 等长:Allow 赢
    ("User-agent: *\nDisallow: /private/\nAllow: /private/open\n", "AnyBot", "/private/open/1", True),  # 最长匹配
    ("User-agent: *\nDisallow: /private/\nAllow: /private/open\n", "AnyBot", "/private/x", False),
    ("User-agent: *\nDisallow: /*.pdf$\n", "AnyBot", "/a/b.pdf", False),
    ("User-agent: *\nDisallow: /*.pdf$\n", "AnyBot", "/a/b.pdf?x=1", True),
    ("User-agent: *\nDisallow: /Private\n", "AnyBot", "/private", True),  # 路径大小写敏感
    ("User-agent: *\nDisallow:\n", "AnyBot", "/", True),  # 空 Disallow = 放行
    ("User-Agent: TestSearchBot\nUser-agent: OtherBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
     "testsearchbot", "/", False),  # 多个 UA 一组、产品名大小写不敏感
    ("User-Agent: TestSearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "OtherBot", "/", True),  # 没有专门组用 *
    ("User-agent: TestSearchBot\nDisallow: /\n", "OtherBot", "/", True),  # 连 * 组都没有:放行
    ("Disallow: /\n", "AnyBot", "/", True),  # 不属于任何组的规则:忽略
    ("# 全是注释\n", "AnyBot", "/", True),
])
def test_robots_rules_follow_rfc_9309(robots, token, path, allowed):
    assert RobotsRules(robots).can_fetch(token, f"https://example.test{path}") is allowed


def test_robots_rules_collect_sitemaps_anywhere():
    rules = RobotsRules("Sitemap: https://a.test/s1.xml\nUser-agent: *\nDisallow: /x  # 注释\nSitemap: https://a.test/s2.xml\n")
    assert rules.site_maps() == ["https://a.test/s1.xml", "https://a.test/s2.xml"]
    assert rules.can_fetch("*", "https://a.test/x/y") is False and rules.can_fetch("*", "https://a.test/y") is True


@respx.mock
def test_robots_parser_parses_a_200_and_treats_missing_as_no_rules():
    """200 → 解析;404 → 没有规则、全放行(读不到的情形见下面第二轮那条测试)。"""
    respx.get("https://a.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /*\n"))
    respx.get("https://b.test/robots.txt").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        assert robots_parser(client, "https://a.test").can_fetch("*", "https://a.test/") is False
        missing = robots_parser(client, "https://b.test")
        assert not missing.unavailable and missing.can_fetch("*", "https://b.test/x") is True


@respx.mock
def test_get_as_browser_caps_the_body_and_keeps_status_url_and_history():
    big = b"x" * (MAX_PAGE_BYTES + 10)
    respx.get("https://big.test/").mock(return_value=httpx.Response(301, headers={"Location": "https://big.test/home"}))
    respx.get("https://big.test/home").mock(
        return_value=httpx.Response(200, content=big, headers={"content-type": "text/html; charset=utf-8"}))
    with httpx.Client(follow_redirects=True) as client:
        response = get_as_browser(client, "https://big.test/")
        assert response.status_code == 200 and str(response.url) == "https://big.test/home"
        assert [r.status_code for r in response.history] == [301]
        assert len(response.content) == MAX_PAGE_BYTES and response.extensions["truncated"] is True
        assert response.text.startswith("xxx") and "html" in response.headers["content-type"]
        small = get_as_browser(client, "https://big.test/home", max_bytes=10)
        assert small.content == b"x" * 10 and small.extensions["truncated"] is True
        assert respx.calls.last.request.headers["User-Agent"].startswith("Mozilla/5.0")
        whole = get_as_browser(client, "https://big.test/", max_bytes=MAX_PAGE_BYTES + 100)
        assert whole.extensions["truncated"] is False and len(whole.content) == MAX_PAGE_BYTES + 10


@respx.mock
def test_manual_redirect_following_treats_a_3xx_without_location_as_final():
    """手动跟跳转(第三方站要逐站查 robots)时,3xx 没带 Location 头当终态,与 httpx 自动跟随一致;
    原来硬取 headers["location"] 抛 KeyError,网页核实把它当单页崩溃(第三轮审核 A16)。"""
    respx.get("https://odd.test/").mock(return_value=httpx.Response(302, headers={"content-type": "text/html"}))
    with httpx.Client() as client:
        response, text, truncated = fetch_page(client, "https://odd.test/", may_visit=lambda url: True)
    assert response.status_code == 302 and text == "" and truncated is False


# ── 第二轮审核(2026-10-28)───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("encoding, compress", [
    ("gzip", __import__("gzip").compress),
    ("deflate", __import__("zlib").compress),
])
def test_compressed_responses_are_read_once_not_decompressed_twice(encoding, compress):
    """真实网站几乎都压缩回包:读的时候已经解压了,重建响应不能再带 Content-Encoding,否则二次解压直接报错——
    官网审计、官网名单、第三方 robots 一起失效(第二轮红 1)。"""
    body = "<html><body>" + "正文" * 50 + "</body></html>"
    robots = "User-agent: *\nDisallow: /private/\n"

    def handler(request: httpx.Request) -> httpx.Response:
        raw = robots if request.url.path == "/robots.txt" else body
        return httpx.Response(200, content=compress(raw.encode("utf-8")),
                              headers={"content-encoding": encoding, "content-type": "text/html; charset=utf-8"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = get_as_browser(client, "https://site.test/")
        assert response.status_code == 200 and response.text == body
        assert "content-encoding" not in response.headers
        rules = robots_parser(client, "https://site.test")
        assert not rules.unavailable and rules.can_fetch("*", "https://site.test/private/a") is False


@pytest.mark.parametrize("robots, token, path, allowed", [
    ("User-agent: *\nDisallow: /产品/\n", "AnyBot", "/%E4%BA%A7%E5%93%81/a", False),  # 规则原样中文、网址已编码
    ("User-agent: *\nDisallow: /%E4%BA%A7%E5%93%81/\n", "AnyBot", "/产品/a", False),  # 规则已编码、网址原样中文
    ("User-agent: *\nDisallow: /foo/%62%61%72\n", "AnyBot", "/foo/bar", False),  # 普通字符的 %xx 解开
    ("User-agent: *\nDisallow: /%c3%a9\n", "AnyBot", "/%C3%A9", False),  # 十六进制大小写统一
    ("User-agent: Claude\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "Claude-User", "/", True),  # 产品名相等才算
    ("User-agent: Claude\nDisallow: /\n", "claude", "/", False),
    ("User-agent: Applebot\nAllow: /\n\nUser-agent: Applebot-Extended\nDisallow: /\n", "Applebot-Extended", "/", False),
    ("User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /\n", "AnyBot", "/x", True),  # 合并后等长 Allow 赢
    ("User-agent: *\nAllow: /a\n\nUser-agent: *\nDisallow: /\n", "AnyBot", "/b", False),  # 两个 * 组合并
    ("User-agent: GPTBot\nDisallow: /x\n\nUser-agent: GPTBot\nDisallow: /y\n", "GPTBot", "/y", False),  # 同名组合并
    ("﻿User-agent: *\nDisallow: /\n", "AnyBot", "/", False),  # 开头 BOM 跳过
    ("User-agent:\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "AnyBot", "/", True),  # 空 User-agent 不对任何爬虫
    ("User-agent: Googlebot/2.1\nDisallow: /\n", "Googlebot", "/", False),  # 产品名取到 / 为止
])
def test_robots_rules_rfc_9309_second_round(robots, token, path, allowed):
    assert RobotsRules(robots).can_fetch(token, f"https://example.test{path}") is allowed


def test_robots_matching_is_not_exponential_on_hostile_patterns():
    """对方的 robots 是不可信输入:12 个 * 的规则配 40 字符路径,正则回溯要几十秒、卡死整个服务;线性匹配要毫秒级。"""
    import time

    rules = RobotsRules("User-agent: *\nDisallow: /" + "*a" * 12 + "b$\n")
    t0 = time.perf_counter()
    assert rules.can_fetch("*", "https://x.test/" + "a" * 40) is True
    assert time.perf_counter() - t0 < 0.5


@respx.mock
def test_unreadable_robots_means_fetch_nothing_but_missing_robots_means_no_rules():
    """RFC 9309 2.3.1:4xx = 没有规则、全放行;5xx 或网络错误 = 读不到、全禁(原来读不到也当没规则,对方一出错就照抓)。"""
    respx.get("https://down.test/robots.txt").mock(return_value=httpx.Response(503))
    respx.get("https://dead.test/robots.txt").mock(side_effect=httpx.ConnectError("x"))
    respx.get("https://none.test/robots.txt").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        for origin in ("https://down.test", "https://dead.test"):
            rules = robots_parser(client, origin)
            assert rules.unavailable and rules.can_fetch("*", f"{origin}/") is False
        missing = robots_parser(client, "https://none.test")
        assert not missing.unavailable and missing.can_fetch("*", "https://none.test/x") is True


def test_pages_without_a_charset_header_are_decoded_by_their_meta_tag():
    """响应头没写编码时看网页里的 <meta charset>:GBK 中文页不再乱码、不再被误判「没写到你」(第二轮)。"""
    from crawler.fetch import decode_page, fetch_page

    html = '<html><head><meta charset="gbk"></head><body>始祖鸟冲锋衣</body></html>'
    assert "始祖鸟" in decode_page(html.encode("gbk"), None)
    assert "始祖鸟" in decode_page(html.encode("utf-8"), "utf-8")  # 响应头写了就按它
    assert "始祖鸟" in decode_page(b"\xef\xbb\xbf" + "始祖鸟".encode(), None)  # UTF-8 BOM

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html.encode("gbk"), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert "始祖鸟" in get_as_browser(client, "https://cn.test/").text
        assert "始祖鸟" in fetch_page(client, "https://cn.test/")[1]


@respx.mock
def test_crawler_identity_probe_is_capped_too():
    from crawler.fetch import get_as

    respx.get("https://big.test/").mock(return_value=httpx.Response(200, content=b"x" * 50))
    with httpx.Client() as client:
        response = get_as(client, "https://big.test/", "TestBot/1.0", max_bytes=10)
        assert response.content == b"x" * 10 and response.extensions["truncated"] is True
        assert respx.calls.last.request.headers["User-Agent"] == "TestBot/1.0"


def test_unknown_encoding_names_do_not_crash_and_known_aliases_decode_properly():
    """第四轮审核:响应头或网页写了个 Python 不认识的编码名(日本老站的 windows-31j、泰国的 windows-874……),
    原来官网审计 / 官网名单 / 跳转还原一读文字就 LookupError,这类站整单崩。现在先查别名表,还不认识就当没写。"""
    from crawler.fetch import decode_page, page_encoding

    japanese = "日本語のページ".encode("cp932")
    for label in ("windows-31j", "x-sjis"):
        assert decode_page(japanese, label) == "日本語のページ", label
    assert page_encoding(b"", "windows-874") == "cp874" and page_encoding(b"", "iso-8859-8-i") == "iso-8859-8"
    assert page_encoding(b"", "no-such-encoding") == "utf-8"  # 完全不认识:当没写
    assert page_encoding(b'<meta charset="x-gbk">', "no-such-encoding") == "gbk"  # 头不认识、网页里写的认识
    assert page_encoding(b'<meta charset="made-up">', None) == "utf-8"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=japanese, headers={"content-type": "text/html; charset=windows-31j"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_as_browser(client, "https://jp.test/").text == "日本語のページ"  # 走 _capped_get 的那条路也不崩


def test_nul_characters_in_a_page_become_replacement_characters():
    """二进制内容被标成网页、标题里夹了个 0x00:带空字符的文字存不进数据库(第四轮审核)。"""
    from crawler.fetch import decode_page, page_title

    text = decode_page(b"<title>a\x00b</title>", None)
    assert "\x00" not in text and page_title(text) == "a\ufffdb"


def test_reading_a_body_stops_at_the_total_time_limit():
    """第四轮审核:httpx 的读超时管的是「每等一块数据」,对方隔几秒吐一点就永远不超时,核实网页的预算也打不断
    正在读的这一页。读正文另有总时限,到点按读超时算。"""
    from crawler.fetch import _read_capped

    ticks = iter(range(0, 1000, 4))  # 假时钟:每看一次过去 4 秒

    def drip():
        while True:
            yield b"x"

    slow = httpx.Response(200, stream=httpx.ByteStream(b""))
    slow.iter_bytes = lambda *args, **kwargs: drip()  # type: ignore[method-assign]
    with pytest.raises(httpx.ReadTimeout):
        _read_capped(slow, 1_000_000, total_s=10.0, clock=lambda: float(next(ticks)))
    quick = httpx.Response(200, content=b"hello")
    assert _read_capped(quick, 1_000_000, total_s=10.0) == (b"hello", False)


def test_page_intro_parsing_stays_fast_on_hostile_markup():
    """别人的网页是不可信输入:开标签重复几十万次、一直不闭合时,取标题 / H1 / 元信息原来随大小平方变慢
    (2 MB 要半小时以上,整个服务跟着卡);现在一步步往后找,几毫秒(第四轮审核)。"""
    import time

    from crawler.fetch import extract_meta_summary, extract_page_intro, page_title

    assert page_title('<head><TITLE lang="en">  Hello\n World </Title></head>') == "Hello World"
    assert page_title("<title>没有闭合") is None
    summary = extract_meta_summary('<title>T</title><meta content="简介在前" name="description">')
    assert summary == "T\n\n简介在前"
    started = time.perf_counter()
    for hostile in ("<title>" * 300_000, "<h1>" * 400_000, "<meta " * 300_000):
        assert extract_page_intro(hostile).empty
    assert time.perf_counter() - started < 5.0
