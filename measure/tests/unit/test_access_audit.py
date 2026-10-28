"""ACCESS 审计单元测试。12.1:respx 模拟全部 HTTP,零真实网络请求。

爬虫名单用虚构 bot 注入,证明模块对名单完全泛型(名单是配置资产)。
"""

import httpx
import respx

from diagnosis.access_audit import (
    DEFAULT_CRAWLERS_PATH,
    AccessLayer,
    CheckStatus,
    CrawlerBot,
    PathProbe,
    audit,
    load_crawlers,
    visible_text_length,
    with_path_probes,
)
from diagnosis.messages import render_msg

SITE = "https://example.com"

BOTS = [
    CrawlerBot(
        name="TestSearchBot",
        robots_token="TestSearchBot",
        user_agent="Mozilla/5.0 (compatible; TestSearchBot/1.0)",
        purpose="search_index",
        gating=True,
    ),
    CrawlerBot(
        name="TestTrainBot",
        robots_token="TestTrainBot",
        user_agent=None,  # 仅 robots 令牌
        purpose="training",
        gating=False,
    ),
]

RICH_HTML = (
    "<html><head><title>产品站</title><script>var x=1;</script></head><body><main>"
    + "这是一段真实存在于 HTML 中的产品介绍文字。" * 30
    + "</main></body></html>"
)
SHELL_HTML = (
    '<html><head><script src="/app.js"></script><style>body{}</style></head>'
    '<body><div id="root"></div></body></html>'
)
ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"


def check_by_name(result, name):
    return next(c for c in result.checks if c.check == name)


def mock_site(*, html=RICH_HTML, robots=ROBOTS_ALLOW_ALL, robots_status=200):
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(robots_status, text=robots)
    )
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=html))


@respx.mock
def test_healthy_site_passes():
    mock_site(robots=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/sitemap.xml\n")
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.PASS
    assert all(c.status is CheckStatus.PASS for c in result.checks if c.gating)
    assert check_by_name(result, "sitemap").status is CheckStatus.PASS


@respx.mock
def test_robots_disallow_of_search_bot_fails_gate():
    mock_site(robots="User-agent: TestSearchBot\nDisallow: /\n")
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.FAIL
    robots_check = check_by_name(result, "robots_txt")
    assert robots_check.status is CheckStatus.FAIL
    assert "TestSearchBot" in render_msg(robots_check.detail, "zh")


@respx.mock
def test_robots_disallow_of_training_bot_only_is_informational():
    mock_site(robots="User-agent: TestTrainBot\nDisallow: /\n" + f"Sitemap: {SITE}/s.xml\n")
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.PASS
    robots_check = check_by_name(result, "robots_txt")
    assert robots_check.status is CheckStatus.PASS
    assert "TestTrainBot" in render_msg(robots_check.detail, "zh")


@respx.mock
def test_js_shell_fails_gate():
    mock_site(html=SHELL_HTML, robots=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n")
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.FAIL
    assert check_by_name(result, "html_content").status is CheckStatus.FAIL


@respx.mock
def test_bot_ua_blocked_yields_unknown_not_fail():
    """伪装 UA 被拒无法区分「封 UA」与「仅放行官方 IP」,只能 UNKNOWN(铁律 7)。"""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n")
    )
    respx.get(f"{SITE}/", headers={"User-Agent": BOTS[0].user_agent}).mock(
        return_value=httpx.Response(403, text="blocked")
    )
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=RICH_HTML))
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.UNKNOWN
    ua_check = check_by_name(result, "ua_differential")
    assert ua_check.status is CheckStatus.UNKNOWN
    assert "TestSearchBot" in render_msg(ua_check.detail, "zh")
    assert not any(c.status is CheckStatus.FAIL for c in result.checks if c.gating)


@respx.mock
def test_missing_robots_and_sitemap_still_passes_gate():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=RICH_HTML))
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.PASS
    assert check_by_name(result, "robots_txt").status is CheckStatus.PASS
    sitemap_check = check_by_name(result, "sitemap")
    assert sitemap_check.status is CheckStatus.FAIL and not sitemap_check.gating


@respx.mock
def test_bot_protection_403_yields_unknown_not_fail():
    """香奈儿首测复盘(铁律 7):带浏览器 UA 仍被 403 = 疑似机器人防护拦截探针,
    真人与官方爬虫可能正常——不可判定为「全站不可达」,只能 UNKNOWN。"""
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL))
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(403, text="Access Denied"))
    result = audit(SITE, BOTS)
    reach = check_by_name(result, "reachability")
    assert reach.status is CheckStatus.UNKNOWN
    assert "机器人防护" in render_msg(reach.detail, "zh")
    assert result.overall is CheckStatus.UNKNOWN  # 资格门不可观测,不判死
    assert not any(c.status is CheckStatus.FAIL for c in result.checks if c.gating)


@respx.mock
def test_layers_assigned_and_unobservable_layers_placeholded():
    """v0.4.13 条目 6:每条检查带证据层;v1 不可观测的三层显式 UNKNOWN 占位且不进资格门。"""
    mock_site()
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    result = audit(SITE, BOTS)
    assert all(c.layer in AccessLayer for c in result.checks)
    assert {c.check for c in result.checks_in(AccessLayer.POLICY)} == {"robots_txt", "sitemap"}
    assert {c.check for c in result.checks_in(AccessLayer.GENERIC_FETCH)} >= {
        "reachability", "redirects", "ua_differential", "html_content"}
    for layer, name in [(AccessLayer.RENDERED_FETCH, "rendered_fetch"),
                        (AccessLayer.OFFICIAL_IDENTITY, "official_identity"),
                        (AccessLayer.WAF_EVIDENCE, "waf_evidence")]:
        (check,) = result.checks_in(layer)
        assert check.check == name and check.status is CheckStatus.UNKNOWN and not check.gating
    assert result.overall is CheckStatus.PASS  # 占位层不进资格门
    assert "站点侧" in render_msg(check.detail, "zh") or "不可观测" in render_msg(check.detail, "zh")


@respx.mock
def test_path_probes_join_generic_fetch_without_touching_gate():
    """按路径探针(引用核验重抓)并入 GENERIC_FETCH:机器人防护按路径拦截时,首页探针不代表全站。"""
    mock_site()
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    result = audit(SITE, BOTS)
    assert with_path_probes(result, []) is result
    mixed = with_path_probes(result, [
        PathProbe(url=f"{SITE}/a", http_status=200),
        PathProbe(url=f"{SITE}/b", http_status=403),
        PathProbe(url=f"{SITE}/c", error="ConnectError"),
    ])
    path = check_by_name(mixed, "path_fetch")
    assert path.layer is AccessLayer.GENERIC_FETCH and not path.gating
    assert path.status is CheckStatus.UNKNOWN and path.evidence == {"ok": 1, "blocked": 1, "other": 1}
    assert "1 条疑似机器人防护" in render_msg(path.detail, "zh")
    assert mixed.overall is result.overall and len(mixed.path_probes) == 3
    clean = with_path_probes(result, [PathProbe(url=f"{SITE}/a", http_status=200)])
    assert check_by_name(clean, "path_fetch").status is CheckStatus.PASS


@respx.mock
def test_network_error_yields_unknown():
    respx.route(host="example.com").mock(side_effect=httpx.ConnectError("no route"))
    result = audit(SITE, BOTS)
    assert result.overall is CheckStatus.UNKNOWN
    assert check_by_name(result, "reachability").status is CheckStatus.UNKNOWN


@respx.mock
def test_bot_probe_records_robots_and_fetch_facts():
    mock_site(robots=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n")
    result = audit(SITE, BOTS)
    search_bot = next(p for p in result.bots if p.bot == "TestSearchBot")
    assert search_bot.robots_allowed is True
    assert search_bot.fetch_status == 200
    assert search_bot.visible_text_chars > 200
    train_bot = next(p for p in result.bots if p.bot == "TestTrainBot")
    assert train_bot.fetch_status is None  # 仅 robots 令牌,不发请求


def test_visible_text_length_distinguishes_shell_from_content():
    assert visible_text_length(SHELL_HTML) < 50
    assert visible_text_length(RICH_HTML) > 300


def test_bundled_crawler_config_loads():
    bots = load_crawlers(DEFAULT_CRAWLERS_PATH)
    assert len(bots) >= 5
    assert any(b.gating for b in bots)
    fetching = [b for b in bots if b.user_agent]
    assert all("compatible" in b.user_agent for b in fetching)


@respx.mock
def test_bot_shell_with_rich_browser_view_is_unknown_not_fail():
    """浏览器视角有正文、冒充的爬虫 UA 拿到 200 空壳:分不清拦真爬虫还是拦假身份,只能 UNKNOWN、不判死
    (铁律 7;审核 2026-10-27 黄 11)。两边都是空壳仍是 JS 白板 FAIL(见上面的测试)。"""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n")
    )
    respx.get(f"{SITE}/", headers={"User-Agent": BOTS[0].user_agent}).mock(
        return_value=httpx.Response(200, html=SHELL_HTML)
    )
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=RICH_HTML))
    result = audit(SITE, BOTS)
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and result.overall is not CheckStatus.FAIL
    assert "看不到" in render_msg(content.detail, "zh") and "not observable" in render_msg(content.detail, "en")
    assert not any(c.status is CheckStatus.FAIL for c in result.checks if c.gating)


@respx.mock
def test_robots_wildcard_disallow_is_respected():
    """「Disallow: /*」是全站禁抓:标准库会当成放行,现在按 RFC 9309 判(审核 2026-10-27 黄 12)。"""
    mock_site(robots="User-agent: TestSearchBot\nDisallow: /*\n")
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))  # 没声明 sitemap 就探默认位置
    result = audit(SITE, BOTS)
    assert check_by_name(result, "robots_txt").status is CheckStatus.FAIL
