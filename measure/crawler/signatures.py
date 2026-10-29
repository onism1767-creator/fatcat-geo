"""网页「指纹」(用户 2026-10-29 定:官网被拦还是靠脚本加载,不用用户动手、自动分清):
- 防护公司的验证 / 拦截页带固定标记(只在拦截时出现的响应头、页面里的验证脚本地址或固定文字);
- 只靠脚本加载的网站,原始网页是一个空壳框架,也带固定标记(空的挂载容器、框架自带的数据块)。

只做机械比对:认出是哪一家 / 哪种框架就返回名字,认不出返回空列表;不判定、不猜。命中只能说「多半」——
标记清单是按各家拦截页与框架的公开特征拟的(Claude 2026-10-29),结论由调用方再结合真浏览器对照下。
只在「页面字很少」时用:正常大页面里也可能嵌一个验证码组件(比如联系表单),那不算被拦。
零项目内依赖。
"""

from __future__ import annotations

import re
from typing import Final

import httpx

# (名字, 只在拦截 / 验证时才出现的响应头: 值里要含的字(空串 = 有这个头就算), 页面里的固定标记)
# 不收「所有经过这家的网页都带」的头(比如 server: cloudflare、__cf_bm cookie):那只说明用了这家,不说明被拦。
_PROTECTION: Final[tuple[tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]], ...]] = (
    ("Cloudflare", (("cf-mitigated", "challenge"),),
     ("/cdn-cgi/challenge-platform/", "<title>just a moment...</title>", "attention required! | cloudflare",
      "cf-error-details", "challenges.cloudflare.com/turnstile")),
    ("Akamai", (), ("errors.edgesuite.net",)),
    ("DataDome", (("x-dd-b", ""),), ("captcha-delivery.com",)),
    ("HUMAN", (), ("px-captcha",)),
    ("Imperva", (), ("_incapsula_resource", "incapsula incident id")),
    ("AWS WAF", (("x-amzn-waf-action", ""),), ("awswaf.com", "aws-waf-token")),
    ("Sucuri", (("x-sucuri-block", ""),), ("sucuri website firewall",)),
    ("Vercel", (("x-vercel-mitigated", "challenge"),), ()),
    ("CAPTCHA", (), ("g-recaptcha", "recaptcha/api.js", "h-captcha", "hcaptcha.com/1/api.js")),
)

# (名字, 页面里的固定标记;正则,在小写后的 HTML 上找)
_SHELL: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("React", (r'<div[^>]+id="root"[^>]*>\s*</div>', r"data-reactroot")),
    ("Next.js", (r'<div[^>]+id="__next"', r'id="__next_data__"')),
    ("Nuxt", (r'<div[^>]+id="__nuxt"', r"window\.__nuxt__")),
    ("Vue", (r'<div[^>]+id="app"[^>]*>\s*</div>',)),
    ("Angular", (r"<app-root", r"ng-version=")),
    ("noscript", (r"<noscript>[^<]*(enable javascript|javascript is required|"
                                 r"need to enable javascript|开启 ?javascript|启用 ?javascript)",)),
)


def protection_marks(headers: httpx.Headers | None, html: str) -> list[str]:
    """这个响应像哪家防护的验证 / 拦截页;认不出 → 空列表。headers 没有(比如真浏览器那条路)就只看页面。"""
    lowered = html.lower()
    found: list[str] = []
    for name, header_marks, body_marks in _PROTECTION:
        by_header = headers is not None and any(
            key in headers and value in headers[key].lower() for key, value in header_marks
        )
        if by_header or any(mark in lowered for mark in body_marks):
            found.append(name)
    return found


def shell_marks(html: str) -> list[str]:
    """这个网页像哪种只靠脚本加载的空壳框架;认不出 → 空列表。"""
    lowered = html.lower()
    return [name for name, patterns in _SHELL if any(re.search(p, lowered) for p in patterns)]
