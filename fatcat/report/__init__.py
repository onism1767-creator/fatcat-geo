"""一份长报告:把各步骤的结果拼成 12 章,同样的内容只出现一次。"""

from fatcat.report.build import Sources, build, merge_advice, site_mismatch, stamp_now
from fatcat.report.html import render

__all__ = ["Sources", "build", "merge_advice", "render", "site_mismatch", "stamp_now"]
