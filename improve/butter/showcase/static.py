"""把活的页面变成能离线打开的静态页(纯层)。设计 §12、ADR-B08。

展示页用的是和本地服务同一套模板。模板里的站内链接都以 / 开头(/runs/…、/registry),离线打开就断了,
所以导出时换成相对路径;展示页里没有的页面(设置、出题、下载),去掉链接、留下文字。
"""

from __future__ import annotations

import re

_LINK = re.compile(r'<a\s+href="(/[^"]*)"([^>]*)>(.*?)</a>', re.DOTALL)


def relink(html: str, here: str, pages: dict[str, str]) -> str:
    """here = 这一页在导出文件夹里的相对路径(如 example/run.html);pages = {站内地址: 导出后的相对路径}。"""
    up = "../" * here.count("/")

    def swap(match: re.Match[str]) -> str:
        target = pages.get(match.group(1))
        if target is None:
            return match.group(3)
        return f'<a href="{up}{target}"{match.group(2)}>{match.group(3)}</a>'

    return _LINK.sub(swap, html)
