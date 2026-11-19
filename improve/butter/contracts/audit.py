"""审计(对外契约 audit/1)。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from butter.contracts.common import Frozen

CheckName = Literal["meta_robots", "visible_date", "numeric_traceability", "hidden_text", "jsonld_consistency"]
FindingLevel = Literal["problem", "notice", "pass", "not_checkable"]


class AuditFinding(Frozen):
    finding_id: str
    check: CheckName
    entry_id: str  # 证据注册表条目 ID
    level: FindingLevel
    page_url: str | None = None  # None = 全站层面
    message: str
    evidence: dict[str, str] = {}  # 位置、原文片段、看到的值、对照值


class AuditReport(Frozen):
    schema_version: Literal["audit/1"] = "audit/1"
    site_url: str
    run_id: str
    generated_at: datetime
    pages_checked: list[str] = []
    findings: list[AuditFinding] = []
    limitations: list[str] = []
