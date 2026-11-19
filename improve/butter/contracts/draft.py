"""起草与检查的数据形状(设计 §5.2、§7)。draft/1、check/1。"""

from __future__ import annotations

from typing import Literal

from butter.contracts.common import Frozen

# 起草器只许写这几个字段(K7 白名单;全部是加法,决定 3a)
DraftField = Literal["title", "h1", "h2", "meta_description", "fact_block_lead", "answer_first"]
DRAFT_FIELDS: tuple[str, ...] = ("title", "h1", "h2", "meta_description", "fact_block_lead", "answer_first")
RuleId = Literal["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9"]
CheckOutcome = Literal["通过", "作废", "警告"]
# 不过时怎么办:rewrite 带着原因让模型重写一次,再不过作废;void 直接作废;warn 留给站长
OnFail = Literal["rewrite", "void", "warn"]
Verdict = Literal["accept", "rewrite", "void"]


class DraftFact(Frozen):
    """允许起草器使用的一条事实。"""

    fact_id: str
    value: str
    source_url: str


class DraftLimits(Frozen):
    title_max_chars: int = 60
    meta_max_chars: int = 160
    sentence_max_words: int = 30   # 英文一句最多几个词
    sentence_max_chars: int = 40   # 中文一句最多几个字


class DraftTask(Frozen):
    task_id: str
    page_url: str
    lang: str
    fields: list[DraftField]
    current: dict[str, str] = {}      # 各字段原值
    question_ids: list[str] = []
    questions: dict[str, str] = {}    # 题号 → 题目原文(只带对上本页、最贴的那一道)
    facts: list[DraftFact] = []
    limits: DraftLimits = DraftLimits()


class DraftProposal(Frozen):
    task_id: str
    field: str            # 模型给什么就是什么,K7 查它在不在白名单里
    text: str
    facts_cited: list[str] = []   # 模型自报,检查器不采信
    rationale: str = ""


class DraftAttempt(Frozen):
    task_id: str = ""
    provider: str
    model: str
    prompt_version: str
    request_hash: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    cached: bool = False
    error: str = ""
    reason: Literal["首次", "格式重问", "检查后重写"] = "首次"


class CheckContext(Frozen):
    """检查一条草稿要对照的东西。全部来自事实库和原页面,不来自模型。"""

    page_url: str
    page_lang: str | None = None
    page_text: str = ""                 # 该页原有的可见文字(标题、H1、H2、meta、正文)
    facts: list[DraftFact] = []         # 允许使用的事实
    own_names: list[str] = []           # 站点自己的名称、别名、商品名、货号、规格值
    competitor_names: list[str] = []    # Potato 品牌表里别家的名字和别名:一律不许出现
    trusted_dates: list[str] = []       # 事实库里可信的日期(ISO:YYYY-MM-DD 或 YYYY-MM)
    sourced_claims: list[str] = []      # 事实库里带来源的对比主张原文
    limits: DraftLimits = DraftLimits()


class CheckResult(Frozen):
    rule: RuleId
    rule_version: str
    entry_id: str | None = None
    outcome: CheckOutcome
    on_fail: OnFail
    detail: str = ""
    span: tuple[int, int] | None = None   # 在草稿文字里的位置


class DraftedItem(Frozen):
    """过了检查的一条草稿。"""

    task_id: str
    page_url: str
    field: DraftField
    text: str                       # 收拾过空白的文字
    before: str = ""                # 这个位置原来的文字(供还原);新增的位置是空
    rewritten: bool = False         # 是不是检查后重写过一次的稿
    checks: list[CheckResult] = []  # 含通过的和警告的
    question_id: str | None = None  # 首屏短答、标题备选回应的是哪道题


class VoidedDraft(Frozen):
    task_id: str
    page_url: str
    field: str
    reasons: list[str]


class DraftOutcome(Frozen):
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    items: list[DraftedItem] = []
    voided: list[VoidedDraft] = []
    attempts: list[DraftAttempt] = []
    skipped: list[str] = []          # 没起草的页面和原因(人话)
    page_warnings: list[str] = []    # 该页可能含针对模型的指令
    stopped: str = ""                # 中途停下的原因(账户问题、到了费用上限);空 = 走完了
    cost_usd: float = 0.0
    calls: int = 0                   # 真发出去的请求数(不含命中缓存的)
