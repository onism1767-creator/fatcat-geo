"""本地服务：页面能打开、每页有 JSON 孪生、出题勾选能存、只认本机、改东西要令牌。全部不联网。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from butter.jobs import JobManager
from butter.pipeline import RunOptions, process_snapshot
from butter.web.app import WebSettings, create_app
from butter.web.views import validation_text

from conftest import NOW

BASE = "http://127.0.0.1:8700"
ORIGIN = {"origin": BASE}


@pytest.fixture()
def site(tmp_path, acme_snapshot):
    out = tmp_path / "out"
    result = process_snapshot(acme_snapshot, RunOptions(out_root=out), now=lambda: NOW, run_id="run-web")
    workspace = tmp_path / "workspace"
    env = {"ANTHROPIC_API_KEY": "super-secret-value"}
    jobs = JobManager(out, workspace, env=env, fetch=lambda url, max_pages: acme_snapshot, start_worker=False)
    settings = WebSettings(out_root=out, workspace=workspace, token="test-token", env=env, jobs=jobs)
    return TestClient(create_app(settings), base_url=BASE), result, settings


def test_every_page_opens_and_has_a_json_twin(site):
    client, _, _ = site
    for path in ("/", "/runs/run-web", "/runs/run-web/questions", "/batches", "/registry", "/settings"):
        page = client.get(path)
        assert page.status_code == 200 and "<html lang=\"zh-CN\">" in page.text, path
        assert 'name="color-scheme" content="light dark"' in page.text
        twin = client.get(("/index" if path == "/" else path) + ".json")
        assert twin.status_code == 200 and twin.json()["title"], path


def test_home_lists_runs_by_site(site):
    client, _, _ = site
    data = client.get("/index.json").json()
    assert data["run_count"] == 1 and data["sites"][0]["host"] == "www.acme-kettles.test"
    assert data["sites"][0]["runs"][0]["run_id"] == "run-web"
    html = client.get("/").text
    assert 'href="/runs/run-web"' in html and "到期" not in html and "提醒" not in html  # 不催站长测


def test_run_page_shows_facts_audit_and_package(site):
    client, result, _ = site
    data = client.get("/runs/run-web.json").json()
    assert len(data["changes"]) == len(result.bundle.package.items) and data["pages"] == 4
    assert data["facts"][0] == {"label": "名称", "value": "Acme Kettles", "source": data["facts"][0]["source"], "conflict": True}  # 夹具站的名称故意写了两种
    assert sum(data["audit_counts"].values()) == len(result.audit.findings)
    assert data["draft"] is None
    html = client.get("/runs/run-web").text
    assert "下载改动包" in html and "检查结果" in html and "Acme Classic Kettle" in html
    assert "<script" not in html.lower()                       # 零 JavaScript;抓来的文字只当文字显示
    assert "&lt;/script&gt;" in html or "alert(" not in html     # 夹具里的恶意商品描述被转义


def test_package_download_is_a_zip_of_the_change_package(site):
    client, result, _ = site
    response = client.get("/runs/run-web/package.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert 'filename="butter-change-package-run-web.zip"' in response.headers["content-disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
    assert names == set(result.bundle.files)


def test_unknown_or_tricky_run_ids_are_404(site):
    client, _, _ = site
    for path in ("/runs/nope", "/runs/..%2F..%2Fsecret", "/runs/run-web/../../x", "/batches/nope"):
        assert client.get(path).status_code == 404, path
    assert "回首页" in client.get("/runs/nope").text


def test_questions_page_generates_matches_and_saves(site):
    client, _, settings = site
    empty = client.get("/runs/run-web/questions.json").json()
    assert empty["groups"] == [] and empty["from_facts"] == [] and empty["brand"] == "Acme Kettles" and empty["lang"] == "en"
    assert "你的网站上没有写品类" in client.get("/runs/run-web/questions").text
    data = client.get("/runs/run-web/questions.json", params={"categories": "kettles", "lang": "en"}).json()
    assert data["total"] == 24 and len(data["groups"]) == 4 and data["matched"] == 20
    assert data["groups"][2]["needs_edit"] and data["groups"][3]["names_brand"]
    html = client.get("/runs/run-web/questions", params={"categories": "kettles", "lang": "en"}).text
    assert "这 6 道比较泛，可以改得更贴你的行业" in html and 'name="pick-g1-d01"' in html and 'value="test-token"' in html

    form = {"token": "test-token", "lang": "en", "categories": "kettles", "brand": "Acme Kettles",
            "pick-g1-d01": "on", "text-g1-d01": "What are the best kettles for beginners?",
            "pick-g1-l01": "on", "text-g1-l01": "  What kettles suit a small café?  ",
            "text-g1-c01": "not picked"}
    saved = client.post("/runs/run-web/questions", data=form, headers=ORIGIN, follow_redirects=False)
    assert saved.status_code == 303 and "/runs/run-web/questions?saved=" in saved.headers["location"]
    files = list((settings.workspace / "panels" / "run-web").glob("*/panel.json"))
    assert len(files) == 1
    panel = json.loads(files[0].read_text(encoding="utf-8"))
    assert panel["source"] == "butter_generated" and panel["categories"] == ["kettles"] and panel["lang"] == "en"
    assert panel["questions"] == [
        {"id": "g1-d01", "intent": "discovery", "text": "What are the best kettles for beginners?", "edited": False},
        {"id": "g1-l01", "intent": "local", "text": "What kettles suit a small café?", "edited": True}]
    after = client.get(saved.headers["location"])
    assert "存好了" in after.text and "已经存过的" in after.text


def test_saving_needs_the_token_a_local_origin_and_at_least_one_question(site):
    client, _, settings = site
    form = {"token": "test-token", "lang": "en", "categories": "kettles", "pick-g1-d01": "on", "text-g1-d01": "Q?"}
    assert client.post("/runs/run-web/questions", data={**form, "token": "wrong"}, headers=ORIGIN).status_code == 403
    assert client.post("/runs/run-web/questions", data=form).status_code == 403                       # 没有来源
    assert client.post("/runs/run-web/questions", data=form, headers={"origin": "https://evil.example"}).status_code == 403
    none_picked = {k: v for k, v in form.items() if not k.startswith("pick-")}
    assert client.post("/runs/run-web/questions", data=none_picked, headers=ORIGIN).status_code == 400
    assert client.post("/runs/run-web/questions", data={**form, "categories": " "}, headers=ORIGIN).status_code == 400
    assert not (settings.workspace / "panels").exists()                                               # 什么都没写


def test_only_local_hosts_are_served(site):
    client, _, _ = site
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8700"}).status_code == 200
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in response.headers["content-security-policy"]


def test_registry_and_settings_speak_plainly_and_never_show_keys(site, monkeypatch):
    client, _, _ = site
    reg = client.get("/registry.json").json()
    assert reg["total"] == 33 and all(e["plain"] for e in reg["entries"])
    data = client.get("/settings.json").json()
    assert [k["ready"] for k in data["keys"]] == [True, False, False]
    html = client.get("/settings").text
    assert "super-secret-value" not in html and "super-secret-value" not in json.dumps(data)
    interval = next(d for d in data["defaults"] if d["key"] == "remeasure.interval_days")
    assert interval["value"] == 28 and interval["label"] == "改完之后，推荐隔多久再测"
    for word in ("σ", "τ", "单次观测", "临时", "预注册", "事后", "bootstrap"):   # 界面不出现研究用语(设计 §11)
        assert word not in html, word


def test_validation_states_in_plain_words():
    assert validation_text("假设") == "还没证实" and validation_text("实验中") == "正在试"
    assert validation_text("单次观测", "变多") == "测到一次变多" and validation_text("单次观测", "变少") == "测到一次变少"
    assert validation_text("已复现", "变多") == "两次都测到变多" and validation_text("可泛化", "变少") == "很多行业都测到变少"
