"""Reddit 官方接口(S6.3 可选增强):应用级 OAuth 拿 token,读帖子标题 + 正文 + 评论;没配凭证就不构造;失败如实返回 None。

respx 模拟全部请求,零真实网络。
"""

import httpx
import respx

from crawler.reddit_api import API_BASE, TOKEN_URL, RedditFetcher, post_id_of

THREAD = [
    {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {
        "title": "Which CRM for a 5-person team?", "selftext": "We tried AuroraCRM and HubSpot.",
        "permalink": "/r/smallbusiness/comments/abc123/which_crm/",
    }}]}},
    {"kind": "Listing", "data": {"children": [
        {"kind": "t1", "data": {"body": "AuroraCRM worked for us.", "replies": {
            "kind": "Listing", "data": {"children": [
                {"kind": "t1", "data": {"body": "Same here, cheap tier is enough.", "replies": ""}},
            ]},
        }}},
        {"kind": "more", "data": {"children": ["x"]}},
        {"kind": "t1", "data": {"body": "   ", "replies": ""}},
    ]}},
]


def test_post_id_parsing_covers_the_usual_link_shapes():
    assert post_id_of("https://www.reddit.com/r/smallbusiness/comments/abc123/which_crm/") == "abc123"
    assert post_id_of("https://reddit.com/comments/abc123") == "abc123"
    assert post_id_of("https://redd.it/abc123") == "abc123"
    assert post_id_of("https://www.reddit.com/r/smallbusiness/") is None
    assert post_id_of("https://example.com/comments/abc123") is None


def test_from_env_requires_both_credentials():
    assert RedditFetcher.from_env({}) is None
    assert RedditFetcher.from_env({"REDDIT_CLIENT_ID": "id"}) is None
    fetcher = RedditFetcher.from_env(
        {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s", "REDDIT_USER_AGENT": "me/1"})
    assert fetcher is not None and fetcher.matches("https://redd.it/abc123") and not fetcher.matches("https://x.test/")
    fetcher.close()


@respx.mock
def test_fetch_reads_title_body_and_comments_with_one_token():
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
    thread = respx.get(f"{API_BASE}/comments/abc123").mock(return_value=httpx.Response(200, json=THREAD))
    fetcher = RedditFetcher("id", "secret", user_agent="geo-toast-test/1")
    try:
        page = fetcher.fetch("https://www.reddit.com/r/smallbusiness/comments/abc123/which_crm/")
        again = fetcher.fetch("https://redd.it/abc123")
    finally:
        fetcher.close()
    assert page is not None and page.title == "Which CRM for a 5-person team?"
    assert page.final_url == "https://www.reddit.com/r/smallbusiness/comments/abc123/which_crm/"
    assert "We tried AuroraCRM and HubSpot." in page.text and "Same here, cheap tier is enough." in page.text
    assert page.text.count("AuroraCRM") == 2  # 正文一次 + 评论一次;空评论与 more 占位不算
    assert token.call_count == 1 and thread.call_count == 2  # token 复用
    assert thread.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert thread.calls[0].request.headers["User-Agent"] == "geo-toast-test/1"
    assert again is not None


@respx.mock
def test_failures_return_none_so_the_caller_falls_back():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
    respx.get(f"{API_BASE}/comments/gone").mock(return_value=httpx.Response(404, json={"error": 404}))
    respx.get(f"{API_BASE}/comments/odd").mock(return_value=httpx.Response(200, json={"unexpected": True}))
    respx.get(f"{API_BASE}/comments/down").mock(side_effect=httpx.ConnectError("no route"))
    fetcher = RedditFetcher("id", "secret")
    try:
        assert fetcher.fetch("https://redd.it/gone") is None
        assert fetcher.fetch("https://redd.it/odd") is None
        assert fetcher.fetch("https://redd.it/down") is None
        assert fetcher.fetch("https://example.com/not-reddit") is None
    finally:
        fetcher.close()
