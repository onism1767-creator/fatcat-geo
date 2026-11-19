"""再测比对与默认值的数据形状(设计 §5.2、§9.4、§11)。compare/1、defaults/1。"""

from __future__ import annotations

from typing import Any, Literal

from butter.contracts.common import Frozen

Metric = Literal["mention", "citation"]
DefaultState = Literal["占位", "单次观测", "临时", "已复现"]
Primary = Literal["pp", "did"]  # 前后差 / 差中差
Outcome = Literal["won", "lost", "inconclusive", "dnf"]
LabelCode = Literal[
    "not_remeasurable",   # 1 国内平台
    "not_deployed",       # 2 站长没部署
    "config_changed",     # 3 配置变了
    "incomplete",         # 4 测量不完整
    "baseline_invalid",   # 5 基线无效,只看时序
    "not_comparable",     # 6 样本不够 / 该腿不比 / 没有这条腿的波动数据
    "drift",              # 8 同期漂移
    "up",                 # 9 超出波动上升
    "down",               # 10 超出波动下降
    "within",             # 11 在波动内
]


class DefaultValue(Frozen):
    key: str
    value: Any
    unit: str
    source: str
    source_plain: str  # 界面上那句人话来源;界面只用这一句
    measured_on: str
    validation_state: DefaultState
    note: str = ""


class NoiseTier(Frozen):
    """一条腿、一个指标、一个间隔的「不改动时的波动」:σ 单题前后差的标准差,τ 所有题一起漂的部分(都是比例)。"""

    metric: Metric
    lang: str
    provider: str
    days: int
    sigma: float
    tau: float


class Thresholds(Frozen):
    min_treated: int
    min_control: int
    min_success_rate: float
    baseline_valid_days: int
    citation_min_searched_share: float
    bootstrap_draws: int
    seed: int
    z: float
    recommended_gap_days: int


class LegRule(Frozen):
    """测基线时就定下的:这条腿这个指标用哪个主判读,以及事先给站长看的波动范围。"""

    lang: str
    provider: str
    metric: Metric
    n_T: int
    n_C: int
    control_valid: bool
    primary: Primary | None  # None = 没有这条腿的波动数据
    tier_days: int | None
    band_pp: float | None
    band_did: float | None


class CompareRules(Frozen):
    method_version: str
    thresholds: Thresholds
    noise: list[NoiseTier]
    legs: list[LegRule]
    defaults_used: list[DefaultValue] = []


class QuestionObs(Frozen):
    question_id: str
    pre_hits: int
    pre_n: int
    post_hits: int
    post_n: int


class LegData(Frozen):
    lang: str
    provider: str
    metric: Metric
    questions: list[QuestionObs]
    pre_ok: int
    pre_expected: int
    post_ok: int
    post_expected: int
    pre_searched_share: float | None = None  # 基线里搜索过的回答占比;被引率要用


class BatchFacts(Frozen):
    batch_id: str
    treated_q: list[str]
    control_q: list[str]
    baseline_day: str   # ISO 日期
    remeasure_day: str
    deploy_day: str | None = None
    deployed: bool = True
    domestic: bool = False
    sitewide: bool = False
    config_mismatch: list[str] = []  # 哪些配置前后不一致;空 = 一致


class LegResult(Frozen):
    lang: str
    provider: str
    metric: Metric
    n_T: int = 0
    n_C: int = 0
    pre_T: float | None = None
    post_T: float | None = None
    d_T: float | None = None
    ci_T: tuple[float, float] | None = None
    band_T: float | None = None
    d_C: float | None = None
    band_C: float | None = None
    did: float | None = None
    ci_did: tuple[float, float] | None = None
    band_did: float | None = None
    primary: Primary | None = None
    tier_days: int | None = None
    gap_days: int | None = None
    comparable: bool = False
    reason: str = ""
    label: LabelCode
    no_control: bool = False  # 第 7 行:标签后加"(无有效对照)"
    outcome: Outcome | None = None


class CompareResult(Frozen):
    batch_id: str
    method_version: str
    legs: list[LegResult]
    judged: int = 0     # 判了"上升 / 下降 / 在波动内"的腿数
    exceeded: int = 0   # 其中超出波动的腿数
    defaults_used: list[DefaultValue] = []
    errors: list[str] = []
