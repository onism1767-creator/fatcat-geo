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
        assert verdict.code == code and verdict.missing == [], code
        text = render_msg(msg(code, ai="Claude"), "zh")
        assert "Claude" in text and "RETRIEVAL" not in text  # 人话,不带技术词


def test_missing_scenarios_are_stated_not_guessed():
    only_probe = surface_verdict("s", "m", [vi(P, Band.OFTEN, False)])
    assert only_probe.code == "verdict.known_site_rare"
    assert only_probe.missing == ["verdict.missing.organic"]
    only_organic = surface_verdict("s", "m", [vi(D, Band.SOMETIMES, True)])
    assert only_organic.code == "verdict.unnamed_sometimes_site_used"
    assert only_organic.missing == ["verdict.missing.probe"]
    nothing = surface_verdict("s", "m", [vi(D, None, None)])
    assert nothing.code == "verdict.no_data"
    assert nothing.missing == ["verdict.missing.probe", "verdict.missing.organic", "verdict.missing.site"]
    assert "本次没测" in render_msg(msg("verdict.missing.probe"), "zh")
