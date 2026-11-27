"""再测比对(设计 §9.4)。全部是纯函数。"""

from butter.compare.core import LABEL_TEXT, METHOD_VERSION, compare_batch, label_text, plan_rules
from butter.compare.stats import jeffreys

__all__ = ["LABEL_TEXT", "METHOD_VERSION", "compare_batch", "jeffreys", "label_text", "plan_rules"]
