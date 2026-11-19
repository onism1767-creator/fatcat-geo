"""批次、实验卡与卡片事件的数据形状(设计 §5.2、§8)。batch/1、card/1、cardevent/1。

- 事件(CardEvent)是唯一事实来源:只追加,状态全部由事件推出来(ADR-B02)。
- Batch 是登记下来的批次;BatchState、Card 是由事件推出来的当前样子,不单独存。
- 一条改动一张卡,测量按部署批次(ADR-B06):比对结果挂在批次上,同批每张卡共用。
- 内部字段里的 scoring(唯一计分的读点)只决定卡片的验证状态;界面不出现这个词(设计 §8.3)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from butter.contracts.common import Frozen
from butter.contracts.compare import CompareResult, CompareRules, LegResult, Outcome
from butter.contracts.registry import EntryKind, Stage

CardActor = Literal["owner", "system"]
CardEventType = Literal[
    "batch_created",       # 出包时建批次
    "card_created",        # 出包时建卡,一条改动一张
    "baseline_started",    # 站长点了"测一次"(改之前)
    "baseline_finished",
    "baseline_failed",
    "deployed",            # 站长点了"我已部署"
    "deploy_reverted",     # 站长撤销"我已部署"
    "remeasure_started",   # 站长点了"再测并比对"
    "remeasure_finished",  # 带比对结果
    "remeasure_failed",
    "abandoned",           # 站长放弃这一批
]
BatchPhase = Literal["已出包", "测基线中", "基线就绪", "已部署", "待再测", "再测中", "已比对", "已放弃"]
CardValidation = Literal["假设", "实验中", "单次观测"]       # 卡上只会走到"单次观测"
MethodValidation = Literal["单次观测", "已复现", "可泛化"]  # 方法(注册表条目)的状态由卡汇总
Direction = Literal["变多", "变少"]
PanelSource = Literal["potato_file", "pasted", "butter_generated"]  # 题板来源(设计 §9.2);第三种要单独标出


# ── 批次 ─────────────────────────────────────────────────────────────

class FrozenPanel(Frozen):
    """冻结的题板:一种语言一份(Potato 一个配置目录只能有一种语言)。"""

    lang: str
    path: str
    prompt_version: str
    sha256: str
    source: PanelSource


class FrozenBrands(Frozen):
    lang: str
    path: str
    sha256: str
    source: str = ""


class Surface(Frozen):
    """一条腿:语言 × 引擎。"""

    lang: str
    provider: str
    model: str
    surface_id: str = ""


class BatchPlan(Frozen):
    """采样计划里卡片要用的那部分;估费、估时等由测量模块(measure/)的 MeasurePlan 补。"""

    runs_per_question: int
    days: int = 1


class BatchFreeze(Frozen):
    """第一次测量开始时冻结的参数(设计 §8.3"测基线"一行)。之后要改就是新批次。"""

    panel: list[FrozenPanel]
    brands: list[FrozenBrands] = []
    surfaces: list[Surface]
    plan: BatchPlan
    treated_q: list[str]
    control_q: list[str] = []
    question_pages: dict[str, str] = {}  # 题 → 对上的页面(Potato 的题板里存不了,存在这里)
    rules: CompareRules                  # 主判读、判读规则与默认值快照


class Batch(Frozen):
    schema_version: Literal["batch/1"] = "batch/1"
    batch_id: str
    site_host: str
    run_id: str
    card_ids: list[str]
    # 以下在第一次测量开始时冻结(frozen 变成 True),之后不再变
    panel: list[FrozenPanel] = []
    brands: list[FrozenBrands] = []
    surfaces: list[Surface] = []
    plan: BatchPlan | None = None
    treated_q: list[str] = []
    control_q: list[str] = []
    question_pages: dict[str, str] = {}
    rules: CompareRules | None = None
    frozen: bool = False


# ── 事件 ─────────────────────────────────────────────────────────────

class CardEvent(Frozen):
    schema_version: Literal["cardevent/1"] = "cardevent/1"
    event_id: str
    at: datetime
    actor: CardActor
    batch_id: str
    card_id: str | None = None
    type: CardEventType
    payload: dict[str, Any] = {}


class CardSpec(Frozen):
    """建卡时记下的东西(card_created 的内容)。条目的阶段、等级、来源是建卡当时从注册表抄下来的。"""

    card_id: str
    batch_id: str
    change_item_id: str
    entry_id: str
    kind: str                 # 改动种类(ChangeKind)
    origin: Literal["rule", "model"] = "rule"
    target: str               # 放到站上哪里
    before: str | None = None
    after: str | None = None
    sitewide: bool = False    # 全站类改动:robots、全站 Organization JSON-LD、新事实页(设计 §8.1)
    industry: str = ""        # 行业(事实库的类别)
    entry_stage: Stage
    entry_kind: EntryKind
    entry_level: str
    entry_hypothesis: str
    entry_sources: list[str] = []


class LegSample(Frozen):
    """一条腿一次测量的摘要:卡片的"基线""修改后结果"两栏要写的数。"""

    lang: str
    provider: str
    model: str = ""
    surface_id: str = ""
    tool_version: str = ""
    config_fingerprint: str = ""
    ok: int
    expected: int
    n_questions: int = 0           # 处理组题数
    runs_per_question: int = 0
    mention_rate: float | None = None   # 处理组本品牌提及率
    citation_rate: float | None = None  # 处理组官方域名被引率(辅助)


class MeasureStart(Frozen):
    """baseline_started / remeasure_started 的内容。第一次测量必须带冻结参数;之后可以不带,带了就必须一样。"""

    freeze: BatchFreeze | None = None


class MeasureDone(Frozen):
    """baseline_finished / remeasure_finished 的内容。再测完带比对结果;没有基线时没有可比的,result 留空。"""

    day: str  # 采样日期 YYYY-MM-DD
    legs: list[LegSample]
    result: CompareResult | None = None


class MeasureFailed(Frozen):
    reason: str


class DeployMark(Frozen):
    """站长勾选实际部署的条目,填日期。"""

    day: str
    card_ids: list[str]


class OwnerNote(Frozen):
    """deploy_reverted / abandoned 的内容。"""

    reason: str = ""


class CardDecision(Frozen):
    """一个动作交给状态机后的结果:接受(带要追加的事件),或拒绝(带原因)。不抛异常。"""

    ok: bool
    event: CardEvent | None = None
    reason: str = ""
    notes: list[str] = []  # 接受了但要提醒的事,例如"基线无效:只看时序"


class SkippedEvent(Frozen):
    """重放日志时不合规的事件:跳过,记下原因。正常情况下不会有(写入前都校验过)。"""

    seq: int
    event_id: str
    type: str
    reason: str


# ── 由事件推出来的当前样子 ───────────────────────────────────────────

class Measurement(Frozen):
    day: str
    legs: list[LegSample]
    complete: bool  # 每条腿成功率都够


class Deployment(Frozen):
    day: str
    card_ids: list[str]
    at: datetime
    baseline_valid: bool
    baseline_note: str = ""


class Reading(Frozen):
    """一次再测。按时间追加,不覆盖。scoring = 过了建议间隔后的第一次,只有它决定卡片的验证状态。"""

    day: str
    days_since_deploy: int
    scoring: bool
    samples: list[LegSample]
    legs: list[LegResult] = []   # 比对结果;没有基线时为空
    lines: list[str] = []        # 每腿一句:前后差、区间、波动范围、标签
    judged: int = 0
    exceeded: int = 0
    errors: list[str] = []


class BatchState(Frozen):
    batch: Batch
    phase: BatchPhase
    created_at: datetime
    updated_at: datetime
    cards_created: int
    baseline: Measurement | None = None   # 当前基线 = 最后一次测完的
    baselines_done: int = 0
    measure_failures: list[str] = []
    deploy: Deployment | None = None
    readings: list[Reading] = []
    scored: bool = False
    recommended_from: str | None = None   # 部署日 + 建议间隔;只是内部推算,Butter 不提醒、不催
    deployed_entries: list[str] = []
    single_entry: bool = False            # 部署的改动都挂同一个条目
    sitewide: bool = False                # 部署的改动里有全站类的
    outcome: Outcome | None = None
    outcome_reason: str = ""
    abandoned_reason: str = ""


class Intervention(Frozen):
    kind: str
    target: str
    before: str | None = None
    after: str | None = None


class EvidenceRef(Frozen):
    entry_id: str
    level: str
    sources: list[str] = []


class Conditions(Frozen):
    """测试条件(§20.4 第 5 栏)。"""

    surfaces: list[Surface] = []
    tool_versions: list[str] = []
    panel: list[FrozenPanel] = []
    page: str = ""
    industry: str = ""
    sample_days: list[str] = []


class ControlInfo(Frozen):
    questions: list[str] = []
    pages: list[str] = []
    valid: bool = False
    note: str = ""


class Card(Frozen):
    """一张实验卡的当前样子。产品文档 §20.4 的 11 个字段按设计 §8.2 由事件推出。"""

    schema_version: Literal["card/1"] = "card/1"
    card_id: str
    batch_id: str
    change_item_id: str
    entry_id: str
    deployed: bool
    phase: BatchPhase
    # §20.4 的 11 个字段
    intervention: Intervention            # 1 干预措施
    target_stage: str                     # 2 目标阶段
    hypothesis: str                       # 3 假设
    evidence: EvidenceRef                 # 4 依据
    test_conditions: Conditions           # 5 测试条件
    baseline: list[LegSample] = []        # 6 基线
    post_change: list[LegSample] = []     # 7 修改后结果(决定验证状态的那次;还没有就取最近一次)
    control: ControlInfo                  # 8 对照
    observed_effect: list[Reading] = []   # 9 观测到的效果(每次再测一条,照实写)
    limitations: list[str] = []           # 10 局限
    validation: CardValidation            # 11 验证状态
    direction: Direction | None = None    #    单次观测的方向:变多 / 变少
    validation_note: str = ""             #    例:"同批 3 条"、"无可辨变化"
    outcome: Outcome | None = None        # 卡片结局(won / lost / inconclusive / dnf)
    outcome_reason: str = ""
    batch_deployed_cards: int = 0         # 同批一起部署了几条


# ── 方法(注册表条目)级汇总 ─────────────────────────────────────────

class MethodExperiment(Frozen):
    """一次实验 = 一个单条目批次的那次计分再测。同批几张卡算一次,不重复数。"""

    batch_id: str
    site_host: str
    industry: str = ""
    direction: Direction
    baseline_day: str
    remeasure_day: str
    card_ids: list[str]
    butter_panel: bool = False  # 题是 Butter 自己出的。照常算一次(2026-11-18 用户定);提案的表里只标出题从哪来
    legs: list[str] = []        # 哪几条腿超出了波动
    note: str = ""              # 没单独计数时写原因


class MethodDirection(Frozen):
    direction: Direction
    state: MethodValidation | None = None
    independent: list[MethodExperiment] = []      # 计数的独立实验
    not_independent: list[MethodExperiment] = []  # 和上面某次同站且时间重叠,照实列出但不另计
    cards: int = 0                                # 这个方向"单次观测"的卡数


class MethodSummary(Frozen):
    """站长放弃的批次不在这里:不计数,也不列出(2026-11-18 用户定)。事件日志里仍有记录。"""

    entry_id: str
    more: MethodDirection
    fewer: MethodDirection
    cards_total: int = 0
    cards_hypothesis: int = 0
    cards_running: int = 0
    same_batch_more: list[str] = []   # 多条目批次里超出波动上升的卡:照写,不计数
    same_batch_fewer: list[str] = []


class CardsView(Frozen):
    """整个卡片库的当前样子。"""

    batches: list[BatchState] = []
    cards: list[Card] = []
    methods: list[MethodSummary] = []
    skipped: list[SkippedEvent] = []
    events: int = 0
