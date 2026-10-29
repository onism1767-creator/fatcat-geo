"""每家 AI 一句话(v0.4.14)规则表测试:逐行对照用户拍板的表,缺项句尾说明,不猜不补。"""

from diagnosis.findings import StageStatus
from diagnosis.messages import msg, render_msg
from diagnosis.verdict import Band, VerdictInput, band_of, combine, surface_verdict
from engine.models import PromptIntent

P, D = PromptIntent.BRAND_FACT, PromptIntent.CATEGORY_DISCOVERY


def vi(intent, mention, site):
    return VerdictInput(intent=intent, mention=mention, site_used=site)


def test_band_maps_rate_status_only():
    assert band_of(StageStatus.PASS) is Band.OFTEN
    assert band_of(StageStatus.WEAK) is Band.SOMETIMES
    assert band_of(StageStatus.FAIL) is Band.RARELY
    assert band_of(StageStatus.NOT_OBSERVABLE) is None


def test_combine_averages_half_up():
    assert combine([]) is None
    assert combine([Band.OFTEN, Band.RARELY]) is Band.SOMETIMES
    assert combine([Band.OFTEN, Band.SOMETIMES]) is Band.OFTEN
    assert combine([Band.SOMETIMES, Band.RARELY]) is Band.SOMETIMES


def test_rule_table_rows():
    rows = [
        ([vi(P, Band.OFTEN, True), vi(D, Band.OFTEN, True)], "verdict.known_recommends_cites"),
        ([vi(P, Band.OFTEN, False), vi(D, Band.OFTEN, False)], "verdict.known_recommends_site_rare"),
        ([vi(P, Band.OFTEN, False), vi(D, Band.SOMETIMES, False)], "verdict.known_sometimes_site_rare"),
        ([vi(P, Band.OFTEN, True), vi(D, Band.SOMETIMES, False)], "verdict.known_sometimes_site_used"),
        ([vi(P, Band.OFTEN, False), vi(D, Band.RARELY, False)], "verdict.only_when_named"),
        ([vi(P, Band.OFTEN, True), vi(D, Band.RARELY, False)], "verdict.only_when_named_site_used"),
        ([vi(P, Band.SOMETIMES, True), vi(D, Band.OFTEN, True)], "verdict.barely_known"),
        ([vi(P, Band.RARELY, False), vi(D, Band.RARELY, False)], "verdict.barely_known"),
    ]
    for inputs, code in rows:
        verdict = surface_verdict("s", "m", inputs)
        assert verdict.code == code and verdict.missing == ["verdict.missing.thirdparty"], code  # 夹具没给第三方线
        text = render_msg(msg(code, ai="Claude"), "zh")
        assert "Claude" in text and "RETRIEVAL" not in text  # 人话,不带技术词


def test_missing_scenarios_are_stated_not_guessed():
    only_probe = surface_verdict("s", "m", [vi(P, Band.OFTEN, False)])
    assert only_probe.code == "verdict.known_site_rare"
    assert only_probe.missing == ["verdict.missing.organic", "verdict.missing.thirdparty"]
    only_organic = surface_verdict("s", "m", [vi(D, Band.SOMETIMES, True)])
    assert only_organic.code == "verdict.unnamed_sometimes_site_used"
    assert only_organic.missing == ["verdict.missing.probe", "verdict.missing.thirdparty"]
    nothing = surface_verdict("s", "m", [vi(D, None, None)])
    assert nothing.code == "verdict.no_data"
    assert nothing.missing == ["verdict.missing.probe", "verdict.missing.organic", "verdict.missing.site",
                               "verdict.missing.thirdparty"]
    assert "本次没测" in render_msg(msg("verdict.missing.probe"), "zh")


def test_low_confidence_inputs_are_left_out_and_said_so():
    """失败 / 拒答太多的题型不许下定论(7.3):不进档位、句尾说明——而不是把降权后的「有时」说成
    「它不够了解你」那种最重的定论(审核 2026-10-27 黄 13)。"""
    shaky = VerdictInput(intent=P, mention=Band.SOMETIMES, site_used=True, low_confidence=True)
    verdict = surface_verdict("s", "m", [shaky, vi(D, Band.OFTEN, True)])
    assert verdict.code == "verdict.unnamed_often_site_used"  # 直接问起的那组没算,只说不点名的
    # 测了但说不准 ≠ 没测:句尾只说「说不准」,不再同时说「本次没测」(第二轮)
    assert verdict.missing == ["verdict.missing.thirdparty", "verdict.missing.low_confidence"]
    assert "说不准" in render_msg(msg("verdict.missing.low_confidence"), "zh")
    steady = surface_verdict("s", "m", [vi(P, Band.SOMETIMES, True), vi(D, Band.OFTEN, True)])
    assert steady.code == "verdict.barely_known" and "low_confidence" not in "".join(steady.missing)


def test_unsure_site_and_all_unsure_are_said_plainly():
    """第二轮:官网引用因失败多被降级时不说「很少引用你的官网」,句尾说说不准;全部题型都说不准时整句就说说不准,
    不再写「没有可用的观测(其中:……本次没测)」。"""
    shaky_site = VerdictInput(intent=D, mention=Band.OFTEN, site_used=None, site_unsure=True)
    v = surface_verdict("s", "m", [shaky_site])
    assert v.code == "verdict.unnamed_often_site_rare" or v.code == "verdict.unnamed_often_site_used"
    assert "verdict.missing.site" not in v.missing and "verdict.missing.low_confidence" in v.missing
    all_unsure = surface_verdict("s", "m", [
        VerdictInput(intent=P, mention=Band.SOMETIMES, site_used=None, low_confidence=True),
        VerdictInput(intent=D, mention=Band.RARELY, site_used=None, low_confidence=True),
    ])
    assert all_unsure.code == "verdict.all_unsure"
    said = set(all_unsure.missing)
    assert not {"verdict.missing.probe", "verdict.missing.organic", "verdict.missing.low_confidence"} & said
    assert "说不准" in render_msg(msg("verdict.all_unsure", ai="X"), "zh")


def test_verdict_sentences_end_properly_in_both_languages():
    """第二轮:一句话和第三方那句不再粘连(中文缺句号、英文缺空格)。"""
    from diagnosis.verdict import SurfaceVerdict
    from report.narrative import verdict_text

    v = SurfaceVerdict(surface="s", model="m", code="verdict.known_recommends_cites",
                       missing=["verdict.missing.probe"], third_party_code="verdict.thirdparty.often")
    zh, en = verdict_text(v, "X", "zh"), verdict_text(v, "X", "en")
    assert "。它常常" in zh and zh.count("。") >= 2 and "。(其中:" in zh
    assert ". It often" in en and " (note: " in en
