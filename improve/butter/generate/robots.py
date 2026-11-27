"""robots 三段式:搜索 / 引用类放行、训练类独立开关、用户触发类说明。

怎么保留原规则:
- 原文件一字不动放在前面,Butter 的三段追加在后面。
- 一个爬虫被单列一组后只看自己那组、不再看 * 组(RFC 9309)。所以给它单列一组时,把 * 组原有的规则原样复制进去,
  免得意外放开站长原来挡住的路径。
- 原文件已经点名写了规则的 UA 不动;点名屏蔽的只提示,由站长决定。
- 读不到现有 robots.txt 就不生成,免得覆盖。
只出文件,不出判定:放行与否的判定是 Toast 的事。

追加段的注释一律用 ASCII 英文:这个文件要放到别人的服务器上,各种 robots 解析器都要读得动。
中文说明在改动包的 README 与 reference/ua-table.md 里。
"""

from __future__ import annotations

from butter.contracts import ChangeItem, NotGenerated, RobotsFetch, UaEntry
from butter.extract.robots import RobotsParsed, RobotsRule, blocks_everything, parse_robots
from butter.registry.ids import ACCESS_ROBOTS

MARK = "[Butter]"
FILE = "robots/robots.txt"
BLOCK_FILE = "robots/butter-block.txt"
STAR = '"User-agent: *"'


def _status(parsed: RobotsParsed, token: str) -> str:
    groups = parsed.groups_for(token)
    if not groups:
        return "inherits"
    rules = [r for g in groups for r in g.rules]
    return "named_blocked" if blocks_everything(rules) else "named"


def _mirror(star_rules: list[RobotsRule], star_blocks: bool) -> list[str]:
    if not star_rules or star_blocks:
        return ["Allow: /"]
    return [r.raw.split("#", 1)[0].strip() for r in star_rules]


def _doc_lines(entries: list[UaEntry]) -> list[str]:
    width = max((len(e.robots_token) for e in entries), default=0)
    return [f"#   {e.robots_token.ljust(width)}  {e.docs_url}  (checked {e.checked_on})" for e in entries]


def _names(entries: list[UaEntry]) -> str:
    return ", ".join(e.robots_token for e in entries)


def build_robots(
    robots: RobotsFetch, ua_table: tuple[UaEntry, ...] | list[UaEntry], *, training: str,
    sitemap_urls: list[str], today: str,
) -> tuple[dict[str, str], list[ChangeItem], list[NotGenerated]]:
    if robots.state == "unreadable":
        return {}, [], [NotGenerated(
            what="robots.txt 三段式",
            reason=f"没能读到现有的 robots.txt({robots.note or robots.status}),为免覆盖你原有的规则,没有生成。")]

    original = robots.text if robots.state == "ok" else ""
    parsed = parse_robots(original)
    star_rules = parsed.star_rules()
    star_blocks = bool(star_rules) and blocks_everything(star_rules)
    mirrored = _mirror(star_rules, star_blocks)
    copied = bool(star_rules) and not star_blocks

    by_purpose: dict[str, list[UaEntry]] = {"search_index": [], "training": [], "user_fetch": []}
    for entry in ua_table:
        by_purpose["training" if entry.purpose == "ai_uses_token" else entry.purpose].append(entry)
    status = {e.robots_token: _status(parsed, e.robots_token) for e in ua_table}

    def split(entries: list[UaEntry]) -> tuple[list[UaEntry], list[UaEntry], list[UaEntry]]:
        return ([e for e in entries if status[e.robots_token] == "inherits"],
                [e for e in entries if status[e.robots_token] == "named"],
                [e for e in entries if status[e.robots_token] == "named_blocked"])

    lines: list[str] = [
        f"# ==== {MARK} Added by Butter (FatCat GEO Improve) on {today}. ====",
        "# Everything above this line is your original file, unchanged.",
        "# Allowing a crawler only makes pages reachable; it promises no ranking and no citations.",
        "# Only vendors with official crawler documentation are listed here.",
        "",
    ]
    needs_human: list[str] = []
    added_rules = False

    def named_notes(named: list[UaEntry], blocked: list[UaEntry]) -> None:
        if named:
            lines.append(f"# Already named in your original file, left as is: {_names(named)}")
        if blocked:
            lines.append(f"# Named AND blocked in your original file, left as is (your call): {_names(blocked)}")

    def copy_note() -> None:
        if copied:
            lines.append(f"# The {len(mirrored)} rule(s) below are copied from your {STAR} group. A crawler that has its own")
            lines.append("# group ignores the * group, so without the copy it would gain access to paths you block.")
        elif star_blocks:
            lines.append(f"# Your {STAR} group blocks the whole site. This group allows the whole site for these")
            lines.append("# crawlers. Add Disallow lines here for any path you do not want fetched.")

    # ── 第 1 段:搜索 / 引用类 ──
    add, named, blocked = split(by_purpose["search_index"])
    lines += [f"# -- {MARK} Section 1: search / citation crawlers (allowed) --",
              "# Blocking one of these removes the site from that product's AI search results (vendor docs).",
              "# Official docs (date checked):"]
    lines += _doc_lines(by_purpose["search_index"])
    named_notes(named, blocked)
    if add:
        copy_note()
        lines += [f"User-agent: {e.robots_token}" for e in add] + mirrored
        added_rules = True
    else:
        lines.append("# Every crawler in this section is already named in your original file; nothing added.")
    lines.append("")
    if blocked:
        needs_human.append(f"原文件点名屏蔽了搜索 / 引用类爬虫 {'、'.join(e.robots_token for e in blocked)},Butter 没有改。"
                           "如果这不是有意的(例如 CDN 代管的 robots.txt 默认加的),需要你自己删掉对应的屏蔽规则。")

    # ── 第 2 段:训练类 ──
    add, named, blocked = split(by_purpose["training"])
    switch = "退出训练" if training == "optout" else "不改动"
    switch_en = "opt out" if training == "optout" else "no change"
    lines += [f"# -- {MARK} Section 2: training crawlers and training-use tokens (separate switch; current: {switch_en}) --",
              "# Whether your content may be used for model training is your decision. It is independent of Section 1.",
              "# Official docs (date checked):"]
    lines += _doc_lines(by_purpose["training"])
    named_notes(named, blocked)
    if not add:
        lines.append("# Every crawler in this section is already named in your original file; nothing added.")
    elif training == "optout":
        lines += [f"User-agent: {e.robots_token}" for e in add] + ["Disallow: /"]
        added_rules = True
    else:
        lines.append("# No change: these crawlers keep following your existing rules.")
        lines.append('# To opt out of training, remove the leading "# " from the lines below:')
        lines += [f"# User-agent: {e.robots_token}" for e in add] + ["# Disallow: /"]
    lines.append("")

    # ── 第 3 段:用户触发类 ──
    add, named, blocked = split(by_purpose["user_fetch"])
    lines += [f"# -- {MARK} Section 3: user-triggered fetchers (explanation) --",
              "# These fetch a page when a user pastes your URL into a chat or asks the AI to read it.",
              "# Several vendors state that such fetches may not follow robots.txt (see each vendor's docs).",
              "# For those, what matters is that the first HTML response already contains the main content.",
              "# Official docs (date checked):"]
    lines += _doc_lines(by_purpose["user_fetch"])
    named_notes(named, blocked)
    if add:
        lines.append("# For the vendors that do follow robots.txt, the rules are handled the same way as Section 1:")
        copy_note()
        lines += [f"User-agent: {e.robots_token}" for e in add] + mirrored
        added_rules = True
    lines.append("")
    if blocked:
        needs_human.append(f"原文件点名屏蔽了用户触发类抓取器 {'、'.join(e.robots_token for e in blocked)},Butter 没有改。")

    # ── sitemap ──
    added_sitemaps = [u for u in sitemap_urls if not parsed.sitemaps]
    if added_sitemaps:
        lines.append(f"# {MARK} Your original file declares no sitemap. This one was read successfully during this run:")
        lines += [f"Sitemap: {u}" for u in added_sitemaps[:3]]
        lines.append("")

    if star_blocks:
        needs_human.append("原文件的 * 组把整站挡住了。改动包对搜索 / 引用类和用户触发类爬虫放行了整站,这是实质改动:"
                           "请确认没有不想公开的路径,有的话在对应组里加 Disallow。")
    if copied:
        needs_human.append("追加的组复制了 * 组现有的路径规则(爬虫被单列一组后不再看 * 组,不复制就会放开你原来挡住的路径);"
                           "以后改 * 组的规则时,记得把这几组一起改。")
    needs_human.append("各家官方对「用户触发类抓取器是否遵守 robots.txt」的说法见 reference/ua-table.md;"
                       "其中几家明说可能不遵守,这类访问靠的是页面第一次返回的 HTML 里就有主要内容。")
    needs_human.append("CDN / 防火墙层的拦截不写在 robots.txt 里,robots.txt 放行了也可能被那一层挡住,需要另查。")

    if not added_rules and not added_sitemaps:
        return {}, [ChangeItem(
            item_id="robots", kind="robots", entry_id=ACCESS_ROBOTS, action="none", target="/robots.txt(站点根目录)",
            rationale="UA 表里的爬虫在现有 robots.txt 里都已点名,训练开关为「不改动」,没有需要追加的规则。",
            validation_state="官方文档要求,必要非充分", needs_human=needs_human)], []

    block = "\n".join(lines).rstrip() + "\n"
    prefix = original if original.endswith("\n") or not original else original + "\n"
    full = (prefix + "\n" if prefix else "") + block
    summary = {
        "ok": "现有 robots.txt 原样保留在文件前半,后面追加 Butter 的三段。",
        "missing": "站上现在没有 robots.txt(或那个地址返回的不是规则文件),这是一份新文件。",
    }[robots.state]
    item = ChangeItem(
        item_id="robots", kind="robots", entry_id=ACCESS_ROBOTS,
        action="replace" if robots.state == "ok" else "add", target="/robots.txt(站点根目录)",
        files=[FILE, BLOCK_FILE],
        rationale=f"{summary}第 1 段给有官方文档的搜索 / 引用类爬虫单列一组并放行;第 2 段是训练类爬虫的独立开关(当前:{switch});"
                  "第 3 段说明用户触发类抓取器。每个 UA 的官方文档地址与核对日期写在文件注释里(注释用英文,方便各种解析器读)。"
                  f"{BLOCK_FILE} 是只含追加部分的版本,方便直接贴到现有文件末尾。",
        validation_state="官方文档要求,必要非充分", needs_human=needs_human)
    return {FILE: full, BLOCK_FILE: block}, [item], []
