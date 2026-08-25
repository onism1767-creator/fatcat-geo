"""建议生成器:只读 evidence_registry.yaml(铁律 9)。

- 只能输出注册表中存在的条目;注册表没有的建议不允许存在;
- 只针对定位到的失败阶段出建议(上游 FAIL 禁下游药方由 diagnose 层
  保证:它只会拿 primary_failure 这一个阶段来要建议);
- C 级及以下 needs_experiment=True,渲染层必须随文标注「建议实验验证」
  (12.2 报告口径测试把守);E 级永不输出;
- v1.1 起注册表的展示字段(hypothesis/guardrail/contraindications/sources)
  为 zh/en 双语字典,语言选择发生在渲染层。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import field_validator

from diagnosis.findings import Stage
from engine.models import FrozenModel, NeedIntent

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "evidence_registry.yaml"

_LEVELS = ("A", "B", "C", "D", "E")
_OUTPUT_LEVELS = ("A", "B", "C", "D")  # E 级永不作为建议输出
_EXPERIMENT_LEVELS = ("C", "D")  # C 级及以下必须带实验标注
_REQUIRED_LANGS = {"zh", "en"}


def _require_langs(value: dict, field: str) -> None:
    if not _REQUIRED_LANGS <= set(value):
        raise ValueError(f"{field} 必须含 zh 与 en 两个语言版本,实得 {sorted(value)}")


class EvidenceEntry(FrozenModel):
    id: str
    stage: Stage
    hypothesis: dict[str, str]
    evidence_level: str
    sources: dict[str, list[str]]
    supported_intents: list[NeedIntent] = []
    contraindications: dict[str, list[str]] = {"zh": [], "en": []}
    engines_tested: list[str] = []
    expected_effect: str = "unknown_experiment_required"
    guardrail: dict[str, str] | None = None
    version: int = 1

    @field_validator("hypothesis", "sources", "contraindications")
    @classmethod
    def _localized(cls, value: dict, info) -> dict:
        _require_langs(value, info.field_name)
        return value

    @field_validator("guardrail")
    @classmethod
    def _localized_optional(cls, value: dict | None) -> dict | None:
        if value is not None:
            _require_langs(value, "guardrail")
        return value


class Recommendation(FrozenModel):
    evidence_id: str
    stage: Stage
    evidence_level: str
    hypothesis: dict[str, str]
    sources: dict[str, list[str]]
    contraindications: dict[str, list[str]]
    guardrail: dict[str, str] | None
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
    out = [
        Recommendation(
            evidence_id=entry.id,
            stage=entry.stage,
            evidence_level=entry.evidence_level,
            hypothesis=entry.hypothesis,
            sources=entry.sources,
            contraindications=entry.contraindications,
            guardrail=entry.guardrail,
            needs_experiment=entry.evidence_level in _EXPERIMENT_LEVELS,
        )
        for entry in entries
        if entry.stage is stage
        and entry.evidence_level in _OUTPUT_LEVELS
        and intent in entry.supported_intents
    ]
    out.sort(key=lambda rec: rec.evidence_level)  # 证据强的排前面
    return out
