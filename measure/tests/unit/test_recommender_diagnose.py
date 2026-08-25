"""建议合规与失败定位测试(12.1 recommender 约束 + 诊断规则表驱动)。

铁律 9 的三条机器化:每条建议必须有注册表 ID;上游 FAIL 时下游建议为空;
C 级及以下必须含「建议实验验证」。
"""

from datetime import UTC, datetime

import pytest
import yaml

from diagnosis.access_audit import AccessAuditResult, CheckStatus
from diagnosis.diagnose import access_finding, diagnose_need, localize_failure
from diagnosis.findings import Msg, Stage, StageFinding, StageStatus
from diagnosis.messages import render_msg
from diagnosis.recommender import (
    DEFAULT_REGISTRY_PATH,
    load_evidence_registry,
    recommend,
)


def zh_cautions(diagnosis) -> list[str]:
    return [render_msg(c, "zh") for c in diagnosis.cautions]
from engine.models import InformationNeed, NeedIntent

REGISTRY = load_evidence_registry()

NEED = InformationNeed(need_id="need-1", statement="测试需求", intent=NeedIntent.DISCOVERY)


def make_finding(stage: Stage, status: StageStatus) -> StageFinding:
    return StageFinding(stage=stage, status=status,
                        headline=Msg(code="test.headline", params={}))


def findings_for(access=StageStatus.PASS, retrieval=StageStatus.PASS,
                 citation=StageStatus.PASS, representation=StageStatus.PASS):
    return dict(
        access=make_finding(Stage.ACCESS, access),
        retrieval=make_finding(Stage.RETRIEVAL, retrieval),
        citation=make_finding(Stage.CITATION, citation),
        representation=make_finding(Stage.REPRESENTATION, representation),
    )


# ── 注册表本身 ────────────────────────────────────────────────────────


def test_registry_loads_with_all_stages_covered():
    stages = {entry.stage for entry in REGISTRY}
    assert stages == set(Stage)
    assert len(REGISTRY) >= 12


def test_registry_rejects_duplicate_ids(tmp_path):
    data = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    data["entries"].append(dict(data["entries"][0]))
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="重复 ID"):
        load_evidence_registry(path)


# ── recommend 约束 ───────────────────────────────────────────────────


def test_recommend_only_returns_registry_ids_for_the_given_stage():
    recs = recommend(REGISTRY, stage=Stage.RETRIEVAL, intent=NeedIntent.DISCOVERY)
    assert recs, "RETRIEVAL×discovery 应有建议"
    registry_ids = {entry.id for entry in REGISTRY}
    assert all(rec.evidence_id in registry_ids for rec in recs)
    assert all(rec.stage is Stage.RETRIEVAL for rec in recs)


def test_recommend_excludes_level_e_entries():
    recs = recommend(REGISTRY, stage=Stage.RETRIEVAL, intent=NeedIntent.DISCOVERY)
    ids = {rec.evidence_id for rec in recs}
    assert "EVIDENCE_LLMSTXT_V1" not in ids  # E 级永不输出
    assert "EVIDENCE_KEYWORD_STUFFING_V1" not in ids


def test_recommend_marks_experiment_for_c_and_below():
    recs = recommend(REGISTRY, stage=Stage.CITATION, intent=NeedIntent.DISCOVERY)
    assert recs
    for rec in recs:
        assert rec.needs_experiment is (rec.evidence_level in ("C", "D"))
        assert {"zh", "en"} <= set(rec.hypothesis)  # 双语齐备


def test_recommend_filters_by_intent():
    assert recommend(REGISTRY, stage=Stage.CITATION, intent=NeedIntent.BRAND_PROBE) == []


def test_recommend_sorts_stronger_evidence_first():
    recs = recommend(REGISTRY, stage=Stage.RETRIEVAL, intent=NeedIntent.DISCOVERY)
    levels = [rec.evidence_level for rec in recs]
    assert levels == sorted(levels)


# ── 定位规则 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (dict(access=StageStatus.FAIL, retrieval=StageStatus.FAIL), Stage.ACCESS),
        (dict(retrieval=StageStatus.FAIL, citation=StageStatus.FAIL), Stage.RETRIEVAL),
        (dict(citation=StageStatus.FAIL), Stage.CITATION),
        (dict(representation=StageStatus.FAIL), Stage.REPRESENTATION),
        (dict(citation=StageStatus.WEAK, representation=StageStatus.WEAK), Stage.CITATION),
        (dict(), None),  # 全 PASS
        (dict(retrieval=StageStatus.NOT_OBSERVABLE, citation=StageStatus.WEAK), Stage.CITATION),
    ],
)
def test_localize_failure_table(statuses, expected):
    f = findings_for(**statuses)
    findings = {
        Stage.ACCESS: f["access"], Stage.RETRIEVAL: f["retrieval"],
        Stage.CITATION: f["citation"], Stage.REPRESENTATION: f["representation"],
    }
    assert localize_failure(findings) is expected


# ── diagnose_need:上游 FAIL 禁下游药方 ───────────────────────────────


def test_upstream_fail_yields_only_upstream_recommendations():
    diagnosis = diagnose_need(NEED, registry=REGISTRY,
                              **findings_for(access=StageStatus.FAIL,
                                             citation=StageStatus.FAIL))
    assert diagnosis.primary_failure is Stage.ACCESS
    assert diagnosis.recommendations, "ACCESS FAIL 应有准入修复建议"
    assert all(rec.stage is Stage.ACCESS for rec in diagnosis.recommendations)


def test_retrieval_failure_warns_against_rewriting():
    # 引用阶段未 PASS(WEAK)时,检索 FAIL 的定位成立
    diagnosis = diagnose_need(NEED, registry=REGISTRY,
                              **findings_for(retrieval=StageStatus.FAIL,
                                             citation=StageStatus.WEAK))
    assert diagnosis.primary_failure is Stage.RETRIEVAL
    assert any("不建议】进行全页" in caution for caution in zh_cautions(diagnosis))
    assert all(rec.stage is Stage.RETRIEVAL for rec in diagnosis.recommendations)


def test_retrieval_fail_contradicted_by_citation_pass_is_channel_divergence():
    """被引必先被检索:引用 PASS 时检索 FAIL 不得定位为失败(风险 #5)。"""
    diagnosis = diagnose_need(NEED, registry=REGISTRY,
                              **findings_for(retrieval=StageStatus.FAIL))
    assert diagnosis.primary_failure is None  # 其余全 PASS → 无失败可定位
    assert any("观测通道" in caution and "不据此定位" in caution
               for caution in zh_cautions(diagnosis))
    assert diagnosis.recommendations == []


def test_divergent_retrieval_skipped_but_downstream_weak_still_localizes():
    diagnosis = diagnose_need(NEED, registry=REGISTRY,
                              **findings_for(retrieval=StageStatus.FAIL,
                                             representation=StageStatus.WEAK))
    assert diagnosis.primary_failure is Stage.REPRESENTATION


def test_all_pass_yields_no_recommendations():
    diagnosis = diagnose_need(NEED, registry=REGISTRY, **findings_for())
    assert diagnosis.primary_failure is None
    assert diagnosis.recommendations == []
    assert any("无需药方" in caution for caution in zh_cautions(diagnosis))


def test_not_observable_stages_are_named_in_cautions():
    diagnosis = diagnose_need(NEED, registry=REGISTRY,
                              **findings_for(retrieval=StageStatus.NOT_OBSERVABLE))
    assert any("RETRIEVAL" in caution for caution in zh_cautions(diagnosis))


# ── ACCESS 结论翻译 ──────────────────────────────────────────────────


def make_audit(overall: CheckStatus) -> AccessAuditResult:
    return AccessAuditResult(
        site="https://example.com", final_url="https://example.com",
        overall=overall, checks=[], bots=[], caveats=[],
        audited_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def test_access_finding_mapping():
    assert access_finding(make_audit(CheckStatus.PASS)).status is StageStatus.PASS
    assert access_finding(make_audit(CheckStatus.FAIL)).status is StageStatus.FAIL
    unknown = access_finding(make_audit(CheckStatus.UNKNOWN))
    assert unknown.status is StageStatus.NOT_OBSERVABLE
    assert access_finding(None).status is StageStatus.NOT_OBSERVABLE
