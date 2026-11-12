"""认人机验证页。纯函数:联网抓取和本地导入共用。

只认明确的验证 / 质询字样,不把普通的 403 当验证码;认出来的唯一用途是「停」或「不导入」,不是为了绕过。
"""

from __future__ import annotations

import re
from typing import Final

REFUSAL_STATUSES: Final = (401, 403, 429, 503)
# 人机验证页的标记(小写比对)
_CHALLENGE_MARKERS: Final = (
    "just a moment...", "cf-chl-", "challenge-platform", "cf-turnstile", "attention required! | cloudflare",
    "g-recaptcha", "recaptcha/api", "hcaptcha.com", "h-captcha", "geetest", "verify you are human",
    "verifying you are human", "are you a robot", "captcha-delivery", "px-captcha", "人机验证", "安全验证",
    "滑动验证", "请完成验证", "验证码", "acw_sc__v2",
)
_SCRIPTS: Final = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAGS: Final = re.compile(r"<[^>]*>")


def looks_like_challenge(status: int | None, headers: dict[str, str], body: str) -> str | None:
    """像不像人机验证页。返回认出的标记;不像返回 None。"""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "cf-mitigated: challenge"
    head = body[:20_000].lower()
    refused = status in REFUSAL_STATUSES
    # 正常返回的页面里也会带验证码组件(联系表单、登录框):只有「被拒」或「页面几乎没有正文」才算被验证页挡住
    thin = len(body) < 20_000 and len(_TAGS.sub("", _SCRIPTS.sub("", body)).strip()) < 400
    if refused or thin:
        for marker in _CHALLENGE_MARKERS:
            if marker in head:
                return marker
    return None
