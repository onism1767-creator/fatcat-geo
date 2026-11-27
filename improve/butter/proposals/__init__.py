"""给 Toast 的提案文档(决定 4、决定 7;设计 §8.5)。纯层:只出 Markdown 文本,由调用方落盘。

Butter 不写 Toast 的任何文件。提案只是文档,由人看过再合并。
"""

from butter.proposals.notes import AMENDMENTS, WHY_ADD
from butter.proposals.render import (
    AMENDMENT_FILE, EXTENSION_FILE, README_FILE, REPLICATION_FILE, TODO, amendment_proposal, build_proposals,
    extension_proposal, replicated, replication_proposal, resolve_toast_id,
)

__all__ = [
    "AMENDMENTS", "AMENDMENT_FILE", "EXTENSION_FILE", "README_FILE", "REPLICATION_FILE", "TODO", "WHY_ADD",
    "amendment_proposal", "build_proposals", "extension_proposal", "replicated", "replication_proposal",
    "resolve_toast_id",
]
