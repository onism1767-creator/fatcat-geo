"""建议生成器:只读 evidence_registry.yaml(铁律 9)。

- 只能输出注册表中存在的条目;注册表没有的建议不允许存在;
- 只针对定位到的失败阶段出建议(上游 FAIL 禁下游药方由 diagnose 层
  保证:它只会拿 primary_failure 这一个阶段来要建议);
- C 级及以下必须带「建议实验验证」字样;E 级永不输出;
- 每条建议随附证据等级、来源、禁忌症与护栏,供报告完整展示。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from diagnosis.findings import Stage
from engine.models import FrozenModel, NeedIntent

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "evidence_registry.yaml"

NEEDS_EXPERIMENT_PHRASE = "建议实验验证"
_LEVELS = ("A", "B", "C", "D", "E")
_OUTPUT_LEVELS = ("A", "B", "C", "D")  # E 级永不作为建议输出
_EXPERIMENT_LEVELS = ("C", "D")  # C 级及以下必须带实验标注


class EvidenceEntry(FrozenModel):
    id: str
    stage: Stage
    hypothesis: str
    evidence_level: str
    sources: list[str] = []
    supported_intents: list[NeedIntent] = []
    contraindications: list[str] = []
    engines_tested: list[str] = []
    expected_effect: str = "unknown_experiment_required"
    guardrail: str | None = None
    version: int = 1


class Recommendation(FrozenModel):
    evidence_id: str
    stage: Stage
    evidence_level: str
    text: str  # 假设文本;C 级及以下已含「建议实验验证」字样
    sources: list[str]
    contraindications: list[str]
    guardrail: str | None
    needs_experiment: bool


def load_evidence_registry(path: Path = DEFAULT_REGISTRY_PATH) -> list[EvidenceEntry]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = [EvidenceEntry(**raw) for raw in data["entries"]]
    ids = [entry.id for entry in entries]
    if len(ids) != len(set(ids)):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"证据注册表存在重复 ID:{duplicated}")
    for entry in entries:
        if entry.evidence_level not in _LEVELS:
            raise ValueError(f"{entry.id}: 非法证据等级 {entry.evidence_level!r}")
    return entries


def recommend(
    entries: list[EvidenceEntry],
    *,
    stage: Stage,
    intent: NeedIntent,
) -> list[Recommendation]:
    """给定【已定位的失败阶段】与需求意图,产出建议。
    调用方只允许传 primary_failure 阶段——这是"上游 FAIL 禁下游药方"的执行点。"""
    out: list[Recommendation] = []
    for entry in entries:
        if entry.stage is not stage:
            continue
        if entry.evidence_level not in _OUTPUT_LEVELS:
            continue
        if intent not in entry.supported_intents:
            continue
        needs_experiment = entry.evidence_level in _EXPERIMENT_LEVELS
        text = entry.hypothesis + (f"({NEEDS_EXPERIMENT_PHRASE})" if needs_experiment else "")
        out.append(Recommendation(
            evidence_id=entry.id,
            stage=entry.stage,
            evidence_level=entry.evidence_level,
            text=text,
            sources=entry.sources,
            contraindications=entry.contraindications,
            guardrail=entry.guardrail,
            needs_experiment=needs_experiment,
        ))
    out.sort(key=lambda rec: rec.evidence_level)  # 证据强的排前面
    return out
