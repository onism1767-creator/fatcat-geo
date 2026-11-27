"""平台官方接口的公共形状(v0.4.26 S6.3):平台自己开的门——拿帖子 / 视频的标题与正文,不要绕过任何东西。

一个平台一个模块(如 crawler/reddit_api.py),实现 PlatformFetcher;没配 key 的平台不构造,调用方退回普通抓取。
只做取回,不做判定;零项目内依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PlatformPage:
    title: str | None
    text: str  # 标题 + 正文(+ 评论),供正文判定;空字串 = 接口说没有内容
    final_url: str
    http_status: int | None = None


class PlatformFetcher(Protocol):
    name: str  # 进 text_source:api:<name>

    def matches(self, url: str) -> bool: ...
    def fetch(self, url: str) -> PlatformPage | None: ...
    def close(self) -> None: ...
