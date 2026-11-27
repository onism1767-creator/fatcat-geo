"""Toast 证据注册表条目的形状(只读),以及 Butter 读到的注册表来自哪里。

EvidenceEntry 逐字段对齐 Toast 的同名模型(决定 7、设计 §11),依据:
- 字段、默认值与两个校验器:Toast `diagnosis/recommender.py:45-78`;
- 不可变、不许多余字段:Toast `engine/models.py:34-37`;
- 四个阶段:Toast `diagnosis/findings.py:22-26`;五种题型:`engine/models.py:88-96`;
- 10 个检查名:Toast `diagnosis/access_audit.py:65-68`;
- 文件级检查(重复 ID、证据等级、检查名、数据条件)在 `butter.registry.core.check_entries`,照搬 `recommender.py:95-111`。
Toast 改了这些,这里要跟着改;`tests/test_registry.py` 在本机有 Toast 时用 Toast 自己的解释器逐项对照。
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import field_validator

from butter.contracts.common import Frozen

Stage = Literal["ACCESS", "RETRIEVAL", "CITATION", "REPRESENTATION"]
PromptIntent = Literal["category_discovery", "problem_solution", "comparison_alternative", "recommendation", "brand_fact"]
EntryKind = Literal["technical_fix", "geo_method"]

EVIDENCE_LEVELS: Final = ("A", "B", "C", "D", "E")
OUTPUT_LEVELS: Final = ("A", "B", "C", "D")  # E 级永不作为建议输出,只存档(供检查器挂 ID)
ACCESS_CHECK_NAMES: Final = frozenset({
    "reachability", "redirects", "robots_txt", "ua_differential", "html_content", "sitemap", "path_fetch",
    "rendered_fetch", "official_identity", "waf_evidence",
})
DATA_CONDITIONS: Final = frozenset({"third_party_cited_elsewhere"})
REQUIRED_LANGS: Final = frozenset({"zh", "en"})


def _require_langs(value: dict, field: str) -> None:
    if not REQUIRED_LANGS <= set(value):
        raise ValueError(f"{field} 必须含 zh 与 en 两个语言版本,实得 {sorted(value)}")


class EvidenceEntry(Frozen):
    id: str
    stage: Stage
    kind: EntryKind  # technical_fix:注明出处直接建议;geo_method:一律写成实验卡,不得称有效
    channel: str = "own"  # own / third_party / answer;Toast 不校验取值
    access_checks: list[str] = []  # 只能取 ACCESS_CHECK_NAMES;空 = 不按检查项过滤
    requires_data: list[str] = []  # 只能取 DATA_CONDITIONS;全部成立才出
    hypothesis: dict[str, str]
    plain: dict[str, str]  # 人话版,只换说法不换主张
    verify: dict[str, str]  # 做完之后怎么检查效果
    evidence_level: str  # A–E,在文件级检查
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


RegistryOrigin = Literal["toast", "toast_snapshot", "butter_extension"]


class RegistrySource(Frozen):
    """一份注册表文件的来历。界面与报告据此写明用的是本机 Toast 的文件,还是 Butter 自带的某日快照。"""

    origin: RegistryOrigin
    path: str
    sha256: str
    entries: int
    snapshot_taken_on: str = ""  # 只有快照才有:YYYY-MM-DD
    snapshot_of: str = ""  # 快照取自哪个文件、哪次提交
