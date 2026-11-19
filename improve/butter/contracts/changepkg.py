"""改动包(对外契约 changepkg/1)。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from butter.contracts.common import Frozen
from butter.contracts.draft import CheckResult

ChangeKind = Literal["robots", "jsonld_organization", "jsonld_product", "fact_page", "fact_block", "heading_suggestion",
                     "meta_description", "fact_block_lead", "answer_first"]
ChangeAction = Literal["add", "replace", "none", "review"]
# 本窗口内任何方法的验证态最高为「假设」;技术修复类只说官方文档要求。不得出现「有效」。
ValidationState = Literal["官方文档要求,必要非充分", "假设"]


class FactRef(Frozen):
    field: str  # 如 entity.name、products[acme-kettle].price
    value: str
    source_url: str


class ChangeItem(Frozen):
    item_id: str
    kind: ChangeKind
    entry_id: str
    action: ChangeAction
    target: str  # 放到站上哪里
    files: list[str] = []  # 改动包内的相对路径
    rationale: str
    facts_used: list[FactRef] = []
    validation_state: ValidationState
    needs_human: list[str] = []  # 需要站长自己判断的事
    # 以下是第 3 周加的(只加字段,版本号不变;读的一方容忍缺字段)
    origin: Literal["rule", "model"] = "rule"
    before: str | None = None    # 原值,供还原
    after: str | None = None     # 建议的新值
    checks: list[CheckResult] = []  # 模型起草的条目过检查的结果


class NotGenerated(Frozen):
    what: str
    reason: str


class ChangePackage(Frozen):
    schema_version: Literal["changepkg/1"] = "changepkg/1"
    site_url: str
    run_id: str
    generated_at: datetime
    items: list[ChangeItem] = []
    not_generated: list[NotGenerated] = []


class PackageBundle(Frozen):
    """生成器的全部输出:清单 + 文件内容(相对路径 → 文本)。由 store 落盘。"""

    package: ChangePackage
    files: dict[str, str] = {}
