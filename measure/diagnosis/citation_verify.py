"""引用独立核验的汇总:三档证据「提及 → 被引 → 已核验」的第三档(v0.4.2 附录条目 1;S6.1 起抓取与判定都在 L2)。

不轻信平台引用:被引的官网 URL 由 L2 自行重抓(engine/page_check.py),「正文里有没有品牌清晰名」由
engine/detectors/page_mention.py 判;本模块只把结果汇成四态计数(v0.4.13 条目 5):
抓不到 ≠ 引用无效——被拦截(FETCH_BLOCKED)、非 HTML 或抽不出正文(UNPARSED)、其余失败(UNREACHABLE)
都只是核验器缺数据,不得解读为平台引用错标;唯一的负面证据是页面可达、正文可读却没有品牌(CONTENT_MISMATCH)。
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from diagnosis.citation_diag import VerificationSummary
from engine.detectors.page_mention import page_mention
from engine.models import BrandSpec, PageCheck, PageFetchStatus
from engine.page_check import PageIndex, normalize_url


class VerificationOutcome(StrEnum):
    """一条被引 URL 对某品牌的核验结果。只有 CONTENT_MISMATCH 是负面证据;
    FETCH_BLOCKED / UNPARSED / UNREACHABLE 是核验器缺数据(不可观测),不进任何负面口径。"""

    VERIFIED_MATCH = "VERIFIED_MATCH"      # 正文可读且出现品牌清晰名
    CONTENT_MISMATCH = "CONTENT_MISMATCH"  # 正文可读、没见品牌——软 404 / 错标指纹
    FETCH_BLOCKED = "FETCH_BLOCKED"        # 401/403/429/503:疑似机器人防护拦截本工具客户端
    UNPARSED = "UNPARSED"                  # 可达但非 HTML,或抽不出正文(脚本白板)
    UNREACHABLE = "UNREACHABLE"            # 其余非成功状态或网络层失败(404、超时、DNS…)


def classify(check: PageCheck, brand: BrandSpec) -> VerificationOutcome:
    if check.status is PageFetchStatus.OK:
        mention = page_mention(check, brand)
        return (VerificationOutcome.VERIFIED_MATCH if mention is not None and mention.found
                else VerificationOutcome.CONTENT_MISMATCH)
    if check.status in (PageFetchStatus.BLOCKED, PageFetchStatus.LOGIN_REQUIRED):
        return VerificationOutcome.FETCH_BLOCKED
    if check.status in (PageFetchStatus.NOT_HTML, PageFetchStatus.EMPTY_TEXT):
        return VerificationOutcome.UNPARSED
    return VerificationOutcome.UNREACHABLE


def summarize_verification(urls: list[str], pages: PageIndex, brand: BrandSpec) -> VerificationSummary:
    """一个被测对象被引的品牌 URL(按规范化去重)在核验结果里的四态计数;没开到的不计入 attempted。"""
    outcomes = Counter(
        classify(check, brand)
        for normalized in {normalize_url(u) for u in urls}
        if (check := pages.checks.get(normalized)) is not None
    )
    return VerificationSummary(
        attempted=sum(outcomes.values()),
        verified_match=outcomes[VerificationOutcome.VERIFIED_MATCH],
        content_mismatch=outcomes[VerificationOutcome.CONTENT_MISMATCH],
        fetch_blocked=outcomes[VerificationOutcome.FETCH_BLOCKED],
        unparsed=outcomes[VerificationOutcome.UNPARSED],
        unreachable=outcomes[VerificationOutcome.UNREACHABLE],
    )
