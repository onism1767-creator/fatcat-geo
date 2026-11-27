"""错误库(S10.1,用户定「完整的错误库和错误页面」):每个码双语齐全、有下一步;代码里用到的码都在表里;
/errors 页列出全部;表单 / 任务页的句子只从库里取。零网络零数据库。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from diagnosis.citation_verify import VerificationOutcome
from engine.models import PageFetchStatus, StopReason
from engine.validity import PublicErrorCode
from report.app import create_app
from report.errors import SCOPES, load_errors
from report.forms import FORM_CODES
from report.runner import RUNNER_CODES
from tests.unit.test_report_page import REGISTRY, DummyAdapter

LIBRARY = load_errors()


def test_every_code_is_bilingual_with_a_next_step_and_a_known_scope():
    assert len(LIBRARY.entries) >= 30
    for entry in LIBRARY.entries:
        assert entry.scope in SCOPES, entry.code
        for field in ("trigger", "message", "next_step"):
            texts = getattr(entry, field)
            assert set(texts) == {"zh", "en"} and all(v.strip() for v in texts.values()), (entry.code, field)
        assert entry.code == entry.code.upper()


def test_user_facing_texts_do_not_use_the_internal_word_instrument():
    """「仪器 / instrument」是内部说法:错误库(页面与错误对照页都显示)和主视图人话目录里一律不用(B23)。"""
    from diagnosis.messages import PLAIN

    texts = [v for e in LIBRARY.entries for part in ("trigger", "message", "next_step", "causes")
             for v in (getattr(e, part) or {}).values()]
    texts += [v for versions in PLAIN.values() for v in versions.values()]
    assert not [t for t in texts if "仪器" in t or "instrument" in t.lower()]


def test_every_code_the_program_can_raise_is_in_the_library():
    codes = {e.code for e in LIBRARY.entries}
    assert set(FORM_CODES) <= codes and set(RUNNER_CODES) <= codes
    assert {c.value for c in PublicErrorCode} <= codes  # 单次提问 + 账户
    assert {s.value for s in StopReason if s is not StopReason.COMPLETED} <= codes  # 熔断
    page_codes = {f"PAGE_{s.value}" for s in PageFetchStatus if s is not PageFetchStatus.OK}
    assert page_codes | {"PAGE_NOT_ATTEMPTED", "CITATION_UNRESOLVED"} <= codes
    assert {f"VERIFY_{v.value}" for v in VerificationOutcome
            if v not in (VerificationOutcome.VERIFIED_MATCH, VerificationOutcome.CONTENT_MISMATCH)} <= codes
    assert {"JOB_STALLED", "JOB_CRASHED", "JUDGE_NOT_VISIBLE", "DRAFTING_FAILED"} <= codes


def test_text_fills_placeholders_and_refuses_missing_ones():
    entry = LIBRARY["ACCOUNT_CREDIT"]
    text = entry.text("zh", with_next_step=True, ai="OpenAI", model="gpt-6-luna", detail="credit balance too low")
    assert "OpenAI(型号 gpt-6-luna)" in text and "调高 / 取消花费上限" in text and "credit balance too low" in text
    assert "Top up" in entry.text("en", with_next_step=True, ai="OpenAI", model="m", detail="x")
    assert "充值" not in entry.text("zh", with_next_step=False, ai="a", model="m", detail="d")
    with pytest.raises(KeyError):
        entry.text("zh", with_next_step=True, ai="a")  # 缺 model / detail:不静默吞
    with pytest.raises(KeyError):
        LIBRARY["NOPE"]


def test_broken_library_fails_at_load(tmp_path: Path):
    entry = "  - {code: A, scope: form, resumable: false, trigger: {zh: x, en: x}, message: {zh: x, en: x}, "
    dup = tmp_path / "errors.yaml"
    dup.write_text("errors:\n" + (entry + "next_step: {zh: x, en: x}}\n") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_errors(dup)
    missing = tmp_path / "missing.yaml"
    missing.write_text("errors:\n" + entry.replace("message: {zh: x, en: x}", "message: {zh: x}")
                       + "next_step: {zh: x, en: x}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="zh / en"):
        load_errors(missing)

def _client() -> TestClient:
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None)
    return TestClient(app)


def test_errors_page_lists_every_code_in_both_languages():
    client = _client()
    zh = client.get("/errors?lang=zh").text
    assert "错误码对照" in zh and "打开网页" in zh
    for entry in LIBRARY.entries:
        assert f'id="{entry.code}"' in zh and entry.message["zh"] in zh and entry.next_step["zh"] in zh
    assert "能接着跑" in zh and "ACCOUNT_CREDIT" in zh
    en = client.get("/errors?lang=en").text
    assert "Error codes" in en and LIBRARY["JOB_STALLED"].message["en"] in en and "错误码对照" not in en


def test_runtime_codes_explain_their_common_causes():
    """用户 2026-10-21:熔断类错误要分析原因和解决方案——运行类范围的码都要有「常见原因」,并出现在 /errors 页。"""
    runtime = {e.code for e in LIBRARY.entries if e.scope in ("account", "db", "job", "ai", "judge", "breaker")}
    missing = {code for code in runtime if LIBRARY[code].causes is None}
    assert not missing, missing
    breaker = LIBRARY["FAILURE_RATE_EXCEEDED"]
    assert breaker.causes is not None and "限流" in breaker.causes["zh"] and "rate limits" in breaker.causes["en"]
    assert "调低并发" in breaker.next_step["zh"] or "并发" in breaker.next_step["zh"]
    page = _client().get("/errors?lang=zh").text
    assert "常见原因" in page and breaker.causes["zh"] in page
