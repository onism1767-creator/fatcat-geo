"""第 5 步:把改动放上网站,再测一次,改前改后并排进报告。三个项目那一层是假的:不联网、不花钱。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from fatcat import project as projects
from fatcat.hub.app import AFTER_NAME, BEFORE_NAME, COMPARE_NAME
from fatcat.hub.tools import STOPPED
from fatcat.report import Sources, build, build_comparison, render
from fatcat.report.words import WORDS
from tests.test_hub import COMPARE, FakeBackend, _create, backend, client  # noqa: F401
from tests.test_report import NOW, _chapter, _text, _toast

TODAY = date.today().isoformat()
RETEST = {"job_id": "feed00000001", "state": "DONE", "brand": "Brand", "website": "https://brand.example",
          "created_at": "2026-11-24T16:00:00", "report_versions": 1, "parent": "job1", "spent_usd": 18.9,
          "budget_usd": 30.0}


def _measured(client: TestClient) -> str:  # noqa: F811
    project_id = _create(client)
    assert client.post(f"/projects/{project_id}/measure/attach", data={"job_id": "job1"}).status_code == 303
    return project_id


def test_nothing_to_compare_until_there_is_a_measurement(client: TestClient) -> None:  # noqa: F811
    project_id = _create(client)
    page = client.get(f"/projects/{project_id}").text
    assert "Finish “Measure and diagnose” first" in page and "/remeasure/start" not in page and 'name="day"' not in page


def test_the_day_must_be_a_real_day_that_has_come(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    project_id = _measured(client)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for bad in ("", "soon", "2026-13-40", tomorrow):
        assert client.post(f"/projects/{project_id}/deploy", data={"day": bad}).status_code == 400, bad
    assert projects.load(tmp_path / "runs", project_id).deploy_day is None
    assert client.post(f"/projects/{project_id}/deploy", data={"day": TODAY}).status_code == 303
    assert projects.load(tmp_path / "runs", project_id).deploy_day == TODAY
    assert f"Changes went live on {TODAY}." in client.get(f"/projects/{project_id}").text


def test_measuring_again_states_the_cost_and_needs_the_day_first(client: TestClient, backend: FakeBackend,  # noqa: F811
                                                                 tmp_path: Path) -> None:
    project_id = _measured(client)
    page = client.get(f"/projects/{project_id}").text
    assert "Before: run job1." in page and "/remeasure/start" not in page  # 没记日子:没有再测的按钮
    refused = client.post(f"/projects/{project_id}/remeasure/start")
    assert refused.status_code == 400 and backend.retests == []  # 绕过页面直接发也不行
    client.post(f"/projects/{project_id}/deploy", data={"day": TODAY})

    backend.tool = {"state": STOPPED, "ours": False, "note": ""}
    page = client.get(f"/projects/{project_id}").text
    assert "/remeasure/start" not in page and "Start the measurement tool (step 3) before measuring again." in page

    backend.tool = {"state": "running", "ours": True, "note": ""}
    page = client.get(f"/projects/{project_id}").text
    # 按钮上和按钮旁边都写着钱:上一单花了多少、上限多少、没有确认页
    assert "That run cost $19.23" in page and "$30.00" in page and "no confirmation page" in page
    assert "Measure again now (about $19.23)" in page

    started = client.post(f"/projects/{project_id}/remeasure/start")
    assert started.status_code == 303 and started.headers["location"] == "http://127.0.0.1:8600/jobs/feed00000001"
    saved = projects.load(tmp_path / "runs", project_id)
    assert backend.retests == ["job1"] and (saved.baseline_job, saved.retest_pending) == ("job1", "feed00000001")
    client.post(f"/projects/{project_id}/remeasure/start")  # 连点两下:只起一单,不花两份钱
    assert backend.retests == ["job1"]
    page = client.get(f"/projects/{project_id}").text
    assert "A re-measurement is in the measurement tool (feed00000001)." in page and "/remeasure/start" not in page


def test_a_refused_remeasurement_changes_nothing(client: TestClient, backend: FakeBackend,  # noqa: F811
                                                 tmp_path: Path) -> None:
    project_id = _measured(client)
    client.post(f"/projects/{project_id}/deploy", data={"day": TODAY})
    backend.retest_error = "测量工具没有开始再测"
    failed = client.post(f"/projects/{project_id}/remeasure/start")
    assert failed.status_code == 502 and "测量工具没有开始再测" in failed.text
    saved = projects.load(tmp_path / "runs", project_id)
    assert (saved.baseline_job, saved.retest_pending) == (None, None)


def test_a_finished_remeasurement_is_compared_and_shown(client: TestClient, backend: FakeBackend,  # noqa: F811
                                                        tmp_path: Path) -> None:
    project_id = _measured(client)
    folder = tmp_path / "runs" / project_id
    client.post(f"/projects/{project_id}/deploy", data={"day": TODAY})
    client.post(f"/projects/{project_id}/remeasure/start")
    backend.jobs.append(RETEST)
    backend.jobs.append({**RETEST, "job_id": "other9", "parent": "job2"})
    page = client.get(f"/projects/{project_id}").text
    assert "Finished re-measurements" in page and 'value="feed00000001"' in page
    assert page.count("Compare with this one") == 1  # 别的单的再测不列出来

    wrong = client.post(f"/projects/{project_id}/remeasure/attach", data={"job_id": "other9"})
    assert wrong.status_code == 400 and "不是 job1 这一单的再测" in wrong.text and backend.compare_calls == []
    backend.compare_error = "对比没做成(退出码 1)"
    broken = client.post(f"/projects/{project_id}/remeasure/attach", data={"job_id": "feed00000001"})
    assert broken.status_code == 400 and "The comparison did not work" in broken.text
    assert projects.load(tmp_path / "runs", project_id).compare_file is None

    backend.compare_error = None
    done = client.post(f"/projects/{project_id}/remeasure/attach", data={"job_id": "feed00000001"})
    assert done.status_code == 303
    assert backend.compare_calls == [(b"PK-export-of-job1", b"PK-export-of-feed00000001", TODAY)]
    assert backend.site_runs == [None]  # 还没读过网站:没有可以用来分组的运行
    client.post(f"/projects/{project_id}/improve/start")
    client.post(f"/projects/{project_id}/remeasure/attach", data={"job_id": "feed00000001"})
    assert backend.site_runs == [None, "RUN_DIR"]  # 读过网站以后再比:把那次运行递过去,按页面分组
    assert (folder / BEFORE_NAME).is_file() and (folder / AFTER_NAME).is_file() and (folder / COMPARE_NAME).is_file()
    saved = projects.load(tmp_path / "runs", project_id)
    assert (saved.retest_job, saved.retest_pending, saved.compare_file, saved.compare_note) == (
        "feed00000001", None, COMPARE_NAME, "")
    page = client.get(f"/projects/{project_id}").text
    assert "Compared run job1 with run feed00000001." in page and "The comparison did not work" not in page

    report = client.get(f"/projects/{project_id}/report").text
    assert "16 / 53 (30%)" in _side(report, "before") and "20 / 55 (36%)" in _side(report, "after")
    assert f"/projects/{project_id}/compare?lang=zh" in page  # 第 5 步上有单独对比报告的入口
    alone = client.get(f"/projects/{project_id}/compare?lang=zh")
    assert alone.status_code == 200 and "Brand · 改前改后对比" in alone.text
    assert "53 / 16" not in alone.text and "16 / 53（30%）" in _side(alone.text, "before")
    assert (folder / "compare-zh.html").read_text(encoding="utf-8") == alone.text


def _side(page: str, tone: str) -> str:
    """页面里改之前或改之后那一页的 HTML。"""
    return page.split(f'<section class="side {tone}"')[1].split("</section>")[0]


def test_no_comparison_report_before_there_is_a_comparison(client: TestClient) -> None:  # noqa: F811
    project_id = _measured(client)
    assert client.get(f"/projects/{project_id}/compare").status_code == 404
    assert "/compare?lang=" not in client.get(f"/projects/{project_id}").text


def test_before_and_after_are_two_pages_side_by_side_not_one_table() -> None:
    """用户 2026-11-27:改前改后不放在同一张表里比,左边改之前一页、右边改之后一页,两页不同颜色。"""
    page = render(build(Sources(toast=_toast(), compare=COMPARE), "en", generated_at=NOW))
    chapter = page.split('<section id="c9"')[1].split('<section id="c10"')[0]
    assert chapter.count('<section class="side before"') == 1 and chapter.count('<section class="side after"') == 1
    assert chapter.index("side before") < chapter.index("side after")  # 改之前在左(在前)
    before, after = _side(page, "before"), _side(page, "after")
    assert "Before" in before and "Measured on 2026-11-03" in before
    assert "After" in after and "Measured on 2026-11-24" in after
    assert "16 / 53 (30%)" in before and "0 / 60 (0%)" in before and "20 / 55 (36%)" not in before
    assert "20 / 55 (36%)" in after and "3 / 60 (5%)" in after and "16 / 53 (30%)" not in after
    assert "→" not in chapter  # 不再把前后挤在一格里
    for side in (before, after):  # 两页一样的行,同一个 AI 在同一行
        assert side.index("Alpha API · a-one") < side.index("Beta API · b-two")
    css = page.split("<style>")[1].split("</style>")[0]
    assert "--before: #2a78d6" in css and "--after: #eb6834" in css  # 两页两种颜色
    assert ".side.after { --tone: var(--after); }" in css


def test_the_comparison_report_stands_alone() -> None:
    for lang in ("en", "zh"):
        page = render(build_comparison(COMPARE, lang, generated_at=NOW, brand="Brand"))
        w = WORDS[lang]
        assert f"<h1>{w['cmp_title'].format(brand='Brand')}</h1>" in page
        assert "<nav" not in page and "<h2" not in page  # 只有这一件事:没有目录,没有章号
        assert page.count('<section class="side before"') == 1 and page.count('<section class="side after"') == 1
        assert w["c9_listed_only"] in page and w["c9_deploy"].format(deploy="2026-11-05") in page
    assert "<h1>Before and after</h1>" in render(build_comparison(COMPARE, "en", generated_at=NOW))


def test_the_same_reason_is_given_once() -> None:
    legs = [{**leg, "reason": "这一批改动还没部署", "label_text": "未部署"} for leg in COMPARE["legs"]]
    text = _text(_chapter(build(Sources(toast=_toast(), compare={**COMPARE, "legs": legs}), "zh", generated_at=NOW),
                          "compare"))
    assert text.count("这一批改动还没部署") == 1 and "上面所有的数：未部署 · 这一批改动还没部署" in text


def test_chapter_nine_lists_numbers_and_gives_no_verdict() -> None:
    for lang in ("en", "zh"):
        report = build(Sources(toast=_toast(), compare=COMPARE), lang, generated_at=NOW)
        text = _text(_chapter(report, "compare"))
        w = WORDS[lang]
        assert w["c9_listed_only"] in text and render(report).count(w["c9_listed_only"]) == 1
        assert "2026-11-03" in text and "2026-11-05" in text and "2026-11-24" in text
        assert "不可比:样本不够" not in text.split(w["c9_remarks"])[0]  # 「没有波动数据」只统一说一次,不逐行重复
        assert "Beta API · b-two" in text and "基线里搜索过的回答只占 24%" in text  # 别的原因照实列出来
        assert "Gamma API · g-three" in text
    en = _text(_chapter(build(Sources(toast=_toast(), compare=COMPARE), "en", generated_at=NOW), "compare"))
    assert "all 20 questions that do not name your brand" in en
    assert "0 / 60 (0%)" in en and "3 / 60 (5%)" in en and "32 / 46 (70%)" in en and "30 / 44 (68%)" in en
    zh = _text(_chapter(build(Sources(toast=_toast(), compare=COMPARE), "zh", generated_at=NOW), "compare"))
    assert "16 / 53（30%）" in zh and "20 / 55（36%）" in zh
    no_day = _text(_chapter(build(Sources(toast=_toast(), compare={**COMPARE, "deploy_day": None}), "zh",
                                  generated_at=NOW), "compare"))
    assert WORDS["zh"]["c9_deploy_none"] in no_day
    for word in ("improved", "got better", "变好了", "上升", "下降"):  # 不说变没变
        assert word not in en and word not in zh
