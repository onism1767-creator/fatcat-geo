"""RETRIEVAL 阶段:仪器不暴露检索链路时的结论(5.2)。

检索阶段只认本仪器自己返回的检索来源列表(有则走 generative_retrieval);
不暴露 → NOT_OBSERVABLE,如实说,不用引用反推、不从别处借数据(铁律 7;产品文档 15.3)。
输出为结论码 + 参数(v1.1),组句在渲染层。
"""

from __future__ import annotations

from diagnosis.findings import Stage, StageFinding, StageStatus, msg


def retrieval_not_observable() -> StageFinding:
    return StageFinding(
        stage=Stage.RETRIEVAL,
        status=StageStatus.NOT_OBSERVABLE,
        headline=msg("retrieval.headline.not_observable"),
        evidence_lines=[msg("retrieval.not_observable_note")],
    )
