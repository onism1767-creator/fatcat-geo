"""双语测试(16.5):测量语境驱动起草 prompt 语言;界面语言 URL 切换。

两个维度严格分离:locale 是口径,lang 只是显示。
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from engine.models import BrandSpec
from report.app import create_app
from tests.unit.test_needs import COMPARISON_OK, COMPETITORS_OK, FACTS_OK, RecordingAdapter
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job

BRAND = BrandSpec(name="AuroraCRM", domains=["aurora-crm.com"])

BLIND_EN = {
    "category_discovery": [f"discovery q{i}" for i in range(1, 9)],
    "problem_solution": [f"problem q{i}" for i in range(1, 5)],
    "recommendation": [f"recommendation q{i}" for i in range(1, 5)],
}
COMPARISON_EN = {"comparison_alternative": [f"comparison q{i}" for i in range(1, 5)]}
COMPETITORS_EN = {"competitors": [{"name": "SalesLoop", "domain": "salesloop.example"},
                                  {"name": "CloudDesk", "domain": "clouddesk.example"}]}  # 英文题的竞品名也是英文
FACTS_EN = {"brand_fact": ["Is AuroraCRM good? Strengths and weaknesses?", "What does AuroraCRM do?",
                           "How does AuroraCRM compare on value for money?", "Who is AuroraCRM for?"]}


def has_cjk(text: str) -> bool:
    return re.search(r"[一-鿿]", text) is not None


def test_en_locale_selects_english_drafting_prompts():
    from engine.drafting import draft_prompt_candidates

    adapter = RecordingAdapter([BLIND_EN, COMPETITORS_EN, COMPARISON_EN, FACTS_EN])
    draft_prompt_candidates(adapter, category="CRM software", brand=BRAND, locale="en-US", ledger=lambda run: None)
    blind_prompt, competitor_prompt, comparison_prompt, facts_prompt = adapter.prompts
    assert "Draft EXACTLY" in blind_prompt and not has_cjk(blind_prompt)
    assert "EXCLUDING" in competitor_prompt and not has_cjk(competitor_prompt)
    assert "alternatives" in comparison_prompt and not has_cjk(comparison_prompt)
    assert "TWO-SIDED" in facts_prompt and not has_cjk(facts_prompt)


def test_zh_locale_keeps_chinese_drafting_prompts():
    from engine.drafting import draft_prompt_candidates
    from tests.unit.test_needs import BLIND_OK

    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK, FACTS_OK])
    draft_prompt_candidates(adapter, category="CRM", brand=BrandSpec(name="极光CRM"), locale="zh-CN",
                          ledger=lambda run: None)
    assert has_cjk(adapter.prompts[0])


def test_ui_language_toggle():
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    client = TestClient(app)
    default = client.get("/").text
    assert "Brand name" in default and "品牌名" not in default  # 默认英文(产品文档 D16;S8.6)
    zh = client.get("/?lang=zh").text
    assert "品牌名" in zh
    en = client.get("/?lang=en").text
    assert "Brand name" in en and "品牌名" not in en


def test_report_page_fully_english():
    """v1.1:结论语句本身随界面语言渲染,英文报告不残留中文结论。"""
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    job = finished_job()
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    html = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "AI Visibility Report" in html
    assert "the brand rarely reaches the model" in html  # L3 结论英文化(s2 生成端检索 FAIL;撇号会被转义)
    assert "points behind competitor" in html
    assert "NOT recommended" in html  # 不建议全页改写的英文警示
    assert "Experiment card" in html and "Basis" in html  # 实验卡英文版
    assert "stop_reasons" in html and "EVIDENCE_" in html  # 口径要素不丢
    assert "在 AI 自己的搜索习惯下" not in html  # 中文结论不残留


def test_catalog_bilingual_completeness():
    """每个结论码必须 zh/en 双版齐备——组句永不静默缺语言。"""
    from diagnosis.messages import CATALOG, PLAIN

    for code, versions in CATALOG.items():
        assert set(versions) == {"zh", "en"}, code
        assert versions["zh"].strip() and versions["en"].strip(), code
    for code, versions in PLAIN.items():  # 人话目录(v0.4.14):码必须存在于技术目录,且双语齐备
        assert code in CATALOG, code
        assert set(versions) == {"zh", "en"} and versions["zh"].strip() and versions["en"].strip(), code


def test_plain_register_renders_counts_and_falls_back():
    """用户视图人话版:比例写「N 次里 k 次(p%)」——计数在前、百分比跟后(用户定 S8);无人话版的码回落技术版。"""
    from diagnosis.findings import rate_params
    from diagnosis.messages import msg, render_msg
    from engine.metrics import rate

    line = msg("citation.rate", **rate_params(rate(7, 16)))
    # 计数在前、百分比跟在后面(用户定 S8)
    assert render_msg(line, "zh", "plain") == "本次 16 次回答里,7 次(44%)拿你的官网当参考"
    assert render_msg(line, "zh") == "被引比例:7/16 = 44%"  # 审计视图:计数 + 比例,不出区间
    nested = msg("caution.unobservable", stages=[msg("stage.ACCESS"), msg("stage.RETRIEVAL")])
    assert "官网访问(爬虫能不能进你的网站)" in render_msg(nested, "zh", "plain")  # 正式用词(口语解释)
    assert render_msg(nested, "zh") == "以下阶段不可观测,未参与瓶颈定位:ACCESS、RETRIEVAL"
    assert render_msg(msg("common.caliber", surface="s", model="m", n_prompts=1, n_valid=1, n_total=1,
                          stop_reasons="", n_refused=0, n_empty=0, n_failed=0), "zh", "plain").startswith("口径")


def test_all_diagnosis_output_renders_in_both_languages():
    """把一份完整诊断的全部 Msg 在两种语言下渲染一遍:缺码/缺参立即炸。"""
    from diagnosis.messages import render_msg

    job = finished_job()
    diagnosis = job.class_reports[0].diagnosis
    messages = [diagnosis.access.headline, *diagnosis.access.evidence_lines]
    for sd in diagnosis.surfaces:
        messages += [f.headline for f in sd.findings]
        messages += [line for f in sd.findings for line in f.evidence_lines]
        messages += sd.cautions
        if sd.retrieval_dependency is not None:
            messages.append(sd.retrieval_dependency.headline)
            messages += sd.retrieval_dependency.evidence_lines
    assert messages
    for lang in ("zh", "en"):
        for message in messages:
            assert render_msg(message, lang).strip()


def test_message_lists_are_joined_per_language():
    """英文输出里不能出现中文顿号(审核 2026-10-27 黄 22)。"""
    from diagnosis.messages import msg, render_msg

    nested = msg("caution.unobservable", stages=[msg("stage.ACCESS"), msg("stage.RETRIEVAL")])
    assert render_msg(nested, "zh").endswith("ACCESS、RETRIEVAL")
    assert render_msg(nested, "en").endswith("ACCESS, RETRIEVAL") and "、" not in render_msg(nested, "en")


ROOT = Path(__file__).resolve().parents[2]


def _scanned(pattern: str, *paths: str) -> set[str]:
    """代码里的字面量:每个文件按正则扫出第一组(或几组)取值。"""
    found: set[str] = set()
    for path in paths:
        for root in [ROOT / path] if (ROOT / path).is_file() else sorted((ROOT / path).glob("*.py")):
            for hit in re.findall(pattern, root.read_text(encoding="utf-8")):
                found |= set(hit) if isinstance(hit, tuple) else {hit}
    return found


def test_every_key_the_pages_build_from_a_value_exists_in_both_languages():
    """界面文案取值是严格的(t[key] 缺了就抛错,首页整页 500):代码和模板里「前缀 + 取值」拼出来的每个键,中英都得有,
    还不能是空串(第二轮审核 B17)。阶段码扫代码字面量;运行结果、网页抓取状态、来源归类、命中方式、官网依据等按枚举或
    判定器取值全列——新增一个取值忘了配文案,这里先炸。"""
    from diagnosis.representation_diag import _RELATION
    from engine.detectors.name_risk import _KIND_ORDER
    from engine.detectors.source_class import REASON_NOT_ATTEMPTED, REASON_UNRESOLVED
    from engine.models import Outcome, PageFetchStatus, Sentiment, SourceClass
    from engine.tiers import MentionMethod, load_tiers
    from engine.validity import PublicErrorCode
    from report.errors import load_errors
    from report.i18n import MESSAGES
    from report.narrative import _STATUS_ORDER

    phases = _scanned(r'"(phase_[a-z_]+)"', "report")
    evidence_kinds = _scanned(r'kind="([a-z_]+)"|\.note\("([a-z_]+)"|kind = "([a-z_]+)" if .+ else "([a-z_]+)"',
                              "engine/brand_domains.py") - {""}
    assert {"phase_draft", "phase_sampling", "phase_done"} <= phases and {"seen", "hreflang"} <= evidence_kinds
    keys = {
        *phases,
        *(f"outcome_{o.value}" for o in Outcome if o is not Outcome.SUCCESS),  # 单条回答页:无效回答的原因
        *(f"reason_{s.value}" for s in PageFetchStatus if s is not PageFetchStatus.OK),  # 引用网页看不到的原因
        f"reason_{REASON_UNRESOLVED}", f"reason_{REASON_NOT_ATTEMPTED}",
        *(f"class_{c.value}" for c in SourceClass), "class_UNKNOWN",  # 来源归类;没有归属记录时 UNKNOWN
        *(f"resp_match_{kind}" for kind in (*_KIND_ORDER, "judge")),  # 命中方式:模糊判定器三种 + AI 裁判
        *(f"sentiment_{s.value}" for s in Sentiment),
        *(f"tier_{tier}" for tier in ("primary", "A", "B", "unknown")),  # 单条回答页命中的官网等级
        *(f"method_{m.value}" for m in MentionMethod),
        *(f"tier_{part}_{spec.name}" for spec in load_tiers().tiers for part in ("name", "blurb")),
        *(f"errors_scope_{scope}" for scope, _ in load_errors().by_scope()),
        *(f"count_{status}" for status in _STATUS_ORDER),
        *(f"breaker_code_{code.value}" for code in PublicErrorCode),  # 提前停了:按对外错误码分组计数
        *(f"compare_{relation}" for relation in _RELATION.values()),
        *(f"ev_{kind}" for kind in evidence_kinds),
    }
    for lang in ("zh", "en"):
        missing = sorted(key for key in keys if not str(MESSAGES[lang].get(key, "")).strip())
        assert not missing, (lang, missing)


def test_both_languages_have_the_same_ui_keys():
    """中文有、英文没有(或反过来)的键:切到那种语言,用到它的页面就 500(B17)。"""
    from report.i18n import MESSAGES

    assert set(MESSAGES["zh"]) == set(MESSAGES["en"])


def test_cells_named_in_how_to_check_texts_exist_on_the_page():
    """实验卡「怎么验证」点名的格子(中文「」、英文“”括起来的)必须是页面上真有的列名 / 标题 / 题型名,完整或到括号为止
    (第二轮审核 B20:原来写的「AI 有没有引用你的官网」页面上没有,用户找不到该看哪一格)。"""
    from diagnosis.recommender import load_evidence_registry
    from report.i18n import INTENT_LABELS, MESSAGES

    quote = {"zh": r"「([^」]+)」", "en": r"“([^”]+)”"}
    for lang in ("zh", "en"):
        labels = [str(v) for v in MESSAGES[lang].values()] + list(INTENT_LABELS[lang].values())
        for entry in load_evidence_registry():
            for named in re.findall(quote[lang], entry.verify[lang]):
                assert any(label == named or re.match(re.escape(named) + r"\s*[(（]", label) for label in labels), \
                    (lang, entry.id, named)
