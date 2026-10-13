"""第三方线诊断(v0.4.24 D2 / D3;S6.1):各自亮灯、各自定位、看不到按原因列;建议只从第三方线的条目取。"""

from diagnosis.diagnose import diagnose_surface
from diagnosis.findings import Line, Stage, StageFinding, StageStatus
from diagnosis.messages import msg, render_msg
from diagnosis.recommender import load_evidence_registry, recommend
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import retrieval_not_observable
from diagnosis.third_party_diag import diagnose_third_party, localize_third_party
from diagnosis.verdict import Band, surface_verdict, verdict_input
from engine.metrics import rate
from engine.models import PromptIntent
from tests.unit.test_stage_diagnostics import make_metric  # 题型层指标夹具

REGISTRY = load_evidence_registry()


def _zh(m, register=None):
    return render_msg(m, "zh", register)


def test_third_party_step_lights_and_lists_unobservable_reasons():
    finding = diagnose_third_party(Stage.CITATION, make_metric(2, 12), rate(1, 2),
                                   {"BLOCKED": 3, "NOT_ATTEMPTED": 1, "ROBOTS_DISALLOWED": 2})
    assert finding.line is Line.THIRD_PARTY and finding.status is StageStatus.FAIL
    assert _zh(finding.headline, "plain") == "AI 基本没有把写到你的第三方网页当作来源"
    lines = [_zh(line) for line in finding.evidence_lines]
    assert lines[0].startswith("第三方引用率:2/12 = 17%")
    assert "其中标题或首段就出现品牌:1/2" in lines[1]
    assert "看不到的网页 6 条" in lines[2] and "站点不允许抓取 2、被拦截 3、没来得及开 1" in lines[2]
    assert _zh(finding.evidence_lines[0], "plain") == "本次 12 次回答里,2 次引用了写到你的第三方网页(和官网分开算)"
    unobservable = diagnose_third_party(Stage.RETRIEVAL, None, None, {"NOT_ATTEMPTED": 4})
    assert unobservable.status is StageStatus.NOT_OBSERVABLE and len(unobservable.evidence_lines) == 1


def test_localization_and_recommendations_stay_inside_the_third_party_line():
    retrieved = diagnose_third_party(Stage.RETRIEVAL, make_metric(1, 12), None, {})  # FAIL
    cited = diagnose_third_party(Stage.CITATION, make_metric(5, 12), None, {})  # WEAK
    assert localize_third_party([retrieved, cited]) is Stage.RETRIEVAL
    assert localize_third_party([retrieved, cited], skip=frozenset({Stage.RETRIEVAL})) is Stage.CITATION
    tp_recs = recommend(REGISTRY, stage=Stage.RETRIEVAL, intent=PromptIntent.CATEGORY_DISCOVERY, channel="third_party")
    assert [r.evidence_id for r in tp_recs] == ["EVIDENCE_THIRDPARTY_RETRIEVAL_V1"]
    own_recs = recommend(REGISTRY, stage=Stage.RETRIEVAL, intent=PromptIntent.CATEGORY_DISCOVERY)
    assert "EVIDENCE_THIRDPARTY_RETRIEVAL_V1" not in [r.evidence_id for r in own_recs]  # 从官网线挪走(D9)

    block = diagnose_surface(
        provider="p", surface="s", model="m",
        access=StageFinding(stage=Stage.ACCESS, status=StageStatus.PASS, headline=retrieved.headline),
        retrieval=retrieval_not_observable(),
        citation=diagnose_third_party(Stage.CITATION, make_metric(10, 12), None, {}),  # 官网线:随便一个 PASS
        representation=diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(6, 12), {}),
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY,
        third_party=[retrieved, cited],
    )
    assert block.third_party_failure is Stage.RETRIEVAL
    assert [r.evidence_id for r in block.third_party_recommendations] == ["EVIDENCE_THIRDPARTY_RETRIEVAL_V1"]
    assert block.third_party_failure is Stage.RETRIEVAL  # 两条线各算各的,官网线的定位不影响这里

    # 通道分歧防线同样适用:搜到 FAIL 但引用 PASS → 不拿搜到步定位,给提示
    divergent = diagnose_surface(
        provider="p", surface="s", model="m",
        access=StageFinding(stage=Stage.ACCESS, status=StageStatus.PASS, headline=retrieved.headline),
        retrieval=retrieval_not_observable(),
        citation=diagnose_third_party(Stage.CITATION, make_metric(10, 12), None, {}),
        representation=diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(6, 12), {}),
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY,
        third_party=[retrieved, diagnose_third_party(Stage.CITATION, make_metric(11, 12), None, {})],
    )
    assert divergent.third_party_failure is None
    assert [c.code for c in divergent.third_party_cautions] == ["caution.thirdparty.divergence"]


def test_verdict_gets_its_own_third_party_sentence():
    cited_pass = diagnose_third_party(Stage.CITATION, make_metric(10, 12), None, {})
    cited_none = diagnose_third_party(Stage.CITATION, None, None, {})
    own = diagnose_third_party(Stage.CITATION, make_metric(10, 12), None, {})
    inputs = [
        verdict_input(PromptIntent.CATEGORY_DISCOVERY, make_metric(6, 12), retrieval_not_observable(), own,
                      third_party_cited=cited_pass),
        verdict_input(PromptIntent.RECOMMENDATION, make_metric(6, 12), retrieval_not_observable(), own,
                      third_party_cited=cited_none),
    ]
    assert inputs[0].third_party is Band.OFTEN and inputs[1].third_party is None
    verdict = surface_verdict("s", "m", inputs)
    assert verdict.third_party_code == "verdict.thirdparty.often"
    assert "verdict.missing.thirdparty" not in verdict.missing
    assert _zh(msg(verdict.third_party_code), "plain") == "写到你的第三方网页经常被它当作来源"
    nothing = surface_verdict("s", "m", [inputs[1]])
    assert nothing.third_party_code is None and "verdict.missing.thirdparty" in nothing.missing
