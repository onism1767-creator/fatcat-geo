"""审计器:只做 Toast 没有的五项检查。纯函数,不联网。

不重做 robots 放行 / 可达 / CDN / SSR 的判定(那是 Toast 的事)。
每项检查对每个页面至少留一条记录:查不了就记「查不了」和原因,不留白。
"""

from __future__ import annotations

from datetime import datetime

from butter.audit import hidden_text, jsonld_consistency, meta_robots, numeric_traceability, visible_date
from butter.contracts import AuditFinding, AuditReport, PageExtract, SiteSnapshot

LIMITATIONS = [
    "Butter 不执行页面脚本:原始 HTML 里没有的内容(脚本渲染的正文、脚本注入的 JSON-LD)看不到,相关检查可能漏报或误报。",
    "隐藏文本只看行内样式、hidden 属性和页面内 <style> 的简单规则,不读外链样式表、不做浏览器渲染,会有漏报。",
    "「可见日期真实性」只能查自相矛盾(未来日期、修改早于发布、可见日期与结构化数据不一致、sitemap 全站同一个 lastmod),不能证明日期为真。",
    "「数值可溯源」是规则启发式(统计型数字旁边有没有来源链接或出处字样),只出提示,可能误报。",
    "robots 放行、可达性、CDN 拦截、服务端渲染的判定不在本报告内(Toast 负责)。",
    "只检查了本次抓到的页面,不代表全站。",
]


def run_audit(snapshot: SiteSnapshot, extracts: list[PageExtract], *, run_id: str, now: datetime) -> AuditReport:
    raw: list[AuditFinding] = []
    raw += meta_robots.check(extracts)
    raw += visible_date.check(extracts, snapshot, now)
    raw += numeric_traceability.check(extracts)
    raw += hidden_text.check(extracts)
    raw += jsonld_consistency.check(extracts, snapshot.host)
    findings = [f.model_copy(update={"finding_id": f"F{i:03d}"}) for i, f in enumerate(raw, start=1)]
    return AuditReport(site_url=snapshot.site_url, run_id=run_id, generated_at=now,
                       pages_checked=[p.url for p in extracts], findings=findings, limitations=LIMITATIONS)
