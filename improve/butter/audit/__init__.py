"""审计器:只做 Toast 没有的五项检查。纯函数,不联网。

不重做 robots 放行 / 可达 / CDN / SSR 的判定(那是 Toast 的事)。
每项检查对每个页面至少留一条记录:查不了就记「查不了」和原因,不留白。
"""

from __future__ import annotations

from datetime import datetime

from butter.audit import hidden_text, jsonld_consistency, meta_robots, numeric_traceability, visible_date
from butter.audit.base import finding
from butter.contracts import AuditFinding, AuditReport, PageExtract, SiteSnapshot
from butter.registry.ids import (
    ACCESS_META_SNIPPET, FAKE_TIMESTAMP, HIDDEN_TEXT_INJECTION, JSONLD_BODY_CONSISTENCY, NUMERIC_TRACEABILITY,
)

LIMITATIONS = [
    "Butter 不执行页面脚本:原始 HTML 里没有的内容(脚本渲染的正文、脚本注入的 JSON-LD)看不到,相关检查可能漏报或误报。",
    "隐藏文本只看行内样式、hidden 属性和页面内 <style> 的简单规则,不读外链样式表、不做浏览器渲染,会有漏报。",
    "「可见日期真实性」只能查自相矛盾(未来日期、修改早于发布、可见日期与结构化数据不一致、sitemap 全站同一个 lastmod),不能证明日期为真。",
    "「数值可溯源」是规则启发式(统计型数字旁边有没有来源链接或出处字样),只出提示,可能误报。",
    "robots 放行、可达性、CDN 拦截、服务端渲染的判定不在本报告内(Toast 负责)。",
    "只检查了本次读到的页面,不代表全站。",
]
# 本地导入(站长自己存下的 HTML 文件)多出来的限制
LOCAL_LIMITATIONS = [
    "页面来自你存在本机的文件,没有联网:HTTP 响应头看不到,所以 X-Robots-Tag(响应头里的 noindex / nosnippet)"
    "和 Last-Modified 这两处查不了;meta robots 一项只看了页面里的 <meta>。",
    "每个文件对应哪个网址,是按 urls.txt、文件里的 saved from url 注释、canonical、og:url 的顺序认的(见导入备注);"
    "认错了的话,在 urls.txt 里写明后重跑。",
]
RENDERED_LIMITATION = ("有的文件是浏览器「网页,全部」方式保存的:那是脚本运行之后的页面,不是服务器返回的原始 HTML。"
                       "脚本后加的正文和 JSON-LD 在这里看得到,多数爬虫却看不到;这些页面的结果要按「渲染后」理解。")


_NO_PAGES = "本次没有抓到任何可分析的页面,这一项查不了(不代表站上没有问题,也不代表有问题)。"
_ENTRY_WHEN_NO_PAGES = (
    (meta_robots.CHECK, ACCESS_META_SNIPPET), (visible_date.CHECK, FAKE_TIMESTAMP),
    (numeric_traceability.CHECK, NUMERIC_TRACEABILITY), (hidden_text.CHECK, HIDDEN_TEXT_INJECTION),
    (jsonld_consistency.CHECK, JSONLD_BODY_CONSISTENCY),
)


def run_audit(snapshot: SiteSnapshot, extracts: list[PageExtract], *, run_id: str, now: datetime) -> AuditReport:
    raw: list[AuditFinding] = []
    if not extracts:  # 一页都没拿到(被拦、被拒、出错):五项各留一条「查不了」,不留白
        raw = [finding(check, entry_id, "not_checkable", None, _NO_PAGES) for check, entry_id in _ENTRY_WHEN_NO_PAGES]
    else:
        raw += meta_robots.check(extracts)
        raw += visible_date.check(extracts, snapshot, now)
        raw += numeric_traceability.check(extracts)
        raw += hidden_text.check(extracts)
        raw += jsonld_consistency.check(extracts, snapshot.host)
    findings = [f.model_copy(update={"finding_id": f"F{i:03d}"}) for i, f in enumerate(raw, start=1)]
    limitations = list(LIMITATIONS)
    if snapshot.source == "local_files":
        limitations += LOCAL_LIMITATIONS
        if any(p.rendered_dom for p in snapshot.pages):
            limitations.append(RENDERED_LIMITATION)
    return AuditReport(site_url=snapshot.site_url, run_id=run_id, generated_at=now,
                       pages_checked=[p.url for p in extracts], findings=findings, limitations=limitations)
