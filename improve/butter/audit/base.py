"""审计发现的小构造器。finding_id 由 run_audit 统一编号。"""

from __future__ import annotations

from butter.contracts import AuditFinding


def finding(check: str, entry_id: str, level: str, page_url: str | None, message: str, **evidence: str) -> AuditFinding:
    return AuditFinding(finding_id="", check=check, entry_id=entry_id, level=level, page_url=page_url,  # type: ignore[arg-type]
                        message=message, evidence={k: str(v) for k, v in evidence.items() if v not in (None, "")})
