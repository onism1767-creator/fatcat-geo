"""起草检查器(设计 §7.4)。全部是纯函数,不调模型。"""

from butter.check.lexicon import puffery_hits
from butter.check.rules import (
    RULE_VERSION, RULES, check_proposal, decide, k9_duplicates, needs_page_warning, rewrite_reasons, tidy,
)

__all__ = ["RULES", "RULE_VERSION", "check_proposal", "decide", "k9_duplicates", "needs_page_warning", "puffery_hits",
           "rewrite_reasons", "tidy"]
