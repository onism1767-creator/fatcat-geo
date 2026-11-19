"""审计(对外契约 audit/2)。

audit/2 比 audit/1 多一个结果级别"不适用"(not_applicable):这项检查在这页上没有东西可查,
比如页面上没有任何日期、没有结构化数据。它不算问题,也不算通过;和"查不了"(想查但读不到)不是一回事。
旧的 audit/1 文件照样能读。只在同一个 rules_version 之间比较结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from butter.contracts.common import Frozen

CheckName = Literal["meta_robots", "visible_date", "numeric_traceability", "hidden_text", "jsonld_consistency"]
FindingLevel = Literal["problem", "notice", "pass", "not_checkable", "not_applicable"]
RULES_VERSION = "audit-rules/2"


class AuditFinding(Frozen):
    finding_id: str
    check: CheckName
    entry_id: str  # 证据注册表条目 ID
    level: FindingLevel
    page_url: str | None = None  # None = 全站层面
    message: str
    evidence: dict[str, str] = {}  # 位置、原文片段、看到的值、对照值


class AuditReport(Frozen):
    schema_version: Literal["audit/1", "audit/2"] = "audit/2"
    rules_version: str = ""  # 旧文件没有这个字段
    site_url: str
    run_id: str
    generated_at: datetime
    pages_checked: list[str] = []
    findings: list[AuditFinding] = []
    limitations: list[str] = []
