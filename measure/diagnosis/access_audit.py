"""ACCESS 审计:四阶段漏斗第一关——「能不能被找到」(架构书 5.2、第 3 章步骤 2)。

自带轻量爬取器,不走 L1/L2,可独立运行(依赖方向铁律的唯一例外)。
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
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Final
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, JsonValue

from diagnosis.messages import Msg, msg, render_msg

BROWSER_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_S: Final = 15.0
# 疑似机器人防护的拒绝状态(401/403/429/503):探针被拒不可判定为全站不可达
PROTECTION_SUSPECT_STATUSES: Final = (401, 403, 429, 503)
MIN_VISIBLE_TEXT_CHARS: Final = 200  # 低于此值视为 JS 白板嫌疑
MAX_PARSED_BYTES: Final = 2_000_000
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


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def extract_visible_text(html: str) -> str:
    """去掉 script/style 等后的可见文本(引用核验器与本模块共用)。"""
    extractor = _TextExtractor()
    extractor.feed(html[:MAX_PARSED_BYTES])
    return re.sub(r"\s+", " ", "".join(extractor.parts)).strip()


def visible_text_length(html: str) -> int:
    """可见文本字符数,用于 JS 白板启发式判断。"""
    return len(extract_visible_text(html))


def _is_html(response: httpx.Response) -> bool:
    return "html" in response.headers.get("content-type", "")


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
        resp = client.get(robots_url, headers={"User-Agent": BROWSER_UA})
    except httpx.HTTPError as exc:
        return None, [], msg("access.robots.note.fetch_failed", error=type(exc).__name__)
    if resp.status_code == 200:
        parser = robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        sitemaps = list(parser.site_maps() or [])
        return (lambda token: parser.can_fetch(token, base_url)), sitemaps, msg("access.robots.note.parsed")
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
    client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        return _audit(url, crawlers, client)
    finally:
        if own_client:
            client.close()


def _audit(url: str, crawlers: list[CrawlerBot], client: httpx.Client) -> AccessAuditResult:
    checks: list[CheckResult] = []
    caveats = list(STANDARD_CAVEATS)

    # 1. 可达性基准(浏览器 UA)+ 重定向链
    baseline: httpx.Response | None = None
    final_url: str | None = None
    redirect_chain: list[str] = []
    try:
        baseline = client.get(url, headers={"User-Agent": BROWSER_UA})
        final_url = str(baseline.url)
        redirect_chain = [f"{r.status_code} {r.url}" for r in baseline.history]
        if baseline.is_success:
            reach = CheckResult(
                check="reachability", layer=AccessLayer.GENERIC_FETCH,
                status=CheckStatus.PASS, gating=True,
                detail=msg("access.reach.ok", status=baseline.status_code),
                evidence={"status": baseline.status_code, "final_url": final_url},
            )
        elif baseline.status_code in PROTECTION_SUSPECT_STATUSES:
            # 铁律 7:探针被拒 ≠ 真人被拒 ≠ 爬虫被拒。带浏览器 UA 仍被 401/403/429/503
            # 拒绝,典型为 CDN 机器人防护按客户端指纹拦截非浏览器客户端(2026-09
            # 香奈儿首测复盘:真实浏览器可开,一切自动化客户端被 Akamai 403)。
            # 三种真相在本审计视角下不可区分,只能 UNKNOWN,不许判死。
            reach = CheckResult(
                check="reachability", layer=AccessLayer.GENERIC_FETCH,
                status=CheckStatus.UNKNOWN, gating=True,
                detail=msg("access.reach.blocked", status=baseline.status_code),
                evidence={"status": baseline.status_code, "final_url": final_url},
            )
        else:
            reach = CheckResult(
                check="reachability", layer=AccessLayer.GENERIC_FETCH,
                status=CheckStatus.FAIL, gating=True,
                detail=msg("access.reach.bad", status=baseline.status_code),
                evidence={"status": baseline.status_code, "final_url": final_url},
            )
    except httpx.TooManyRedirects:
        reach = CheckResult(
            check="reachability", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.FAIL, gating=True,
            detail=msg("access.reach.redirect_loop"),
        )
    except httpx.HTTPError as exc:
        reach = CheckResult(
            check="reachability", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.reach.error", error=type(exc).__name__),
        )
    checks.append(reach)
    baseline_ok = baseline is not None and baseline.is_success

    if redirect_chain:
        origin_host = urlsplit(url).hostname
        final_host = urlsplit(final_url or url).hostname
        chain_params = dict(
            count=len(redirect_chain), chain=" → ".join(redirect_chain), final=final_url
        )
        detail = (
            msg("access.redirects.chain_cross", host=final_host, **chain_params)
            if final_host != origin_host
            else msg("access.redirects.chain", **chain_params)
        )
        checks.append(CheckResult(
            check="redirects", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=False,
            detail=detail, evidence={"chain": redirect_chain, "final_url": final_url},
        ))
    else:
        checks.append(CheckResult(
            check="redirects", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=False,
            detail=msg("access.redirects.none"),
        ))

    # 2. robots.txt(按最终主机)
    robots_base = final_url or url
    allowed_fn, sitemap_urls, robots_note = _fetch_robots(client, robots_base)

    # 3. 逐爬虫探测:robots 放行 + 真实 UA 请求
    probes: list[BotProbe] = []
    for bot in crawlers:
        fetch_status: int | None = None
        fetch_error: str | None = None
        chars: int | None = None
        if bot.user_agent:
            try:
                resp = client.get(url, headers={"User-Agent": bot.user_agent})
                fetch_status = resp.status_code
                if resp.is_success and _is_html(resp):
                    chars = visible_text_length(resp.text)
            except httpx.HTTPError as exc:
                fetch_error = type(exc).__name__
        probes.append(BotProbe(
            bot=bot.name, gating=bot.gating,
            robots_allowed=allowed_fn(bot.robots_token) if allowed_fn else None,
            fetch_status=fetch_status, fetch_error=fetch_error, visible_text_chars=chars,
        ))

    # 4. robots 聚合
    if allowed_fn is None:
        checks.append(CheckResult(
            check="robots_txt", layer=AccessLayer.POLICY,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.robots.unknown", note=robots_note),
        ))
    else:
        denied_gating = [p.bot for p in probes if p.gating and p.robots_allowed is False]
        denied_info = [p.bot for p in probes if not p.gating and p.robots_allowed is False]
        info: Msg | str = (
            msg("access.robots.info_denied", names=", ".join(denied_info)) if denied_info else ""
        )
        if denied_gating:
            checks.append(CheckResult(
                check="robots_txt", layer=AccessLayer.POLICY,
                status=CheckStatus.FAIL, gating=True,
                detail=msg("access.robots.fail", denied=", ".join(denied_gating), info=info),
                evidence={"denied": denied_gating, "denied_non_gating": denied_info},
            ))
        else:
            checks.append(CheckResult(
                check="robots_txt", layer=AccessLayer.POLICY,
                status=CheckStatus.PASS, gating=True,
                detail=msg("access.robots.pass",
                           note=robots_note, n=sum(p.gating for p in probes), info=info),
                evidence={"denied_non_gating": denied_info},
            ))

    # 5. UA 差异化拦截(只出 PASS/UNKNOWN,不出 FAIL,理由见模块 docstring)
    probed = [p for p in probes if p.gating and (p.fetch_status is not None or p.fetch_error)]
    blocked = [
        f"{p.bot}(HTTP {p.fetch_status})" if p.fetch_status else f"{p.bot}({p.fetch_error})"
        for p in probed
        if p.fetch_error or (p.fetch_status is not None and not (200 <= p.fetch_status < 300))
    ]
    if not baseline_ok:
        checks.append(CheckResult(
            check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.ua.no_baseline"),
        ))
    elif blocked:
        checks.append(CheckResult(
            check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.ua.blocked", blocked=", ".join(blocked)),
            evidence={"blocked": blocked},
        ))
    else:
        checks.append(CheckResult(
            check="ua_differential", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=True,
            detail=msg("access.ua.pass", n=len(probed)),
        ))

    # 6. HTML 内容(JS 白板):优先以爬虫视角判定
    browser_chars = (
        visible_text_length(baseline.text)
        if baseline_ok and baseline is not None and _is_html(baseline)
        else None
    )
    bot_views = [(p.bot, p.visible_text_chars) for p in probes if p.visible_text_chars is not None]
    label: Msg | str = ""
    if bot_views:
        bot_name, chars = max(bot_views, key=lambda view: view[1])
        label = msg("access.content.label.bot", bot=bot_name)
    elif browser_chars is not None:
        label, chars = msg("access.content.label.browser"), browser_chars
        caveats.append(msg("access.caveat.browser_view_fallback"))
    else:
        chars = -1
    if chars < 0:
        checks.append(CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.UNKNOWN, gating=True,
            detail=msg("access.content.none"),
        ))
    elif chars >= MIN_VISIBLE_TEXT_CHARS:
        prerender: Msg | str = (
            msg("access.content.prerender_note")
            if browser_chars is not None and browser_chars < MIN_VISIBLE_TEXT_CHARS and bot_views
            else ""
        )
        checks.append(CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.PASS, gating=True,
            detail=msg("access.content.pass", chars=chars, label=label, prerender=prerender),
            evidence={"visible_chars": chars, "browser_chars": browser_chars},
        ))
    else:
        reverse: Msg | str = (
            msg("access.content.reverse_note")
            if browser_chars is not None and browser_chars >= MIN_VISIBLE_TEXT_CHARS and bot_views
            else ""
        )
        checks.append(CheckResult(
            check="html_content", layer=AccessLayer.GENERIC_FETCH,
            status=CheckStatus.FAIL, gating=True,
            detail=msg("access.content.fail", chars=chars, label=label,
                       threshold=MIN_VISIBLE_TEXT_CHARS, reverse=reverse),
            evidence={"visible_chars": chars, "browser_chars": browser_chars},
        ))

    # 7. sitemap(仅提示,不进资格门)
    if sitemap_urls:
        checks.append(CheckResult(
            check="sitemap", layer=AccessLayer.POLICY,
            status=CheckStatus.PASS, gating=False,
            detail=msg("access.sitemap.declared", urls=", ".join(sitemap_urls[:3])),
            evidence={"sitemaps": sitemap_urls},
        ))
    else:
        sitemap_url = urljoin(robots_base, "/sitemap.xml")
        try:
            resp = client.get(sitemap_url, headers={"User-Agent": BROWSER_UA})
            if resp.is_success:
                checks.append(CheckResult(
                    check="sitemap", layer=AccessLayer.POLICY,
                    status=CheckStatus.PASS, gating=False,
                    detail=msg("access.sitemap.fallback", url=sitemap_url),
                ))
            else:
                checks.append(CheckResult(
                    check="sitemap", layer=AccessLayer.POLICY,
                    status=CheckStatus.FAIL, gating=False,
                    detail=msg("access.sitemap.missing", url=sitemap_url, status=resp.status_code),
                ))
        except httpx.HTTPError as exc:
            checks.append(CheckResult(
                check="sitemap", layer=AccessLayer.POLICY,
                status=CheckStatus.UNKNOWN, gating=False,
                detail=msg("access.sitemap.error", error=type(exc).__name__),
            ))

    # 7b. v1 不可观测的证据层显式占位(铁律 7:不可观测就记下来,不留空白让人脑补)
    checks.extend([
        CheckResult(check="rendered_fetch", layer=AccessLayer.RENDERED_FETCH,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.rendered.not_observable")),
        CheckResult(check="official_identity", layer=AccessLayer.OFFICIAL_IDENTITY,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.official.not_observable")),
        CheckResult(check="waf_evidence", layer=AccessLayer.WAF_EVIDENCE,
                    status=CheckStatus.UNKNOWN, gating=False,
                    detail=msg("access.waf.not_observable")),
    ])

    # 8. 资格门聚合:任一 gating 检查 FAIL → FAIL;否则有 UNKNOWN → UNKNOWN;否则 PASS
    gating = [c.status for c in checks if c.gating]
    if CheckStatus.FAIL in gating:
        overall = CheckStatus.FAIL
    elif CheckStatus.UNKNOWN in gating:
        overall = CheckStatus.UNKNOWN
    else:
        overall = CheckStatus.PASS

    return AccessAuditResult(
        site=url, final_url=final_url, overall=overall,
        checks=checks, bots=probes, caveats=caveats, audited_at=datetime.now(UTC),
    )


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
