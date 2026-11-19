"""测量的数据形状(设计 §5.2、§10.2)。measure/1。"""

from __future__ import annotations

from typing import Literal

from butter.contracts.cards import BatchFreeze
from butter.contracts.common import Frozen

MeasureKind = Literal["baseline", "remeasure"]
LegState = Literal["待跑", "在跑", "完成", "不完整", "中止", "失败"]


class PotatoSettings(Frozen):
    """怎么找到并调用 Potato。默认是同级文件夹里的 Potato 和它自己的解释器。"""

    potato_dir: str
    python: str                 # Potato 的解释器
    pythonpath: str = ""        # 需要时额外加的模块搜索路径(测试里指向假 Potato)


class LegEstimate(Frozen):
    lang: str
    provider: str
    model: str
    calls: int
    upper_usd: float | None = None    # Potato 自己给的保守上界(读它的 estimate 输出)
    typical_usd: float                # 按 FatCat 实测的每次花费估
    minutes: float
    judged: bool = True               # False = 没有这条腿平时的波动数据:只列数字,不判变没变


class MeasurePlan(Frozen):
    """点"测一次"之前给站长看的:要测什么、大概花多少、多久;同时带上要冻结的参数。"""

    batch_id: str
    freeze: BatchFreeze
    config_dirs: dict[str, str]       # 语言 → Potato 配置目录(绝对路径)
    legs: list[LegEstimate]
    n_questions: dict[str, int]       # 语言 → 题数
    n_treated: int
    n_control: int
    typical_usd: float
    upper_usd: float | None = None
    minutes: float
    band_pp: float | None = None      # 不改动时,前后差通常在 ± 多少个百分点内(取各腿里最宽的)
    notes: list[str] = []


class LegProgress(Frozen):
    """测量进度的一行(只追加;一条腿的当前样子取它最后一行)。"""

    at: str
    lang: str
    provider: str
    model: str
    state: LegState
    run_id: str = ""
    done: int = 0
    total: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    note: str = ""


class QuestionCount(Frozen):
    question_id: str
    ok: int                   # 成功拿到的回答数
    mention_hits: int         # 清晰提到本品牌的回答数
    citation_hits: int        # 引用了本品牌官网的回答数


class LegReading(Frozen):
    """一条腿一次测量读回来的结果:每题的数,加上这次运行的身份(比对前后是否同一套配置要用)。"""

    lang: str
    provider: str
    model: str
    run_id: str
    surface_id: str = ""
    tool_version: str = ""
    config_fingerprint: str = ""
    ok: int
    expected: int
    cost_usd: float = 0.0
    searched_share: float | None = None
    complete: bool            # 有 manifest 和提及判定,成功率够
    questions: list[QuestionCount] = []


class MeasureReading(Frozen):
    batch_id: str
    kind: MeasureKind
    day: str
    stamp: str
    legs: list[LegReading]
