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
def test_js_shell_without_a_real_browser_is_unconfirmed_not_fail():
    """各个原始视角都是空壳、本机又没有真浏览器对照:分不清被拦还是靠脚本加载,记看不到;空壳框架标记只作佐证
    (用户 2026-10-29 定。原来这里直接判挂)。"""
    mock_site(html=SHELL_HTML, robots=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n")
    result = audit(SITE, BOTS)
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and result.overall is CheckStatus.UNKNOWN
    zh = render_msg(content.detail, "zh")
    assert "没装真浏览器内核" in zh and "React" in zh and "分不清" in zh
    assert check_by_name(result, "rendered_fetch").status is CheckStatus.UNKNOWN


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
    assert path.status is CheckStatus.UNKNOWN
    assert path.evidence == {"ok": 1, "blocked": 1, "other": 1, "with_text": 0}
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
    (铁律 7;审核 2026-10-27 黄 11)。两边都是空壳时要真浏览器对照才判(见下面的测试)。"""
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


# ── 各个原始视角都只拿到很少的字时怎么分清(用户 2026-10-29 定:指纹 + 真浏览器对照 + 本次数据核对) ──

class FakeRenderer:
    """真浏览器的替身:给什么就交回什么,记下被叫了几次。"""

    def __init__(self, page):
        self.page, self.calls = page, []

    def render(self, url):
        self.calls.append(url)
        return self.page

    def close(self):
        pass


CHALLENGE_HTML = "<html><head><title>Just a moment...</title></head><body>checking</body></html>"


def _page(*, html, document=SHELL_HTML, status=200):
    from crawler.render import RenderedPage

    return RenderedPage(html=html, final_url=f"{SITE}/", http_status=status, document_html=document)


def _shell_site():
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n"))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=SHELL_HTML))


@respx.mock
def test_real_browser_confirms_script_only_content_and_only_then_it_fails():
    """原始网页空(真浏览器收到的原始网页也空)、运行脚本后有内容:确认「内容全靠脚本加载」,判挂了。"""
    _shell_site()
    renderer = FakeRenderer(_page(html=RICH_HTML))
    result = audit(SITE, BOTS, renderer=renderer)
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.FAIL and result.overall is CheckStatus.FAIL and renderer.calls == [SITE]
    zh, en = render_msg(content.detail, "zh"), render_msg(content.detail, "en")
    assert "大部分内容靠脚本加载" in zh and "React" in zh and "loaded by scripts" in en
    assert check_by_name(result, "rendered_fetch").status is CheckStatus.PASS


@respx.mock
def test_real_browser_gets_content_in_the_raw_page_so_we_were_served_differently():
    """真浏览器收到的原始网页本来就有正文:是网站对我们的检测身份给了不同页面,记看不到,不判挂。"""
    _shell_site()
    result = audit(SITE, BOTS, renderer=FakeRenderer(_page(html=RICH_HTML, document=RICH_HTML)))
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and "不同身份给了不同页面" in render_msg(content.detail, "zh")


@respx.mock
def test_real_browser_blocked_too_means_protection():
    _shell_site()
    for page in (_page(html=CHALLENGE_HTML, document=CHALLENGE_HTML), _page(html="<html></html>", status=403)):
        result = audit(SITE, BOTS, renderer=FakeRenderer(page))
        content = check_by_name(result, "html_content")
        assert content.status is CheckStatus.UNKNOWN and "安全系统拦了" in render_msg(content.detail, "zh")
        assert check_by_name(result, "rendered_fetch").status is CheckStatus.UNKNOWN


@respx.mock
def test_few_words_even_in_a_real_browser_or_no_browser_at_all_is_unconfirmed():
    _shell_site()
    thin = audit(SITE, BOTS, renderer=FakeRenderer(_page(html=SHELL_HTML)))
    assert check_by_name(thin, "html_content").status is CheckStatus.UNKNOWN
    assert "加载得慢" in render_msg(check_by_name(thin, "html_content").detail, "zh")
    failed = audit(SITE, BOTS, renderer=FakeRenderer(None))
    assert "没能打开首页" in render_msg(check_by_name(failed, "html_content").detail, "zh")


@respx.mock
def test_a_protection_page_is_recognised_by_its_marks_without_opening_a_browser():
    """冒充的爬虫拿到防护公司的验证页(只在拦截时出现的响应头):直接记「被防护拦了」,不用再开真浏览器。"""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n"))
    respx.get(f"{SITE}/", headers={"User-Agent": BOTS[0].user_agent}).mock(
        return_value=httpx.Response(200, html=CHALLENGE_HTML, headers={"cf-mitigated": "challenge"}))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(403, html=CHALLENGE_HTML))
    renderer = FakeRenderer(_page(html=RICH_HTML))
    result = audit(SITE, BOTS, renderer=renderer)
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and renderer.calls == []
    assert "Cloudflare" in render_msg(content.detail, "zh")
    assert "only the site's own logs" in render_msg(content.detail, "en")
    probe = next(b for b in result.bots if b.bot == "TestSearchBot")
    assert probe.protection == ["Cloudflare"]


def _refused_site(html="Access Denied"):
    """浏览器身份和各个爬虫身份都被拒(403)——最常见的「被拦」那条路(第三轮审核 A13)。"""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n"))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(403, html=html))


@respx.mock
def test_everyone_refused_with_protection_marks_says_blocked_without_opening_a_browser():
    """各个身份都被拒、拒绝页带防护公司的标记:原来直接记「没拿到可分析的网页」、指纹被扔了;现在说像是被防护拦了。"""
    _refused_site(CHALLENGE_HTML)
    renderer = FakeRenderer(_page(html=RICH_HTML))
    result = audit(SITE, BOTS, renderer=renderer)
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and renderer.calls == []
    zh, en = render_msg(content.detail, "zh"), render_msg(content.detail, "en")
    assert "都没拿到能读的首页" in zh and "Cloudflare" in zh and "安全系统拦了" in zh
    assert "Neither the browser identity nor any crawler identity" in en
    assert content.evidence["protection"] == ["Cloudflare"]
    assert check_by_name(result, "rendered_fetch").status is CheckStatus.UNKNOWN


@respx.mock
def test_everyone_refused_without_marks_opens_a_real_browser_once():
    """拒绝页认不出是谁家的:开一次真浏览器对照。也被拦 → 像是被拦;看得到 → 把看到的写进说明。一律看不到、不判挂。"""
    _refused_site()
    blocked = audit(SITE, BOTS, renderer=FakeRenderer(_page(html="<html></html>", status=403)))
    content = check_by_name(blocked, "html_content")
    assert content.status is CheckStatus.UNKNOWN
    assert "都没拿到能读的首页" in render_msg(content.detail, "zh") and "HTTP 403" in render_msg(content.detail, "zh")
    assert "也被拦了" in render_msg(check_by_name(blocked, "rendered_fetch").detail, "zh")

    renderer = FakeRenderer(_page(html=RICH_HTML))
    seen = audit(SITE, BOTS, renderer=renderer)
    assert renderer.calls == [SITE]
    assert check_by_name(seen, "html_content").status is CheckStatus.UNKNOWN
    rendered = check_by_name(seen, "rendered_fetch")
    assert rendered.status is CheckStatus.PASS and "真浏览器打开首页" in render_msg(rendered.detail, "zh")
    assert not any(c.status is CheckStatus.FAIL for c in seen.checks if c.gating)


@respx.mock
def test_everyone_refused_and_no_usable_browser_is_still_not_observable():
    _refused_site()
    no_browser = audit(SITE, BOTS)
    assert check_by_name(no_browser, "html_content").status is CheckStatus.UNKNOWN
    assert "没装真浏览器内核" in render_msg(check_by_name(no_browser, "rendered_fetch").detail, "zh")
    failed = audit(SITE, BOTS, renderer=FakeRenderer(None))
    assert "没能打开首页" in render_msg(check_by_name(failed, "rendered_fetch").detail, "zh")


@respx.mock
def test_cited_site_pages_are_added_as_a_cross_check_to_an_unresolved_homepage():
    """采样后 AI 引用的官网页面我们打开时读不读得到正文、有没有被拦,补进首页那条的说明(只补说明,不改结论)。"""
    _shell_site()
    result = audit(SITE, BOTS)
    merged = with_path_probes(result, [
        PathProbe(url=f"{SITE}/a", http_status=200, text_chars=900),
        PathProbe(url=f"{SITE}/b", http_status=403),
    ])
    content = check_by_name(merged, "html_content")
    assert content.status is CheckStatus.UNKNOWN
    zh = render_msg(content.detail, "zh")
    assert "AI 引用过的 2 个官网页面" in zh and "1 个有完整正文" in zh and "1 个被拦" in zh
    assert "of 2 site page(s) the AI cited" in render_msg(content.detail, "en")


@respx.mock
def test_a_challenge_page_with_an_unusual_status_is_not_reported_as_the_site_being_down():
    """第四轮审核:首页被验证码 / 防护页以 401 / 403 / 429 / 503 之外的状态码拒绝(比如 405),原来只按状态码分,
    判成「网站对普通访问者也打不开」、整单卡在第一步——把我们自己被拦说成了网站的故障(产品文档原则 4)。
    认得出是防护页就记看不到;认不出的非 2xx 照旧判打不开。"""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n"))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(
        405, headers={"x-amzn-waf-action": "captcha"}, html="<html><script src='https://x.awswaf.com/c.js'></script>"))
    blocked = audit(SITE, BOTS)
    reach = check_by_name(blocked, "reachability")
    assert reach.status is CheckStatus.UNKNOWN and blocked.overall is not CheckStatus.FAIL
    assert "405" in render_msg(reach.detail, "zh")

    respx.get(f"{SITE}/").mock(return_value=httpx.Response(410, html="<html><body>Gone</body></html>"))
    gone = audit(SITE, BOTS)
    assert check_by_name(gone, "reachability").status is CheckStatus.FAIL and gone.overall is CheckStatus.FAIL


@respx.mock
def test_marks_on_a_refused_identity_are_not_pinned_on_the_page_we_did_read():
    """第四轮审核:首页是真·脚本空壳,浏览器身份和搜索类爬虫身份都读到了(200 空壳),只有另一个身份被防护 403。
    原来把被拒身份身上的防护标记算到「我们拿到的这张首页」头上:说成「被拦了」,真浏览器对照也跳过,
    本该查出的「内容靠脚本加载」漏掉。现在只认读到的页面自己的标记。"""
    blocked_bot = CrawlerBot(name="TestOtherBot", robots_token="TestOtherBot",
                             user_agent="Mozilla/5.0 (compatible; TestOtherBot/1.0)", purpose="training", gating=False)

    def home(request: httpx.Request) -> httpx.Response:
        if "TestOtherBot" in request.headers["user-agent"]:
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, html=CHALLENGE_HTML)
        return httpx.Response(200, html=SHELL_HTML)

    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL + f"Sitemap: {SITE}/s.xml\n"))
    respx.get(f"{SITE}/").mock(side_effect=home)
    renderer = FakeRenderer(_page(html=RICH_HTML))
    result = audit(SITE, [*BOTS, blocked_bot], renderer=renderer)
    content = check_by_name(result, "html_content")
    assert renderer.calls == [SITE]  # 开了真浏览器对照
    assert content.status is CheckStatus.FAIL and "大部分内容靠脚本加载" in render_msg(content.detail, "zh")


@respx.mock
def test_script_only_is_not_concluded_when_the_real_browser_hands_back_no_raw_page():
    """第四轮审核:判「内容靠脚本加载」的条件是「真浏览器收到的原始网页也空、运行脚本后才有内容」。真浏览器没交回
    原始网页时(跳转后的主文档有时读不出),原始网页根本没看到——分不清是不是网站给不同身份不同页面,不判挂。"""
    _shell_site()
    result = audit(SITE, BOTS, renderer=FakeRenderer(_page(html=RICH_HTML, document=None)))
    content = check_by_name(result, "html_content")
    assert content.status is CheckStatus.UNKNOWN and result.overall is not CheckStatus.FAIL
    zh, en = render_msg(content.detail, "zh"), render_msg(content.detail, "en")
    assert "没交回它收到的原始网页" in zh and "did not hand back the raw page" in en
