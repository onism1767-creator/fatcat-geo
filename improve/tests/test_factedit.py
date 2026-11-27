"""站长改事实:改过的值优先、原值不丢、按改过的事实重出改动包(不重新读网站)、旧的一批怎么处理。全部不联网。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from butter import cards, store
from butter.contracts import FactEdit
from butter.factedit import apply_edits, changed_edits, editable_fields
from butter.jobs import JobManager
from butter.pipeline import RunOptions, process_snapshot
from butter.web.app import WebSettings, create_app

from conftest import NOW

HOST = "www.acme-kettles.test"
BASE = "http://127.0.0.1:8700"
ORIGIN = {"origin": BASE}


def edit(field: str, value: str, at: str = "2026-11-18T10:00:00+00:00", host: str = HOST, note: str = "") -> FactEdit:
    return FactEdit(site_host=host, field=field, value=value, note=note, at=at)


def test_owner_value_wins_and_the_read_value_is_kept(acme_factbase):
    product = acme_factbase.products[0]
    path = f"products[{product.product_id}].price"
    fb, skipped = apply_edits(acme_factbase, [edit(path, "39.00", note="网站上是划线价"), edit("entity.name", "Acme")])
    assert not skipped
    price = fb.products[0].price
    assert price.values[0].value == "39.00" and price.values[0].source_kind == "owner_edit"
    assert price.values[0].source_url == "站长在 Butter 里改的" and price.values[0].source_path == "网站上是划线价"
    assert "49.00" in [v.value for v in price.alternatives] and not price.conflict         # 原来读到的值还在
    assert all(v.source_kind != "owner_edit" for v in price.alternatives)
    assert fb.entity.name.values[0].value == "Acme" and not fb.entity.name.conflict
    assert acme_factbase.products[0].price.values[0].value == "49.00"                       # 原对象不变(不可变)


def test_latest_edit_wins_other_sites_and_unknown_fields_are_skipped(acme_factbase):
    fb, skipped = apply_edits(acme_factbase, [
        edit("entity.name", "First"), edit("entity.name", "Second", at="2026-11-18T11:00:00+00:00"),
        edit("entity.name", "Other site", host="other.test"),
        edit("products[no-such].price", "1"), edit("entity.logo", "x"), edit("entity.category", "kettles")])
    assert fb.entity.name.values[0].value == "Second" and fb.entity.category.values[0].value == "kettles"
    assert len(skipped) == 2 and all("没用上" in s for s in skipped)
    cleared, _ = apply_edits(acme_factbase, [edit("entity.description", "")])
    assert cleared.entity.description.values == [] and cleared.entity.description.alternatives     # 清空 = 不用这个值
    again, _ = apply_edits(cleared, [edit("entity.description", "New text")])
    assert [v.source_kind for v in again.entity.description.alternatives] != ["owner_edit"]


def test_only_real_changes_become_edits(acme_factbase):
    rows = {r["field"]: r["value"] for r in editable_fields(acme_factbase)}
    assert rows["entity.name"] == "Acme Kettles" and any(f.endswith("].price") for f in rows)
    submitted = dict(rows) | {"entity.name": "  Acme  Kettle Co ", "entity.unknown": "x"}
    edits = changed_edits(acme_factbase, submitted, note=" 改个名 ", at="t")
    assert [(e.field, e.value, e.note, e.site_host) for e in edits] == [("entity.name", "Acme Kettle Co", "改个名", HOST)]
    assert changed_edits(acme_factbase, rows, note="", at="t") == []


def test_pipeline_uses_edited_facts_in_the_package(tmp_path, acme_snapshot, acme_factbase):
    path = f"products[{acme_factbase.products[0].product_id}].price"
    options = RunOptions(out_root=tmp_path, fact_edits=(edit(path, "39.00"), edit("entity.bogus", "x")))
    result = process_snapshot(acme_snapshot, options, now=lambda: NOW, run_id="edited")
    jsonld = "\n".join(text for name, text in result.bundle.files.items() if name.startswith("jsonld/product"))
    assert '"39.00"' in jsonld and '"49.00"' not in jsonld                    # 改动包里用的是改过的价格
    assert "站长在 Butter 里改的" in (result.run_dir / "factbase.md").read_text(encoding="utf-8")
    meta = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["owner_edits"] == 1 and any("没用上" in w for w in meta["warnings"])


def setup_site(tmp_path, acme_snapshot):
    jobs = JobManager(tmp_path / "out", tmp_path / "workspace", env={}, fetch=lambda url, n: acme_snapshot,
                      start_worker=False)
    settings = WebSettings(out_root=jobs.out_root, workspace=jobs.workspace, token="tok", jobs=jobs)
    client = TestClient(create_app(settings), base_url=BASE)
    jobs.submit_run("https://www.acme-kettles.test/")
    jobs.run_next()
    first = next(iter(jobs.jobs().values())).result
    return client, jobs, first


def test_edit_facts_in_the_browser_then_recompute_without_refetching(tmp_path, acme_snapshot):
    fetched = []

    def fetch(url, n):
        fetched.append(url)
        return acme_snapshot

    client, jobs, first = setup_site(tmp_path, acme_snapshot)
    jobs._fetch = fetch
    page = client.get(f"/runs/{first['run_id']}/facts")
    assert page.status_code == 200 and "改事实" in page.text and 'name="fact:entity.name"' in page.text
    data = client.get(f"/runs/{first['run_id']}/facts.json").json()
    price_field = next(r["field"] for g in data["groups"] for r in g["rows"] if r["field"].endswith("].price"))
    form = {"token": "tok", "note": "网站上是划线价", f"fact:{price_field}": "39.00", "fact:entity.name": "Acme Kettles"}
    posted = client.post(f"/runs/{first['run_id']}/facts", data=form, headers=ORIGIN, follow_redirects=False)
    assert posted.status_code == 303 and posted.headers["location"].startswith("/jobs/")
    assert [e.value for e in store.read_fact_edits(jobs.workspace, HOST)] == ["39.00"]     # 只记真改了的那一处
    assert jobs.run_next()
    done = client.get(posted.headers["location"] + ".json").json()
    assert done["state"] == "DONE" and done["run_id"] != first["run_id"] and fetched == []   # 没有重新读网站
    assert any("没有让模型重写" in n for n in done["notes"])
    new_run = client.get(f"/runs/{done['run_id']}.json").json()
    assert any(p["price"].startswith("39.00") for p in new_run["products"])
    assert "你改过" in client.get(f"/runs/{done['run_id']}/facts").text
    view, _ = cards.load(jobs.workspace)
    phases = {b.batch.run_id: b.phase for b in view.batches}
    assert phases == {first["run_id"]: "已放弃", done["run_id"]: "已出包"}          # 旧的一批没动过,标成放弃
    old = next(b for b in view.batches if b.batch.run_id == first["run_id"])
    assert "事实改过" in old.abandoned_reason

    # 之后再从头读一次这个站,改过的事实照样套用
    jobs.submit_run("https://www.acme-kettles.test/")
    jobs.run_next()
    latest = sorted(jobs.jobs().values(), key=lambda j: j.updated_at)[-1].result
    assert any(p["price"].startswith("39.00") for p in client.get(f"/runs/{latest['run_id']}.json").json()["products"])


def test_saving_nothing_or_without_token_is_refused(tmp_path, acme_snapshot):
    client, jobs, first = setup_site(tmp_path, acme_snapshot)
    url = f"/runs/{first['run_id']}/facts"
    same = {"token": "tok", "fact:entity.name": "Acme Kettles"}
    assert client.post(url, data=same, headers=ORIGIN).status_code == 400
    assert client.post(url, data={**same, "fact:entity.name": "X", "token": "no"}, headers=ORIGIN).status_code == 403
    assert client.post(url, data={**same, "fact:entity.name": "X"}).status_code == 403
    assert client.post("/runs/nope/facts", data={**same, "fact:entity.name": "X"}, headers=ORIGIN).status_code == 404
    assert store.read_fact_edits(jobs.workspace) == [] and len(jobs.jobs()) == 1


def test_a_batch_already_measured_or_deployed_is_left_alone(tmp_path, acme_snapshot):
    from datetime import UTC, datetime

    from butter.contracts import DeployMark

    client, jobs, first = setup_site(tmp_path, acme_snapshot)
    view, _ = cards.load(jobs.workspace)
    card_ids = [c.card_id for c in view.cards]
    decision = cards.submit(jobs.workspace, at=datetime.now(UTC), actor="owner", batch_id=first["batch_id"],
                            type="deployed", payload=DeployMark(day="2026-11-18", card_ids=card_ids))
    assert decision.ok, decision.reason
    store.append_fact_edits(jobs.workspace, [edit("entity.name", "Acme")])
    jobs.submit_recompute(first["run_id"])
    jobs.run_next()
    done = sorted(jobs.jobs().values(), key=lambda j: j.updated_at)[-1]
    assert done.state == "DONE" and any("没有动它" in n for n in done.result["notes"])
    view, _ = cards.load(jobs.workspace)
    assert {b.batch.run_id: b.phase for b in view.batches}[first["run_id"]] != "已放弃"
