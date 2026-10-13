"""Reddit 官方数据接口(v0.4.26 S6.3,可选增强):用户自己注册的 script 应用凭证,应用级 OAuth(client_credentials),
读被 AI 引用的帖子:标题、正文、评论。没配凭证就不构造(from_env 返回 None),调用方退回普通抓取 + 嵌入接口(只有标题)。

环境变量(放 .env,不入库):REDDIT_CLIENT_ID、REDDIT_CLIENT_SECRET;REDDIT_USER_AGENT 可选(Reddit 要求说明用途)。
用途只是读公开帖子核对品牌名,不存用户数据、不训练模型;商业化前要按 Reddit 数据接口条款另行申请。
只做取回,不做判定;零项目内依赖。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import structlog

from crawler.platform import PlatformPage

log = structlog.get_logger("crawler.reddit_api")

NAME: Final = "reddit"
TOKEN_URL: Final = "https://www.reddit.com/api/v1/access_token"
API_BASE: Final = "https://oauth.reddit.com"
DEFAULT_USER_AGENT: Final = "geo-toast/0.1 (checks whether cited public posts mention a brand)"
MAX_COMMENTS: Final = 100
_HOSTS: Final = ("reddit.com", "redd.it")
_POST_ID = re.compile(r"/comments/([a-z0-9]+)")
_SHORT_ID = re.compile(r"^/([a-z0-9]+)/?$")


def post_id_of(url: str) -> str | None:
    """帖子 ID:reddit.com/r/x/comments/<id>/...、reddit.com/comments/<id>、redd.it/<id>;认不出 → None。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "redd.it" or host.endswith(".redd.it"):
        match = _SHORT_ID.match(parts.path)
        return match.group(1) if match else None
    if host == "reddit.com" or host.endswith(".reddit.com"):
        match = _POST_ID.search(parts.path)
        return match.group(1) if match else None
    return None


class RedditFetcher:
    name = NAME

    def __init__(self, client_id: str, client_secret: str, *, user_agent: str = DEFAULT_USER_AGENT,
                 client: httpx.Client | None = None, timeout_s: float = 10.0) -> None:
        self._auth = (client_id, client_secret)
        self._user_agent = user_agent
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)
        self._token: str | None = None
        self._token_expires_at = 0.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RedditFetcher | None:
        client_id, secret = env.get("REDDIT_CLIENT_ID", "").strip(), env.get("REDDIT_CLIENT_SECRET", "").strip()
        if not client_id or not secret:
            return None
        return cls(client_id, secret, user_agent=env.get("REDDIT_USER_AGENT", "").strip() or DEFAULT_USER_AGENT)

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def matches(self, url: str) -> bool:
        return post_id_of(url) is not None

    def fetch(self, url: str) -> PlatformPage | None:
        post_id = post_id_of(url)
        if post_id is None:
            return None
        try:
            token = self._access_token()
            response = self._client.get(
                f"{API_BASE}/comments/{post_id}", params={"limit": MAX_COMMENTS, "depth": 2, "raw_json": 1},
                headers={"Authorization": f"Bearer {token}", "User-Agent": self._user_agent},
            )
        except httpx.HTTPError as exc:
            log.warning("reddit_api_failed", url=url, error=type(exc).__name__)
            return None
        if response.status_code != 200:
            log.warning("reddit_api_status", url=url, status=response.status_code)
            return None
        try:
            post, comments = _parse_thread(response.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("reddit_api_shape", url=url, error=type(exc).__name__)
            return None
        title = post.get("title") or None
        body = post.get("selftext") or ""
        text = "\n\n".join(part for part in [title or "", body, *comments] if part)
        return PlatformPage(title=title, text=text, final_url=f"https://www.reddit.com{post.get('permalink', '')}",
                            http_status=200)

    def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expires_at - 30:
            return self._token
        response = self._client.post(
            TOKEN_URL, auth=self._auth, data={"grant_type": "client_credentials"},
            headers={"User-Agent": self._user_agent},
        )
        response.raise_for_status()
        data = response.json()
        self._token = str(data["access_token"])
        self._token_expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token


def _parse_thread(data: Any) -> tuple[dict[str, Any], list[str]]:
    """Reddit 的帖子接口返回两段 listing:[帖子, 评论树]。评论只取正文,最多 MAX_COMMENTS 条,深度随接口给的。"""
    post = data[0]["data"]["children"][0]["data"]
    comments: list[str] = []

    def walk(listing: Any) -> None:
        if not isinstance(listing, dict):
            return
        for child in listing.get("data", {}).get("children", []):
            if len(comments) >= MAX_COMMENTS:
                return
            if child.get("kind") != "t1":
                continue
            body = child.get("data", {}).get("body")
            if isinstance(body, str) and body.strip():
                comments.append(body.strip())
            walk(child.get("data", {}).get("replies"))

    walk(data[1])
    return post, comments
