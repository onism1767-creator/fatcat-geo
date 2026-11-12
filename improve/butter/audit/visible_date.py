"""检查二:可见日期真实性。

只能查自相矛盾,不能证明日期为真:
- 未来日期;修改早于发布;
- 读者看得到的日期与机器可读日期(结构化数据 / meta)对不上;
- sitemap 里所有地址的 lastmod 是同一天;
- 页面有机器可读日期,但读者看不到日期。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from butter.audit.base import finding
from butter.contracts import AuditFinding, PageExtract, RawDate, SiteSnapshot
from butter.extract.dates import to_iso_date
from butter.registry_ids import FAKE_TIMESTAMP, FRESHNESS_VISIBLE_DATE

CHECK = "visible_date"
_VISIBLE = ("visible", "time_element")
_MACHINE_MODIFIED = ("jsonld_modified", "meta_modified", "og_updated")
_MACHINE_PUBLISHED = ("jsonld_published", "meta_published")
_SITEMAP_MIN = 5  # 少于这么多条不下「全站同一天」的判断
_KIND_LABEL = {
    "visible": "正文里的日期", "time_element": "<time> 元素", "jsonld_published": "结构化数据 datePublished",
    "jsonld_modified": "结构化数据 dateModified", "meta_published": "meta 发布时间", "meta_modified": "meta 修改时间",
    "og_updated": "og:updated_time", "sitemap_lastmod": "sitemap lastmod", "http_last_modified": "HTTP Last-Modified",
}


def _d(raw: RawDate) -> date | None:
    return date.fromisoformat(raw.iso_date) if raw.iso_date else None


def _close(a: date, b: date) -> bool:
    return abs((a - b).days) <= 1  # 时区差一天不算


def _describe(d: RawDate) -> str:
    return f"{_KIND_LABEL[d.kind]}「{d.raw}」"


def check(extracts: list[PageExtract], snapshot: SiteSnapshot, now: datetime) -> list[AuditFinding]:
    today = now.date()
    findings: list[AuditFinding] = []
    modified_today_pages = 0
    pages_with_modified = 0

    for page in extracts:
        dated = [(d, _d(d)) for d in page.dates]
        parsed = [(d, day) for d, day in dated if day is not None]
        if not page.dates:
            findings.append(finding(CHECK, FAKE_TIMESTAMP, "not_checkable", page.url,
                                    "本页没有找到任何日期(正文、<time>、结构化数据、meta、sitemap、HTTP 头都没有),查不了。"))
            continue
        page_findings: list[AuditFinding] = []

        future_seen: set[date] = set()  # 同一个日期在 <time> 和正文里各出现一次,只报一条
        for d, day in parsed:
            if day > today + timedelta(days=1) and day not in future_seen:
                future_seen.add(day)
                page_findings.append(finding(
                    CHECK, FAKE_TIMESTAMP, "problem", page.url,
                    f"{_describe(d)} 是未来的日期(抓取当天是 {today.isoformat()})。",
                    location=d.source_path, observed=d.raw, expected=f"不晚于 {today.isoformat()}"))

        for family, pub_kind, mod_kind in (("结构化数据", "jsonld_published", "jsonld_modified"),
                                           ("meta", "meta_published", "meta_modified")):
            published = [day for d, day in parsed if d.kind == pub_kind]
            modified = [day for d, day in parsed if d.kind == mod_kind]
            if published and modified and max(modified) < min(published):
                page_findings.append(finding(
                    CHECK, FAKE_TIMESTAMP, "problem", page.url,
                    f"{family}里的修改日期({max(modified).isoformat()})早于发布日期({min(published).isoformat()}),自相矛盾。",
                    observed=f"发布 {min(published).isoformat()} / 修改 {max(modified).isoformat()}"))

        visible = [(d, day) for d, day in parsed if d.kind in _VISIBLE]
        machine_mod = [day for d, day in parsed if d.kind in _MACHINE_MODIFIED]
        machine_pub = [day for d, day in parsed if d.kind in _MACHINE_PUBLISHED]
        mismatch_seen: set[tuple[date, str]] = set()
        for d, day in visible:
            pool = {"modified": machine_mod, "published": machine_pub}.get(d.label, machine_mod + machine_pub)
            if pool and not any(_close(day, other) for other in pool) and (day, d.label) not in mismatch_seen:
                mismatch_seen.add((day, d.label))
                page_findings.append(finding(
                    CHECK, FAKE_TIMESTAMP, "notice", page.url,
                    f"读者看到的日期是 {day.isoformat()},机器可读的日期里没有同一天的"
                    f"({'、'.join(sorted({o.isoformat() for o in pool}))}),两边对不上。",
                    location=d.source_path, observed=d.raw,
                    expected="、".join(sorted({o.isoformat() for o in pool}))))

        # 只对自己声明了日期的页面提这条(结构化数据 / meta);光是 sitemap 里有 lastmod 的首页、关于页不提
        declared = _MACHINE_MODIFIED + _MACHINE_PUBLISHED
        if not any(d.kind in _VISIBLE for d in page.dates) and any(d.kind in declared for d in page.dates):
            machine = "、".join(sorted({_describe(d) for d in page.dates if d.kind in declared}))
            page_findings.append(finding(
                CHECK, FRESHNESS_VISIBLE_DATE, "notice", page.url,
                "本页有机器可读的日期,但读者在页面上看不到日期。可以把真实的更新日期显示出来(只写真实日期,不为了显得新而改)。",
                observed=machine))

        unreadable = [d.raw for d, day in dated if day is None]
        if machine_mod:
            pages_with_modified += 1
            if any(_close(day, today) for day in machine_mod):
                modified_today_pages += 1
        if not page_findings:
            note = f";另有 {len(unreadable)} 个日期写法认不出" if unreadable else ""
            page_findings.append(finding(
                CHECK, FAKE_TIMESTAMP, "pass", page.url,
                f"找到 {len(page.dates)} 个日期,未见自相矛盾{note}。",
                observed=";".join(_describe(d) for d in page.dates[:6])))
        findings.extend(page_findings)

    if pages_with_modified >= 3 and modified_today_pages == pages_with_modified:
        findings.append(finding(
            CHECK, FAKE_TIMESTAMP, "notice", None,
            f"带修改日期的 {pages_with_modified} 个页面,修改日期全是抓取当天({today.isoformat()})。"
            "如果这些页面今天并没有都改过,这个日期可能是每次访问自动刷新的。",
            observed=f"{modified_today_pages}/{pages_with_modified} 个页面"))

    # sitemap 层面
    lastmods = [(e.loc, to_iso_date(e.lastmod)) for e in snapshot.sitemap_entries if e.lastmod]
    days = [date.fromisoformat(iso) for _, iso in lastmods if iso]
    if not days:
        findings.append(finding(CHECK, FAKE_TIMESTAMP, "not_checkable", None,
                                "没有读到 sitemap,或其中没有 lastmod,查不了 sitemap 的日期。"))
    else:
        site_findings: list[AuditFinding] = []
        future = [(loc, iso) for loc, iso in lastmods if iso and date.fromisoformat(iso) > today + timedelta(days=1)]
        if future:
            site_findings.append(finding(
                CHECK, FAKE_TIMESTAMP, "problem", None,
                f"sitemap 里有 {len(future)} 个地址的 lastmod 是未来的日期。",
                location=snapshot.sitemap_urls[0] if snapshot.sitemap_urls else "sitemap",
                observed=f"{future[0][0]} → {future[0][1]}"))
        if len(days) >= _SITEMAP_MIN and len(set(days)) == 1:
            site_findings.append(finding(
                CHECK, FAKE_TIMESTAMP, "notice", None,
                f"sitemap 里 {len(days)} 个地址的 lastmod 全是同一天({days[0].isoformat()})。"
                "如果这些页面不是同一天改的,这个日期可能是生成 sitemap 时统一打的,不是各页真实的修改日期。",
                location=snapshot.sitemap_urls[0] if snapshot.sitemap_urls else "sitemap",
                observed=f"{len(days)} 条 lastmod 都是 {days[0].isoformat()}"))
        if not site_findings:
            site_findings.append(finding(
                CHECK, FAKE_TIMESTAMP, "pass", None,
                f"sitemap 里 {len(days)} 条 lastmod 分布在 {len(set(days))} 个不同的日子,没有未来日期。"))
        findings.extend(site_findings)
    return findings
