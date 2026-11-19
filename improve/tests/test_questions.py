"""Butter 出题:固定句式、8/6/6/4、不点名品牌、按品类词对页面、和 Potato 的英文句式逐字一致。"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from butter import sources
from butter.check.lexicon import puffery_hits
from butter.contracts import PageFacts, TemplateSet
from butter.questions import (
    QuestionError, categories_from_factbase, check_templates, draft_questions, guess_lang, match_by_category,
    to_board_questions,
)

T = sources.load_question_templates()
POTATO = Path(__file__).resolve().parents[3] / "AI checker"
POTATO_PY = POTATO / ".venv" / "Scripts" / "python.exe"


def test_templates_follow_the_recipe():
    check_templates(T)
    for lang in ("en", "zh"):
        qs = draft_questions(T, lang=lang, categories=["kettles" if lang == "en" else "烧水壶"], brand="Acme")
        assert Counter(q.intent for q in qs) == {"discovery": 8, "comparison": 6, "local": 6, "brand_defense": 4}
        assert [q.id for q in qs][:3] == ["g1-d01", "g1-d02", "g1-d03"] and qs[-1].id == "g1-b04"
        neutral = [q for q in qs if not q.names_brand]
        assert len(neutral) == 20 and not any("Acme" in q.text for q in neutral)   # 前 20 道不点名任何品牌
        assert all("Acme" in q.text for q in qs if q.names_brand)
        assert all(q.needs_edit == (q.intent == "local") for q in qs)             # 场景 6 道提示站长改
        assert all("{" not in q.text and q.template_version == T.version for q in qs)
        assert len({q.text for q in qs}) == 24


def test_chinese_templates_are_chinese_and_avoid_preset_answers():
    qs = draft_questions(T, lang="zh", categories=["耳机"], brand="某品牌")
    for q in qs:
        assert q.text.endswith("？") and "," not in q.text and "?" not in q.text, q.text
        for word in ("最好", "最佳", "第一", "唯一"):   # 规则书:不预设答案
            assert word not in q.text, q.text
    assert "某品牌的耳机怎么样？有哪些优点和缺点？" in [q.text for q in qs]     # 品牌题两面都问


def test_bad_template_files_are_rejected():
    doc = T.model_dump()
    doc["en"]["discovery"] = doc["en"]["discovery"][:7]
    with pytest.raises(QuestionError):
        check_templates(TemplateSet.model_validate(doc))
    doc = T.model_dump()
    doc["zh"]["comparison"][0] = "{f}和别家的{c}怎么比？"           # 中立题里点了品牌
    with pytest.raises(QuestionError):
        check_templates(TemplateSet.model_validate(doc))
    doc = T.model_dump()
    doc["en"]["brand_defense"][0] = "Is this a good {c} brand?"      # 品牌题没有品牌
    with pytest.raises(QuestionError):
        check_templates(TemplateSet.model_validate(doc))


def test_categories_brand_and_language_edge_cases():
    qs = draft_questions(T, lang="en", categories=[" robot  vacuums ", "Robot Vacuums", "", "video doorbells"])
    assert {q.category for q in qs} == {"robot vacuums", "video doorbells"} and len(qs) == 40   # 没有品牌名:不出品牌题
    assert {q.id.split("-")[0] for q in qs} == {"g1", "g2"}
    assert draft_questions(T, lang="en", categories=[])[0].text == "What are the best products for beginners?"
    assert draft_questions(T, lang="zh", categories=[" "])[0].category == "产品"
    with pytest.raises(QuestionError):
        draft_questions(T, lang="fr", categories=["x"])


def test_categories_come_from_the_factbase_or_nowhere(acme_factbase):
    assert categories_from_factbase(acme_factbase) == []      # 夹具站没有写品类:不猜,留给站长填
    assert guess_lang(acme_factbase) == "en"


def page(url: str, title: str, h1: str = "") -> PageFacts:
    return PageFacts(url=url, role="discovered", title=title, h1=[h1] if h1 else [])


def test_generated_questions_match_pages_by_category_word():
    pages = [page("u1", "Wireless Headphones | Acme", "Acme Headphone X"), page("u2", "Robot Vacuum Omni | Acme"),
             page("u3", "About us"), page("u4", "降噪耳机 | 某品牌"), page("u5", "Vacuum bags")]
    en = draft_questions(T, lang="en", categories=["headphones", "robot vacuums"], brand="Acme")
    m = match_by_category(pages, en)
    assert m["g1-d01"] == ["u1"]                 # 单个词的品类也对得上(单复数归一)
    assert m["g2-d01"] == ["u2"]                 # 两个词都要在:只有 vacuum 的 u5 不算
    assert m["g1-b01"] == [] and m["g2-b04"] == []   # 品牌题不对页面
    zh = draft_questions(T, lang="zh", categories=["耳机"])
    assert match_by_category(pages, zh)["g1-c01"] == ["u4"]
    assert match_by_category(pages, draft_questions(T, lang="en", categories=["kettles"]))["g1-d01"] == []


def test_owner_picks_and_edits_then_board_is_potato_shaped():
    qs = draft_questions(T, lang="en", categories=["kettles"], brand="Acme")
    board = to_board_questions(qs, edits={"g1-l01": "  What kettles suit a café? ", "g1-l02": " "},
                               picked={"g1-d01", "g1-l01", "g1-l02"})
    assert board == [{"id": "g1-d01", "intent": "discovery", "text": "What are the best kettles for beginners?"},
                     {"id": "g1-l01", "intent": "local", "text": "What kettles suit a café?"}]   # 改成空的那道丢掉
    assert len(to_board_questions(qs)) == 24


def test_most_english_templates_would_not_be_copied_into_titles():
    """设计 §9.2:Potato 的句式几乎每道都带 best 这类词,所以 Butter 出的题基本不会被照抄成标题。"""
    qs = [q for q in draft_questions(T, lang="en", categories=["kettles"]) if not q.names_brand]
    blocked = [q for q in qs if puffery_hits(q.text, for_title=True)]
    assert len(blocked) >= 17


@pytest.mark.skipif(not POTATO_PY.exists(), reason="这台机器上没有 Potato:英文句式对拍跳过")
def test_english_templates_match_potato_word_for_word():
    """用 Potato 自己的解释器跑它的出题函数(只读,-B 不写缓存),和 Butter 抄来的英文句式逐字比。"""
    code = ("import json; from aivis.templater import draft_template_questions as d; "
            "print(json.dumps([[q.id, q.intent, q.text] for q in d(category='robot vacuums', focal_name='Acme')]))")
    env = {**os.environ, "PYTHONPATH": str(POTATO / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([str(POTATO_PY), "-B", "-c", code], capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=str(POTATO), timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    theirs = json.loads(done.stdout)
    ours = [[q.id.split("-", 1)[1], q.intent, q.text]
            for q in draft_questions(T, lang="en", categories=["robot vacuums"], brand="Acme")]
    assert ours == theirs
