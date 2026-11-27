"""UA 表、「未纳入厂商」表、平台表(data/ 下的配置数据)。"""

from __future__ import annotations

from typing import Literal

from butter.contracts.common import Frozen

UaPurpose = Literal["search_index", "training", "user_fetch", "ai_uses_token"]


class UaEntry(Frozen):
    vendor: str
    name: str
    robots_token: str
    user_agent: str | None = None  # 官方文档给出的 UA 字符串;没给就是 None
    # verbatim 当日整段照抄 / stitched 分段取回后拼接、未逐字对照 / short_form 只确认了不带括号的短串 /
    # not_given 文档没给完整串 / unverified 出自旧文档、是否现行未核
    user_agent_checked: Literal["verbatim", "stitched", "short_form", "not_given", "unverified"]
    purpose: UaPurpose
    robots_stance: str  # 官方对「是否遵守 robots.txt」的说法(转述)
    docs_url: str
    checked_on: str  # YYYY-MM-DD
    checked_how: str  # 当日打开 / 沿用哪次调研
    note: str = ""


class ExcludedVendor(Frozen):
    vendor: str
    products: str = ""
    reason: str
    looked_at: str = ""  # 查过哪些地方
    checked_on: str
    source: str  # 依据哪份调研记录


class Platform(Frozen):
    name: str
    kind: str
    hosts: list[str]
