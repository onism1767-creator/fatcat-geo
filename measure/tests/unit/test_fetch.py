"""crawler/fetch 公共部件:robots.txt 按 RFC 9309 的口径判(通配符、结尾 $、最长匹配、等长 Allow 赢)——标准库不认通配符
(审核 2026-10-27 黄 12);以浏览器身份整页取回有体积上限(黄 20)。respx 拦截,零真实网络。"""

from __future__ import annotations

import httpx
import pytest
import respx

from crawler.fetch import MAX_PAGE_BYTES, RobotsRules, get_as_browser, robots_parser


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
def test_robots_parser_only_parses_a_200():
    respx.get("https://a.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /*\n"))
    respx.get("https://b.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://c.test/robots.txt").mock(side_effect=httpx.ConnectError("down"))
    with httpx.Client() as client:
        rules = robots_parser(client, "https://a.test")
        assert rules is not None and rules.can_fetch("*", "https://a.test/") is False
        assert robots_parser(client, "https://b.test") is None and robots_parser(client, "https://c.test") is None


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
