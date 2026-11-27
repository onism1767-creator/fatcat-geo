"""批次与实验卡(设计 §8;ADR-B02、ADR-B06)。

- `machine`:事件与状态机(纯层)。合法的记进账本,非法的拒绝并说明原因,不抛异常。
- `judge`:卡片结局与验证状态(§8.5、§9.4 总表)。
- `project`:账本 → 批次与卡片的当前样子(§20.4 的 11 个字段)。
- `methods`:按证据条目汇总,变多、变少分开数,找出两次独立同向的条目。
- `build`:出包时建批次和卡。
- `index`:算出要写进 SQLite 索引的行。
- `service`:薄外壳,经 store.py 落盘(事件日志只追加;索引可整份重建)。
"""

from butter.cards.build import SITEWIDE_KINDS, card_spec, plan_batch
from butter.cards.index import index_tables
from butter.cards.judge import card_status, judge_batch, judge_reading
from butter.cards.machine import Ledger, apply, check, decide, draft, fold
from butter.cards.methods import split_independent, summarize_methods
from butter.cards.project import batch_state, card_view, project
from butter.cards.service import load, rebuild, submit

__all__ = [
    "SITEWIDE_KINDS", "Ledger", "apply", "batch_state", "card_spec", "card_status", "card_view", "check", "decide",
    "draft", "fold", "index_tables", "judge_batch", "judge_reading", "load", "plan_batch", "project", "rebuild",
    "split_independent", "submit", "summarize_methods",
]
