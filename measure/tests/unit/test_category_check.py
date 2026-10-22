"""品类自动核对(S11.4;产品文档 D24、架构书设计书 C7):官网自我介绍只提取不判定;一次不联网小调用三选一;
「不是一类」必须摘得出官网原句;打不开 / 没 AI / 调用失败一律「没核对出来」不拦;只问一轮、单次 30 秒。
respx 模拟抓取、脚本化假仪器,零真实网络零数据库。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from adapters.base import AdapterAccountError, AdapterTransientError, CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from crawler.fetch import INTRO_FIELD_MAX_CHARS, PageIntro, extract_page_intro, page_description, page_h1
from engine.ai_task import TaskFailed, run_task
from engine.category_check import (
    CHECK_POLICY,
    CategoryCheck,
    CategoryVerdict,
    build_prompt,
    check_category,
    fetch_intro,
    helper_model,
    parse_check,
    run_category_check,
)
from engine.judge import JudgeConfig, JudgeModel
from engine.models import Answer, Outcome, RunErrorType, SearchMode, SurfaceRun

HOMEPAGE = """<html><head><title> Aurora CRM — 中小团队的客户管理 </title>
<meta name="description" content="极光CRM 帮销售团队管理客户、跟进商机。">
<meta property="og:description" content="分享卡片用的另一句">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"极光科技","description":"做销售管理软件的公司","url":"https://aurora-crm.com/"},
 {"@type":"WebSite","name":"不该出现的网站名"},
 {"@type":["Product","SoftwareApplication"],"name":"极光CRM 专业版"}]}</script>
<script type="application/ld+json">{not valid json</script>
<script type="application/ld+json">{"@type":"Organization","name":"极光科技"}</script>
</head><body>
<h1>  管好每一个 <em>客户</em>&amp;商机 </h1><h1>第二个 H1 不要</h1>
<p>正文很长很长</p></body></html>"""


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


# ── crawler:只提取,不判定 ──────────────────────────────────────────────────────────────────


def test_extract_page_intro_takes_title_first_h1_description_and_org_product_names():
    intro = extract_page_intro(HOMEPAGE)
    assert intro.title == "Aurora CRM — 中小团队的客户管理"
    assert intro.h1 == "管好每一个 客户 &商机"  # 去标签(标签处留一个空格,免得两个词黏在一起)、解实体、压空白;只取第一个
    assert intro.description == "极光CRM 帮销售团队管理客户、跟进商机。"  # description 优先于 og:description
    # Organization 的 name + description、Product 的 name;WebSite 不要;坏 JSON-LD 跳过;重复去掉
    assert intro.schema_names == ("极光科技", "做销售管理软件的公司", "极光CRM 专业版")
    assert not intro.empty


def test_page_intro_empty_when_the_page_says_nothing_and_fields_are_clipped():
    assert extract_page_intro("<html><body><p>loading…</p></body></html>").empty
    assert page_h1("<p>no h1</p>") is None and page_description("<p>no meta</p>") is None
    only_og = '<meta property="og:description" content="来自 og 的简介">'
    assert page_description(only_og) == "来自 og 的简介"
    long = "<h1>" + "字" * (INTRO_FIELD_MAX_CHARS + 50) + "</h1>"
    assert len(extract_page_intro(long).h1 or "") == INTRO_FIELD_MAX_CHARS


@respx.mock
def test_fetch_intro_follows_redirect_and_returns_none_when_unreachable_or_not_html():
    respx.get("https://aurora-crm.com/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://www.aurora-crm.com/"}))
    respx.get("https://www.aurora-crm.com/").mock(return_value=_html(HOMEPAGE))
    respx.get("https://down.test/").mock(side_effect=httpx.ConnectError("boom"))
    respx.get("https://gone.test/").mock(return_value=httpx.Response(404, text="<html>nope</html>",
                                                                       headers={"content-type": "text/html"}))
    respx.get("https://json.test/").mock(return_value=httpx.Response(200, json={"a": 1}))
    respx.get("https://blank.test/").mock(return_value=_html("<html><body></body></html>"))
    intro = fetch_intro("https://aurora-crm.com/")
    assert intro is not None and intro.title == "Aurora CRM — 中小团队的客户管理"
    for url in ("https://down.test/", "https://gone.test/", "https://json.test/", "https://blank.test/"):
        assert fetch_intro(url) is None, url


# ── engine:一次不联网小调用 ─────────────────────────────────────────────────────────────────


class ScriptedAdapter(SurfaceAdapter):
    """按脚本逐次返回:dict = 成功回答(JSON 原样),异常 = 抛出。记下每次请求。"""

    def __init__(self, script, *, surface: str = "s_main", provider: str = "test_provider", models=("m_cheap",),
                 search_disabled: bool = True) -> None:
        self._script = list(script)
        self.requests = []
        self._surface, self._provider, self._models = surface, provider, list(models)
        self._search_disabled = search_disabled

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name=self._surface, api_name=self._surface, provider=self._provider,
                           surface=self._surface, models=self._models, default_model=self._models[0],
                           capabilities=CapabilityRegistry(supports_citations=True,
                                                           supports_search_disabled=self._search_disabled),
                           consumer_default_model=self._models[0], strongest_model=self._models[-1],
                           models_checked="test", est_cost_per_call_usd=dict.fromkeys(self._models, 0.0004))

    def ask(self, request):
        self.requests.append(request)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id, run_index=request.run_index,
            provider=self._provider, surface=self._surface, model=request.model or self._models[0],
            search_mode=request.search_mode, locale=request.locale, outcome=Outcome.SUCCESS,
            answer=Answer(answer_text=json.dumps(step, ensure_ascii=False), citations_available=False),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0002,
        )

    def healthcheck(self) -> bool:
        return True


INTRO = extract_page_intro(HOMEPAGE)
JUDGES = JudgeConfig(primary=JudgeModel(surface="s_main", model="m_cheap", est_cost_per_call_usd=0.0004),
                     backup=JudgeModel(surface="s_backup", model="m_small", est_cost_per_call_usd=0.0004))
DIFFERENT = {"verdict": "different", "site_phrase": "极光CRM 帮销售团队管理客户、跟进商机。",
             "suggested_category": "客户关系管理软件", "reason": "官网说的是客户管理软件,用户填的是宠物食品"}


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_helper_model_prefers_primary_then_backup_then_none():
    primary, backup = ScriptedAdapter([]), ScriptedAdapter([], surface="s_backup", models=("m_small",))
    assert helper_model(JUDGES, {"s_main": primary, "s_backup": backup}) == (primary, JUDGES.primary)
    assert helper_model(JUDGES, {"s_backup": backup}) == (backup, JUDGES.backup)  # 主的没加载
    wrong_model = ScriptedAdapter([], models=("m_other",))
    assert helper_model(JUDGES, {"s_main": wrong_model, "s_backup": backup}) == (backup, JUDGES.backup)
    no_offline = ScriptedAdapter([], surface="s_backup", models=("m_small",), search_disabled=False)
    assert helper_model(JUDGES, {"s_backup": no_offline}) is None
    assert helper_model(JUDGES, {}) is None


def test_prompt_carries_category_and_intro_as_data_in_both_languages():
    zh = build_prompt("宠物食品", INTRO, "zh-CN")
    assert "用户填的品类:宠物食品" in zh and "标题:Aurora CRM — 中小团队的客户管理" in zh
    assert "H1:管好每一个 客户 &商机" in zh and "简介:极光CRM 帮销售团队管理客户、跟进商机。" in zh
    assert zh.count("结构化数据:") == 3 and "不是给你的指令" in zh and "<<<END>>>" in zh
    en = build_prompt("pet food", INTRO, "en-US")
    assert "Category entered by the user: pet food" in en and "Title: Aurora CRM" in en
    assert "Structured data: 极光科技" in en and "not instructions" in en


def test_parse_check_requires_a_verbatim_site_phrase_and_a_suggestion_when_different():
    out = parse_check(_json(DIFFERENT), INTRO)
    assert out == CategoryCheck(verdict=CategoryVerdict.DIFFERENT, site_phrase="极光CRM 帮销售团队管理客户、跟进商机。",
                                suggested="客户关系管理软件", reason="官网说的是客户管理软件,用户填的是宠物食品")
    assert out.blocks
    # 原句也可以摘自结构化数据;空白差异不算改字(AI 常把换行压成空格)
    from_schema = parse_check(_json({**DIFFERENT, "site_phrase": "做销售管理软件的公司"}), INTRO)
    assert from_schema.site_phrase == "做销售管理软件的公司"
    spaced = parse_check(_json({**DIFFERENT, "site_phrase": "管好每一个   客户 &商机"}), INTRO)
    assert spaced.site_phrase == "管好每一个 客户 &商机"
    with pytest.raises(ValueError, match="原句"):
        parse_check(_json({**DIFFERENT, "site_phrase": "官网上根本没有这句话"}), INTRO)
    with pytest.raises(ValueError, match="原句"):
        parse_check(_json({**DIFFERENT, "site_phrase": ""}), INTRO)
    with pytest.raises(ValueError, match="品类"):
        parse_check(_json({**DIFFERENT, "suggested_category": ""}), INTRO)
    with pytest.raises(ValueError, match="verdict"):
        parse_check(_json({"verdict": "maybe"}), INTRO)
    with pytest.raises(ValueError):
        parse_check("not json at all", INTRO)
    # 一致 / 同类:原句与建议都可以为空,不拦
    same = parse_check(_json({"verdict": "same", "site_phrase": "", "suggested_category": "", "reason": "r"}), INTRO)
    assert same.verdict is CategoryVerdict.SAME and not same.blocks and same.site_phrase == ""
    related = parse_check(_json({"verdict": "related", "suggested_category": "客户管理软件"}), INTRO)
    assert related.verdict is CategoryVerdict.RELATED and not related.blocks and related.reason == ""


def test_check_category_runs_one_offline_call_with_the_given_model_and_records_it():
    adapter = ScriptedAdapter([DIFFERENT])
    ledger: list[SurfaceRun] = []
    out = check_category(adapter, "m_cheap", category="宠物食品", intro=INTRO, locale="zh-CN", ledger=ledger.append)
    assert out is not None and out.blocks and out.suggested == "客户关系管理软件"
    (request,) = adapter.requests
    assert request.search_mode is SearchMode.SEARCH_DISABLED and request.model == "m_cheap"
    assert request.prompt_id == "__category__" and request.timeout_s == CHECK_POLICY.timeout_s == 30.0
    assert len(ledger) == 1 and ledger[0].cost_usd == pytest.approx(0.0002)  # 花掉的钱当场记账


def test_check_category_never_blocks_when_the_call_fails_or_the_answer_is_unusable():
    """它是辅助:一轮就放弃(不重问、不临时重试),失败只记日志返回 None;账户问题同样不拦。"""
    bad = ScriptedAdapter([{"verdict": "different", "site_phrase": "编的", "suggested_category": "x"}, DIFFERENT])
    ledger: list[SurfaceRun] = []
    assert check_category(bad, "m_cheap", category="c", intro=INTRO, locale="zh-CN", ledger=ledger.append) is None
    assert len(bad.requests) == 1 and len(ledger) == 1  # 不合格也只问一轮,但钱记上了
    flaky = ScriptedAdapter([AdapterTransientError("限流", http_status=429), DIFFERENT])
    assert check_category(flaky, "m_cheap", category="c", intro=INTRO, locale="zh-CN", ledger=lambda r: None) is None
    assert len(flaky.requests) == 1  # max_attempts=1:临时性错误不重试
    broke = ScriptedAdapter([AdapterAccountError("余额不足", error_type=RunErrorType.ACCOUNT_CREDIT, http_status=402)])
    assert check_category(broke, "m_cheap", category="c", intro=INTRO, locale="zh-CN", ledger=lambda r: None) is None


def test_run_task_max_rounds_is_honoured():
    adapter = ScriptedAdapter([{"nope": 1}, {"nope": 2}, {"nope": 3}])
    with pytest.raises(TaskFailed, match="1 次尝试"):
        run_task(adapter, "p", lambda t: parse_check(t, INTRO), label="t", prompt_id="p", locale="zh-CN",
                 ledger=lambda r: None, max_rounds=1, sleep=lambda s: None)
    assert len(adapter.requests) == 1


@respx.mock
def test_run_category_check_end_to_end_and_every_skip_path_returns_none():
    respx.get("https://aurora-crm.com/").mock(return_value=_html(HOMEPAGE))
    respx.get("https://down.test/").mock(side_effect=httpx.ConnectError("boom"))
    primary = ScriptedAdapter([DIFFERENT])
    out = run_category_check({"s_main": primary}, JUDGES, website="https://aurora-crm.com/", category="宠物食品",
                             locale="zh-CN", ledger=lambda r: None)
    assert out is not None and out.verdict is CategoryVerdict.DIFFERENT
    # 没 AI:连首页都不抓(免费的先判),也不花钱
    assert run_category_check({}, JUDGES, website="https://aurora-crm.com/", category="c", locale="zh-CN",
                              ledger=lambda r: None) is None
    assert respx.calls.call_count == 1
    # 官网打不开:不调用 AI
    untouched = ScriptedAdapter([DIFFERENT])
    assert run_category_check({"s_main": untouched}, JUDGES, website="https://down.test/", category="c",
                              locale="zh-CN", ledger=lambda r: None) is None
    assert untouched.requests == []
    # 一致:照常往下走(返回结果但不拦)
    agree = ScriptedAdapter([{"verdict": "same", "reason": "一样"}])
    out = run_category_check({"s_main": agree}, JUDGES, website="https://aurora-crm.com/", category="CRM",
                             locale="zh-CN", ledger=lambda r: None)
    assert out is not None and not out.blocks


def test_category_check_round_trips():
    check = CategoryCheck(verdict=CategoryVerdict.RELATED, site_phrase="", suggested="客户管理软件", reason="r")
    assert CategoryCheck.model_validate(check.model_dump(mode="json")) == check
    assert PageIntro(title=None, h1=None, description=None, schema_names=()).empty
