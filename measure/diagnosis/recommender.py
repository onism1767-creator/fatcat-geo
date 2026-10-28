"""建议生成器:只读 evidence_registry.yaml(铁律 9)。

- 只能输出注册表中存在的条目;注册表没有的建议不允许存在;
- 只针对定位到的失败阶段出建议(上游 FAIL 禁下游药方由 diagnose 层
  保证:它只会拿 primary_failure 这一个阶段来要建议);
- 每条必带 kind(硬边界 9,2026-10-28 落地):technical_fix = 修网站的技术活,注明出处直接建议;
  geo_method = GEO 优化方法,一律写成实验卡(猜想 / 依据 / 怎么验证),不得称有效——needs_experiment 对它恒为 True;
  技术修复类只在 C 级及以下才标「建议实验验证」;E 级永不输出;
- v1.1 起注册表的展示字段(hypothesis/guardrail/contraindications/sources)
  为 zh/en 双语字典,语言选择发生在渲染层;
- v0.4.14 起每条必带 plain(人话版,只换说法不换主张):用户视图读 plain,
  「为什么这么建议」与审计视图读 hypothesis 原句;
- v0.4.17 起每条必带 verify(做完之后怎么检查效果,人话),报告建议表第二列。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import field_validator

from diagnosis.findings import Stage
from engine.models import FrozenModel, PromptIntent

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "evidence_registry.yaml"

_LEVELS = ("A", "B", "C", "D", "E")
_OUTPUT_LEVELS = ("A", "B", "C", "D")  # E 级永不作为建议输出
_EXPERIMENT_LEVELS = ("C", "D")  # 技术修复类 C 级及以下必须带实验标注;GEO 方法不看等级,一律实验卡
GEO_METHOD = "geo_method"
_REQUIRED_LANGS = {"zh", "en"}


def _require_langs(value: dict, field: str) -> None:
    if not _REQUIRED_LANGS <= set(value):
        raise ValueError(f"{field} 必须含 zh 与 en 两个语言版本,实得 {sorted(value)}")


class EvidenceEntry(FrozenModel):
    id: str
    stage: Stage
    kind: Literal["technical_fix", "geo_method"]  # 技术修复:直接建议;GEO 方法:实验卡(硬边界 9)
    channel: str = "own"  # own = 官网线 / third_party = 第三方线 / answer = 回答文字(架构书附录 v0.4.24 D9)
    # ACCESS 条目对应官网审计里的哪几项检查(check 名):只在那几项 FAIL 时给出——只是 robots 挂了,
    # 不该把 CDN、预渲染的建议整批端上(审核 2026-10-27 黄 14);空 = 不按检查项过滤
    access_checks: list[str] = []
    hypothesis: dict[str, str]
    plain: dict[str, str]  # 人话版(v0.4.14)
    verify: dict[str, str]  # 做完之后怎么检查效果(v0.4.17)
    evidence_level: str
    sources: dict[str, list[str]]
    supported_intents: list[PromptIntent] = []
    contraindications: dict[str, list[str]] = {"zh": [], "en": []}
    engines_tested: list[str] = []
    expected_effect: str = "unknown_experiment_required"
    guardrail: dict[str, str] | None = None
    version: int = 1

    @field_validator("hypothesis", "plain", "verify", "sources", "contraindications")
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
    kind: str  # technical_fix / geo_method:渲染层据此选「直接建议」或「实验卡」
    evidence_level: str
    hypothesis: dict[str, str]
    plain: dict[str, str]
    verify: dict[str, str]
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
    intent: PromptIntent,
    channel: str = "own",
    access_checks: frozenset[str] | None = None,
) -> list[Recommendation]:
    """给定【已定位的失败阶段】、需求意图与线(官网 / 第三方),产出建议。
    调用方只允许传该线的 primary_failure 阶段——这是"上游 FAIL 禁下游药方"的执行点。
    access_checks:官网审计里 FAIL 的检查项名;给了就只出对应这些检查项的 ACCESS 条目(None = 不按检查项过滤)。"""
    out = [
        Recommendation(
            evidence_id=entry.id,
            stage=entry.stage,
            kind=entry.kind,
            evidence_level=entry.evidence_level,
            hypothesis=entry.hypothesis,
            plain=entry.plain,
            verify=entry.verify,
            sources=entry.sources,
            contraindications=entry.contraindications,
            guardrail=entry.guardrail,
            needs_experiment=entry.kind == GEO_METHOD or entry.evidence_level in _EXPERIMENT_LEVELS,
        )
        for entry in entries
        if entry.stage is stage
        and entry.channel == channel
        and entry.evidence_level in _OUTPUT_LEVELS
        and intent in entry.supported_intents
        and (access_checks is None or not entry.access_checks or set(entry.access_checks) & access_checks)
    ]
    out.sort(key=lambda rec: rec.evidence_level)  # 证据强的排前面
    return out
