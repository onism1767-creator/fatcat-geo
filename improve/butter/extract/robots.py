"""robots.txt 解析(RFC 9309 的分组与最长匹配)。保留原始行,供生成器原样带回。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RobotsRule:
    directive: str  # 小写:allow / disallow / crawl-delay / 其他
    value: str
    raw: str  # 原始行(去掉行尾换行)


@dataclass
class RobotsGroup:
    user_agents: list[str] = field(default_factory=list)  # 原样大小写
    rules: list[RobotsRule] = field(default_factory=list)

    def tokens(self) -> set[str]:
        return {ua.lower() for ua in self.user_agents}


@dataclass
class RobotsParsed:
    groups: list[RobotsGroup] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    def groups_for(self, token: str) -> list[RobotsGroup]:
        """点名这个令牌的所有组(同一令牌可以出现在多组,RFC 9309 要求合并)。"""
        token = token.lower()
        return [g for g in self.groups if token in g.tokens()]

    def star_rules(self) -> list[RobotsRule]:
        return [rule for g in self.groups_for("*") for rule in g.rules]


_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z\-]*)\s*:\s*(.*?)\s*$")


def parse_robots(text: str) -> RobotsParsed:
    parsed = RobotsParsed()
    current: RobotsGroup | None = None
    expecting_agents = False  # 连续的 User-agent 行属于同一组
    for raw in text.splitlines():
        body = raw.split("#", 1)[0]
        match = _LINE.match(body)
        if not match:
            continue
        directive, value = match.group(1).lower(), match.group(2)
        if directive == "user-agent":
            if current is None or not expecting_agents:
                current = RobotsGroup()
                parsed.groups.append(current)
                expecting_agents = True
            current.user_agents.append(value)
        elif directive == "sitemap":
            if value:
                parsed.sitemaps.append(value)
        else:
            expecting_agents = False
            if current is not None:
                current.rules.append(RobotsRule(directive, value, raw.rstrip()))
    return parsed


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in core)
    return re.compile(regex + ("$" if anchored else ""))


def rules_allow(rules: list[RobotsRule], path: str) -> bool:
    """按最长匹配判定;同长时 Allow 胜。没有规则命中 = 允许。"""
    best_len, allowed = -1, True
    for rule in rules:
        if rule.directive not in ("allow", "disallow") or not rule.value:
            continue
        if _pattern_to_regex(rule.value).match(path):
            length = len(rule.value)
            if length > best_len or (length == best_len and rule.directive == "allow"):
                best_len, allowed = length, rule.directive == "allow"
    return allowed


def blocks_everything(rules: list[RobotsRule]) -> bool:
    """这组规则是否把整站挡掉(根路径不允许)。"""
    return not rules_allow(rules, "/")
