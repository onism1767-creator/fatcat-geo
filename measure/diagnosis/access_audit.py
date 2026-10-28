"""ACCESS 审计:四阶段漏斗第一关——「能不能被找到」(架构书 5.2、第 3 章步骤 2)。

抓网页用 crawler/(与 adapters/ 同层的公共抓取模块,代码审查 #9);不走 L2,可独立运行。
资格门,不打分:输出 PASS / FAIL / UNKNOWN。

分层的站点侧准入证据(v0.4.13 条目 6):伪装 UA 的探针只能证明「本工具客户端在此
条件下得到的响应」,不能证明官方爬虫得到同样响应(各家均以来源 IP / 反向 DNS 验真身)。
因此每条检查都标注所属证据层:
- POLICY:robots.txt / sitemap,站点单方声明;
- GENERIC_FETCH:本工具探针可达性(首页 + 引用核验重抓的按路径结果);
- RENDERED_FETCH / OFFICIAL_IDENTITY / WAF_EVIDENCE:v1 一律 NOT_OBSERVABLE 占位,
  分别待 Playwright(v2)、站长日志、CDN 后台证据。
资格门只由 POLICY 显式禁止与 GENERIC_FETCH 的非防护类硬失败触发;报告措辞是
「站点侧证据」,不称「AI 爬虫可达 PASS/FAIL」。

v1.1 起,检查详情与口径以结论码(Msg)输出,组句在 diagnosis/messages.py
的双语目录中完成;本模块仅引用零依赖的 messages(不破坏独立性)。

检查项:站点可达性、robots.txt 对各 AI 爬虫令牌的放行、CDN/WAF 对爬虫 UA 的
差异化拦截、HTML 中是否存在可见内容(JS 白板)、sitemap、重定向链。

爬虫名单是配置资产(config/crawlers.yaml):本模块源码不含任何厂商名,
新增要审计的爬虫只改配置,tests/unit/test_no_provider_strings.py 机械检查。

诚实口径(随结果一并输出,见 STANDARD_CAVEATS):
- 审计从普通网络发起,拿不到各家官方爬虫 IP 段。站点若按「官方 IP 白名单」
  放行真爬虫,伪装 UA 会被拒——这与「封杀爬虫 UA」在本审计视角不可区分,
  因此 UA 差异化拦截只出 UNKNOWN,不出 FAIL(铁律 7:不可观测不猜测)。
- robots.txt 是站点单方声明,爬虫遵不遵守不在担保范围。

手动 smoke 入口(12.3:真实网络只存在于此,不进单元测试):
    python -m diagnosis.access_audit https://example.com [--json]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, JsonValue

from crawler.fetch import (
    PROTECTION_SUSPECT_STATUSES,
    RobotsRules,
    extract_visible_text,
    get_as_browser,
    is_html,
    new_client,
)
from diagnosis.messages import Msg, msg, render_msg

MIN_VISIBLE_TEXT_CHARS: Final = 200  # 低于此值视为 JS 白板嫌疑
DEFAULT_CRAWLERS_PATH: Final = Path(__file__).resolve().parents[1] / "config" / "crawlers.yaml"

STANDARD_CAVEATS: Final = (
    msg("access.caveat.vantage"),
    msg("access.caveat.robots_voluntary"),
    msg("access.caveat.ua_list"),
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class AccessLayer(StrEnum):
    """站点侧准入证据层(v0.4.13 条目 6),报告按层分组呈现。"""

    POLICY = "POLICY"
    GENERIC_FETCH = "GENERIC_FETCH"
    RENDERED_FETCH = "RENDERED_FETCH"
    OFFICIAL_IDENTITY = "OFFICIAL_IDENTITY"
    WAF_EVIDENCE = "WAF_EVIDENCE"


class PathProbe(FrozenModel):
    """按路径的探针结果(来自引用核验重抓,同一探针视角):机器人防护常按路径拦截,
    首页探针不代表全站——香奈儿首测 21 条被引 URL 16 条 200、5 条 403。"""

    url: str
    http_status: int | None = None  # None = 网络层失败
    error: str | None = None


class CrawlerBot(FrozenModel):
    name: str
    robots_token: str
    user_agent: str | None = None  # None = 仅 robots 令牌,不发请求
    purpose: str = "search_index"
    gating: bool = True
    docs: str | None = None


class CheckResult(FrozenModel):
    check: str
    layer: AccessLayer
    status: CheckStatus
    gating: bool  # 是否参与资格门聚合;False = 仅提示
    detail: Msg
    evidence: JsonValue = None


class BotProbe(FrozenModel):
    bot: str
    gating: bool
    robots_allowed: bool | None  # None = robots 状态无法判定
    fetch_status: int | None = None  # None = 未发请求(仅 robots 令牌)或网络错误
    fetch_error: str | None = None
    visible_text_chars: int | None = None


class AccessAuditResult(FrozenModel):
    site: str
    final_url: str | None
    overall: CheckStatus
    checks: list[CheckResult]
    bots: list[BotProbe]
    caveats: list[Msg]
    audited_at: datetime
    path_probes: list[PathProbe] = []  # 采样后由 with_path_probes 追加(非资格门)

    def checks_in(self, layer: AccessLayer) -> list[CheckResult]:
        return [c for c in self.checks if c.layer is layer]


def with_path_probes(result: AccessAuditResult, probes: list[PathProbe]) -> AccessAuditResult:
    """把按路径探针结果并入 GENERIC_FETCH 层(仅提示,不改资格门)。没有探针则原样返回。"""
    if not probes:
        return result
    ok = sum(1 for p in probes if p.http_status is not None and 200 <= p.http_status < 300)
    blocked = sum(1 for p in probes if p.http_status in PROTECTION_SUSPECT_STATUSES)
    other = len(probes) - ok - blocked
    check = CheckResult(
        check="path_fetch", layer=AccessLayer.GENERIC_FETCH,
        status=CheckStatus.PASS if ok == len(probes) else CheckStatus.UNKNOWN, gating=False,
        detail=msg("access.path.summary", total=len(probes), ok=ok, blocked=blocked, other=other),
        evidence={"ok": ok, "blocked": blocked, "other": other},
    )
    return result.model_copy(update={
        "checks": [*result.checks, check], "path_probes": [*result.path_probes, *probes],
    })


# ── HTML 可见文本 ──────────────────────────────────────────────────────


def visible_text_length(html: str) -> int:
    """可见文本字符数,用于 JS 白板启发式判断。"""
    return len(extract_visible_text(html))


# ── 配置装载 ──────────────────────────────────────────────────────────


def load_crawlers(path: Path = DEFAULT_CRAWLERS_PATH) -> list[CrawlerBot]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [CrawlerBot(**entry) for entry in data["crawlers"]]


# ── 审计主流程 ────────────────────────────────────────────────────────


def _normalize_url(site_url: str) -> str:
    return site_url if "://" in site_url else f"https://{site_url}"


def _fetch_robots(
    client: httpx.Client, base_url: str
) -> tuple[Callable[[str], bool] | None, list[str], Msg]:
    """返回 (放行判定函数 | None=无法判定, sitemap 声明列表, 状态说明)。"""
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        resp = get_as_browser(client, robots_url)
    except httpx.HTTPError as exc:
        return None, [], msg("access.robots.note.fetch_failed", error=type(exc).__name__)
    if resp.status_code == 200:
        rules = RobotsRules(resp.text)  # 认通配符与 $(审核 2026-10-27 黄 12)
        return (lambda token: rules.can_fetch(token, base_url)), rules.site_maps(), msg("access.robots.note.parsed")
    if 400 <= resp.status_code < 500:
        return (lambda token: True), [], msg("access.robots.note.no_restrictions", status=resp.status_code)
    return None, [], msg("access.robots.note.server_error", status=resp.status_code)


def audit(
    site_url: str,
    crawlers: list[CrawlerBot] | None = None,
    client: httpx.Client | None = None,
) -> AccessAuditResult:
    crawlers = crawlers if crawlers is not None else load_crawlers()
    url = _normalize_url(site_url)
    own_client = client is None
    client = client or new_client()
    try:
        return _audit(url, crawlers, client)
    finally:
        if own_client:
            client.close()


def _audit(url: str, crawlers: list[CrawlerBot], client: httpx.Client) -> AccessAuditResult:
    """逐项检查后按资格门聚合。网络请求顺序:基准 → robots.txt → 各爬虫 UA → sitemap。"""
    caveats = list(STANDARD_CAVEATS)
    reach, baseline, final_url, redirect_chain = _check_reachability(client, url)
    robots_base = final_url or url  # robots.txt / sitemap 按最终主机
    allowed_fn, sitemap_urls, robots_note = _fetch_robots(client, robots_base)
    probes = _probe_bots(client, url, crawlers, allowed_fn)
    content, browser_view_fallback = _check_content(baseline, probes)
    if browser_view_fallback:
        caveats.append(msg("access.caveat.browser_view_fallback"))
    checks = [
        reach,
        _check_redirects(url, final_url, redirect_chain),
        _check_robots(allowed_fn, robots_note, probes),
        _check_ua_differential(baseline is not None, probes),
        content,
        _check_sitemap(client, robots_base, sitemap_urls),
        *_unobservable_layers(),
    ]
    return AccessAuditResult(
        site=url, final_url=final_url, overall=_gate(checks),
        checks=checks, bots=probes, caveats=caveats, audited_at=datetime.now(UTC),
    )


def _check_reachability(
    client: httpx.Client, url: str
) -> tuple[CheckResult, httpx.Response | None, str | None, list[str]]:
    """1. 可达性基准(浏览器 UA)+ 重定向链。返回 (检查结果, 成功的基准响应 | None, 最终地址, 重定向链)。"""
    try:
        baseline = get_as_browser(client, url)
    except httpx.TooManyRedirects:
        return CheckResult(
            check="reachability", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.FAIL, gating=True,
            detail=msg("access.reach.redirect_loop"),
        ), None, None, []
    except httpx.HTTPError as exc:
        return CheckResult(
            check="reachability", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.reach.error", error=type(exc).__name__),
        ), None, None, []
    final_url = str(baseline.url)
    redirect_chain = [f"{r.status_code} {r.url}" for r in baseline.history]
    evidence: dict[str, JsonValue] = {"status": baseline.status_code, "final_url": final_url}
    if baseline.is_success:
        status, detail = CheckStatus.PASS, msg("access.reach.ok", status=baseline.status_code)
    elif baseline.status_code in PROTECTION_SUSPECT_STATUSES:
        # 铁律 7:探针被拒 ≠ 真人被拒 ≠ 爬虫被拒。带浏览器 UA 仍被 401/403/429/503
        # 拒绝,典型为 CDN 机器人防护按客户端指纹拦截非浏览器客户端(2026-09
        # 香奈儿首测复盘:真实浏览器可开,一切自动化客户端被 Akamai 403)。
        # 三种真相在本审计视角下不可区分,只能 UNKNOWN,不许判死。
        status, detail = CheckStatus.UNKNOWN, msg("access.reach.blocked", status=baseline.status_code)
    else:
        status, detail = CheckStatus.FAIL, msg("access.reach.bad", status=baseline.status_code)
    reach = CheckResult(check="reachability", layer=AccessLayer.GENERIC_FETCH,
                        status=status, gating=True, detail=detail, evidence=evidence)
    return reach, baseline if baseline.is_success else None, final_url, redirect_chain


def _check_redirects(url: str, final_url: str | None, redirect_chain: list[str]) -> CheckResult:
    if not redirect_chain:
        return CheckResult(
            check="redirects", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=False,
            detail=msg("access.redirects.none"),
        )
    origin_host = urlsplit(url).hostname
    final_host = urlsplit(final_url or url).hostname
    chain_params = dict(count=len(redirect_chain), chain=" → ".join(redirect_chain), final=final_url)
    detail = (
        msg("access.redirects.chain_cross", host=final_host, **chain_params)
        if final_host != origin_host
        else msg("access.redirects.chain", **chain_params)
    )
    return CheckResult(
        check="redirects", layer=AccessLayer.GENERIC_FETCH,
        status=CheckStatus.PASS, gating=False,
        detail=detail, evidence={"chain": redirect_chain, "final_url": final_url},
    )


def _probe_bots(
    client: httpx.Client, url: str, crawlers: list[CrawlerBot], allowed_fn: Callable[[str], bool] | None,
) -> list[BotProbe]:
    """3. 逐爬虫探测:robots 放行 + 真实 UA 请求(只有 user_agent 的爬虫才抓)。"""
    probes: list[BotProbe] = []
    for bot in crawlers:
        fetch_status: int | None = None
        fetch_error: str | None = None
        chars: int | None = None
        if bot.user_agent:
            try:
                resp = client.get(url, headers={"User-Agent": bot.user_agent})
                fetch_status = resp.status_code
                if resp.is_success and is_html(resp):
                    chars = visible_text_length(resp.text)
            except httpx.HTTPError as exc:
                fetch_error = type(exc).__name__
        probes.append(BotProbe(
            bot=bot.name, gating=bot.gating,
            robots_allowed=allowed_fn(bot.robots_token) if allowed_fn else None,
            fetch_status=fetch_status, fetch_error=fetch_error, visible_text_chars=chars,
        ))
    return probes


def _check_robots(
    allowed_fn: Callable[[str], bool] | None, robots_note: Msg, probes: list[BotProbe]
) -> CheckResult:
    """4. robots 聚合:进资格门的爬虫被禁 → FAIL;不进资格门的被禁只作附注。"""
    if allowed_fn is None:
        return CheckResult(
            check="robots_txt", layer=AccessLayer.POLICY,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.robots.unknown", note=robots_note),
        )
    denied_gating = [p.bot for p in probes if p.gating and p.robots_allowed is False]
    denied_info = [p.bot for p in probes if not p.gating and p.robots_allowed is False]
    info: Msg | str = msg("access.robots.info_denied", names=", ".join(denied_info)) if denied_info else ""
    if denied_gating:
        return CheckResult(
            check="robots_txt", layer=AccessLayer.POLICY,
            status=CheckStatus.FAIL, gating=True,
            detail=msg("access.robots.fail", denied=", ".join(denied_gating), info=info),
            evidence={"denied": denied_gating, "denied_non_gating": denied_info},
        )
    return CheckResult(
        check="robots_txt", layer=AccessLayer.POLICY,
        status=CheckStatus.PASS, gating=True,
        detail=msg("access.robots.pass", note=robots_note, n=sum(p.gating for p in probes), info=info),
        evidence={"denied_non_gating": denied_info},
    )


def _check_ua_differential(baseline_ok: bool, probes: list[BotProbe]) -> CheckResult:
    """5. UA 差异化拦截:只出 PASS / UNKNOWN,不出 FAIL(理由见模块 docstring)。"""
    probed = [p for p in probes if p.gating and (p.fetch_status is not None or p.fetch_error)]
    blocked = [
        f"{p.bot}(HTTP {p.fetch_status})" if p.fetch_status else f"{p.bot}({p.fetch_error})"
        for p in probed
        if p.fetch_error or (p.fetch_status is not None and not (200 <= p.fetch_status < 300))
    ]
    if not baseline_ok:
        return CheckResult(
            check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.ua.no_baseline"),
        )
    if blocked:
        return CheckResult(
            check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.ua.blocked", blocked=", ".join(blocked)),
            evidence={"blocked": blocked},
        )
    return CheckResult(
        check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
        status=CheckStatus.PASS, gating=True,
        detail=msg("access.ua.pass", n=len(probed)),
    )


def _check_content(baseline: httpx.Response | None, probes: list[BotProbe]) -> tuple[CheckResult, bool]:
    """6. HTML 内容(JS 白板):优先以爬虫视角判定;没有爬虫视角才退用浏览器视角(返回的布尔值 = 退用了)。"""
    browser_chars = visible_text_length(baseline.text) if baseline is not None and is_html(baseline) else None
    bot_views = [(p.bot, p.visible_text_chars) for p in probes if p.visible_text_chars is not None]
    label: Msg | str = ""
    fallback = False
    if bot_views:
        bot_name, chars = max(bot_views, key=lambda view: view[1])
        label = msg("access.content.label.bot", bot=bot_name)
    elif browser_chars is not None:
        label, chars = msg("access.content.label.browser"), browser_chars
        fallback = True
    else:
        return CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.content.none"),
        ), fallback
    evidence: dict[str, JsonValue] = {"visible_chars": chars, "browser_chars": browser_chars}
    if chars >= MIN_VISIBLE_TEXT_CHARS:
        prerender: Msg | str = (
            msg("access.content.prerender_note")
            if browser_chars is not None and browser_chars < MIN_VISIBLE_TEXT_CHARS and bot_views
            else ""
        )
        return CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=True,
            detail=msg("access.content.pass", chars=chars, label=label, prerender=prerender),
            evidence=evidence,
        ), fallback
    if browser_chars is not None and browser_chars >= MIN_VISIBLE_TEXT_CHARS and bot_views:
        # 浏览器视角有正文、冒充的爬虫 UA 拿到 200 空壳:本工具用的是假身份,分不清「拦真爬虫」与「拦假身份」
        # (与 UA 差异检查同一条理由),只能记看不到,不判死(铁律 7;审核 2026-10-27 黄 11)
        return CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.content.bot_shell_unknown", chars=chars, label=label,
                       threshold=MIN_VISIBLE_TEXT_CHARS, browser_chars=browser_chars),
            evidence=evidence,
        ), fallback
    return CheckResult(
        check="html_content", layer=AccessLayer.GENERIC_FETCH,
        status=CheckStatus.FAIL, gating=True,
        detail=msg("access.content.fail", chars=chars, label=label, threshold=MIN_VISIBLE_TEXT_CHARS),
        evidence=evidence,
    ), fallback


def _check_sitemap(client: httpx.Client, robots_base: str, sitemap_urls: list[str]) -> CheckResult:
    """7. sitemap(仅提示,不进资格门):robots.txt 声明了就认;没声明再试 /sitemap.xml。"""
    if sitemap_urls:
        return CheckResult(
            check="sitemap", layer=AccessLayer.POLICY,
            status=CheckStatus.PASS, gating=False,
            detail=msg("access.sitemap.declared", urls=", ".join(sitemap_urls[:3])),
            evidence={"sitemaps": sitemap_urls},
        )
    sitemap_url = urljoin(robots_base, "/sitemap.xml")
    try:
        resp = get_as_browser(client, sitemap_url)
    except httpx.HTTPError as exc:
        return CheckResult(
            check="sitemap", layer=AccessLayer.POLICY,
            status=CheckStatus.UNKNOWN, gating=False,
            detail=msg("access.sitemap.error", error=type(exc).__name__),
        )
    if resp.is_success:
        return CheckResult(
            check="sitemap", layer=AccessLayer.POLICY,
            status=CheckStatus.PASS, gating=False,
            detail=msg("access.sitemap.fallback", url=sitemap_url),
        )
    return CheckResult(
        check="sitemap", layer=AccessLayer.POLICY,
        status=CheckStatus.FAIL, gating=False,
        detail=msg("access.sitemap.missing", url=sitemap_url, status=resp.status_code),
    )


def _unobservable_layers() -> list[CheckResult]:
    """7b. v1 不可观测的证据层显式占位(铁律 7:不可观测就记下来,不留空白让人脑补)。"""
    return [
        CheckResult(check="rendered_fetch", layer=AccessLayer.RENDERED_FETCH,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.rendered.not_observable")),
        CheckResult(check="official_identity", layer=AccessLayer.OFFICIAL_IDENTITY,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.official.not_observable")),
        CheckResult(check="waf_evidence", layer=AccessLayer.WAF_EVIDENCE,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.waf.not_observable")),
    ]


def _gate(checks: list[CheckResult]) -> CheckStatus:
    """8. 资格门聚合:任一进门检查 FAIL → FAIL;否则有 UNKNOWN → UNKNOWN;否则 PASS。"""
    gating = [c.status for c in checks if c.gating]
    if CheckStatus.FAIL in gating:
        return CheckStatus.FAIL
    if CheckStatus.UNKNOWN in gating:
        return CheckStatus.UNKNOWN
    return CheckStatus.PASS


# ── 呈现与 CLI(开发者工具,固定中文渲染)───────────────────────────────


def render_text(result: AccessAuditResult) -> str:
    lines = [f"■ ACCESS — {result.overall}   {result.site}(站点侧准入证据,本工具探针视角)"]
    if result.final_url and result.final_url != result.site:
        lines.append(f"  最终地址:{result.final_url}")
    for layer in AccessLayer:
        lines.append("")
        lines.append(f"  ▸ {render_msg(msg(f'access.layer.{layer.value}'), 'zh')}")
        for check in result.checks_in(layer):
            tag = "" if check.gating else "(提示)"
            lines.append(f"    [{check.status}]{tag} {check.check} — {render_msg(check.detail, 'zh')}")
    lines.append("")
    lines.append("  爬虫探测明细:")
    for probe in result.bots:
        robots = {True: "robots 放行", False: "robots 禁止", None: "robots 未知"}[probe.robots_allowed]
        if probe.fetch_status is not None:
            fetch = f"HTTP {probe.fetch_status}"
            if probe.visible_text_chars is not None:
                fetch += f",可见文本约 {probe.visible_text_chars} 字符"
        elif probe.fetch_error:
            fetch = f"请求失败({probe.fetch_error})"
        else:
            fetch = "仅 robots 令牌,不抓取"
        gate = "" if probe.gating else "(不进资格门)"
        lines.append(f"    {probe.bot}{gate}: {robots}; {fetch}")
    lines.append("")
    lines.append("  口径与局限:")
    lines.extend(f"    - {render_msg(caveat, 'zh')}" for caveat in result.caveats)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACCESS 审计:分层的站点侧准入证据(本工具探针视角)")
    parser.add_argument("url", help="待审计的站点地址")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--config", type=Path, default=DEFAULT_CRAWLERS_PATH, help="爬虫名单配置")
    args = parser.parse_args(argv)

    result = audit(args.url, load_crawlers(args.config))
    print(result.model_dump_json(indent=2) if args.json else render_text(result))
    return {CheckStatus.PASS: 0, CheckStatus.FAIL: 1, CheckStatus.UNKNOWN: 2}[result.overall]


if __name__ == "__main__":
    sys.exit(main())
