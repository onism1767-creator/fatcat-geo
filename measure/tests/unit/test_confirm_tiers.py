"""确认页三张档位卡片 + 自定义 + 预估按档位(S9.1;架构书附录 v0.4.27 第 3 节;用户定默认「中」)。
页面流用假仪器与桩执行器,零网络零数据库。"""

from fastapi.testclient import TestClient

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from engine.models import Target
from engine.tiers import MentionMethod
from report.app import create_app
from report.jobs import DiagnosisJob
from tests.unit.test_report_page import REGISTRY, UI_FORBIDDEN, DummyAdapter, visible_text


class TwoModelAdapter(SurfaceAdapter):
    """两个型号:m_cheap 是消费端默认、m_top 是最强——三档挑出来的型号不一样,卡片才有区别。"""

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="S2", api_name="S2 API", provider="test_provider2", surface="s2",
                           models=["m_cheap", "m_top"], default_model="m_cheap",
                           consumer_default_model="m_cheap", strongest_model="m_top", models_checked="test",
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={"m_cheap": 0.01, "m_top": 0.10})

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def _client() -> tuple[TestClient, list]:
    calls: list = []

    def stub_runner(*, job, adapters, dsn, evidence, replay):
        calls.append(job.job_id)

    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter(), "s2": TwoModelAdapter()},
                     dsn="postgresql://unused", evidence=REGISTRY, job_runner=stub_runner,
                     calibration=None)  # 页面测试不读本机的校准文件:「还没校准」的样子也要能测
    return TestClient(app), calls


def _confirm_url(client: TestClient, questions: str = "问题一?\n问题二?") -> str:
    resp = client.post("/jobs?lang=zh", data={"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com",
                                             "category": "CRM", "questions": questions, "generate": ""},
                       follow_redirects=False)
    return resp.headers["location"]


def _job_of(client: TestClient, url: str) -> DiagnosisJob:
    return client.app.state.ctx.registry.get(url.split("/jobs/")[1].split("/")[0].split("?")[0])


def test_confirm_page_shows_three_tier_cards_with_medium_selected():
    client, _ = _client()
    html = client.get(_confirm_url(client)).text
    assert 'name="tier" value="low"' in html and 'name="tier" value="high"' in html
    assert 'name="tier" value="medium" checked' in html  # 默认「中」(用户定)
    assert "低档" in html and "中档" in html and "高档" in html
    # 低档每家一个消费端默认型号;中档 s2 两个(最强 + 默认)、s1 一个;高档全部
    assert "测 2 个 AI 型号:S1 · m1;S2 API · m_cheap" in html
    assert "测 3 个 AI 型号:S1 · m1;S2 API · m_top;S2 API · m_cheap" in html
    assert "每道题原样问 5 次" in html and "每道题原样问 7 次" in html
    assert "按你的名字和别名精确匹配" in html  # 「提到你」怎么认,从配置来
    assert "自动校准还没做" in html  # 准确率对比只写实测,没有就说没有
    # 自定义区:型号勾选跟默认档(中档)、次数默认 5、范围 3–10
    assert 'value="s2::m_top" checked' in html and 'value="s2::m_cheap" checked' in html
    assert 'name="runs" step="1" min="3" max="10" value="5"' in html
    text = visible_text(html)
    assert not any(token in text for token in UI_FORBIDDEN), text[:300]
    en = client.get(_confirm_url(client).replace("lang=zh", "lang=en")).text
    assert "Medium" in en and "asked 5 times" in en and "models checked:" in en


def test_estimate_endpoint_returns_every_tier_and_flags_custom():
    client, _ = _client()
    url = _confirm_url(client)
    path = url.split("?")[0].rsplit("/", 1)[0]
    base = {"prompts_selected": ["0", "1"], "custom": ""}
    low = client.post(f"{path}/estimate", data={**base, "tier": "low", "runs": "3",
                                                "targets_selected": ["s1::m1", "s2::m_cheap"]}).json()
    assert low["calls"] == 2 * 3 * 2 and low["custom"] is False
    assert low["tiers"]["low"]["calls"] == 12 and low["tiers"]["medium"]["calls"] == 2 * 5 * 3
    assert low["tiers"]["high"]["calls"] == 2 * 7 * 3
    assert low["tiers"]["medium"]["cost_usd"] == round(10 * (0.005 + 0.10 + 0.01), 4)
    # 改了次数 → 自定义;只改型号也是自定义
    more = client.post(f"{path}/estimate", data={**base, "tier": "low", "runs": "4",
                                                 "targets_selected": ["s1::m1", "s2::m_cheap"]}).json()
    assert more["calls"] == 2 * 4 * 2 and more["custom"] is True
    fewer = client.post(f"{path}/estimate", data={**base, "tier": "low", "runs": "3",
                                                  "targets_selected": ["s1::m1"]}).json()
    assert fewer["custom"] is True
    bad = client.post(f"{path}/estimate", data={**base, "tier": "low", "runs": "11", "targets_selected": ["s1::m1"]})
    assert bad.status_code == 400 and bad.json() == {"error": "FORM_RUNS_RANGE"}


def test_confirm_freezes_the_tier_and_the_runner_reads_it():
    client, calls = _client()
    url = _confirm_url(client)
    resp = client.post(url, data={"prompts_selected": ["0", "1"], "custom": "", "tier": "low", "runs": "3",
                                  "targets_selected": ["s1::m1", "s2::m_cheap"]}, follow_redirects=False)
    assert resp.status_code == 303 and "error" not in resp.headers["location"]
    job = _job_of(client, resp.headers["location"])
    assert calls == [job.job_id]
    assert job.tier is not None and (job.tier.name, job.tier.runs_per_prompt, job.tier.custom) == ("low", 3, False)
    assert job.tier.mention_method is MentionMethod.RULES
    assert job.targets == [Target(surface="s1", model="m1"), Target(surface="s2", model="m_cheap")]
    assert job.policy().runs_per_prompt == 3  # 调度按档位跑
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    assert restored.tier == job.tier and restored.fingerprint == job.fingerprint  # 档位进快照、进指纹
    assert "× 3 次" in client.get(f"/jobs/{job.job_id}?lang=zh").text  # 任务页写的是本单的次数


def test_custom_choice_keeps_the_base_tier_for_the_mention_method():
    client, _ = _client()
    url = _confirm_url(client)
    resp = client.post(url, data={"prompts_selected": ["0"], "custom": "", "tier": "high", "runs": "4",
                                  "targets_selected": ["s2::m_top"]}, follow_redirects=False)
    job = _job_of(client, resp.headers["location"])
    assert job.tier is not None and job.tier.custom is True and job.tier.name == "high"  # 起点档留着:认法跟它走
    assert job.tier.runs_per_prompt == 4 and job.targets == [Target(surface="s2", model="m_top")]


def test_tier_and_runs_are_validated_with_plain_errors():
    client, _ = _client()
    url = _confirm_url(client)
    base = {"prompts_selected": ["0"], "custom": "", "targets_selected": ["s1::m1"]}
    no_tier = client.post(url, data={**base, "runs": "3"}, follow_redirects=False)
    assert "error=FORM_BAD_TIER" in no_tier.headers["location"]
    too_many = client.post(url, data={**base, "tier": "low", "runs": "11"}, follow_redirects=False)
    assert "error=FORM_RUNS_RANGE" in too_many.headers["location"]
    page = visible_text(client.get(too_many.headers["location"]).text)
    assert "每题次数请填 3 到 10" in page
    not_int = client.post(url, data={**base, "tier": "low", "runs": "three"}, follow_redirects=False)
    assert "error=FORM_RUNS_RANGE" in not_int.headers["location"]


def test_fingerprint_changes_with_tier_runs():
    client, _ = _client()
    prints = []
    for runs in ("3", "4"):
        url = _confirm_url(client)
        resp = client.post(url, data={"prompts_selected": ["0"], "custom": "", "tier": "low", "runs": runs,
                                      "targets_selected": ["s1::m1"]}, follow_redirects=False)
        prints.append(_job_of(client, resp.headers["location"]).fingerprint)
    assert prints[0] != prints[1]  # 每题次数是口径的一部分,进指纹


def test_confirm_page_offers_spellings_and_ticked_ones_become_aliases():
    """S9.2b(用户定「甲」):确认页列 AI 查到的写法与网页里的次数,出现过的预勾、太短的不勾;勾上的进别名表、进指纹。"""
    from engine.spellings import SpellingCandidate

    client, _ = _client()
    url = _confirm_url(client)
    job = _job_of(client, url)
    job.spellings_suggested = [
        SpellingCandidate(text="Aurora CRM", reason="英文名加空格", pages_seen=5, pages_with=3, occurrences=7,
                          short=False),
        SpellingCandidate(text="JG", reason="拼音首字母", pages_seen=5, pages_with=2, occurrences=2, short=True),
        SpellingCandidate(text="Aurora", reason="只写前半截", pages_seen=5, pages_with=0, occurrences=0, short=False),
    ]
    html = client.get(url).text
    assert "AI 查到的你的常见写法" in html and "共 5 个网页" in html
    assert 'name="spellings_selected" value="0" checked' in html and "在 3 个网页里出现 7 次" in html
    assert 'name="spellings_selected" value="1" >' in html and "太短,容易把别家认成你" in html
    assert 'name="spellings_selected" value="2" >' in html and "查到的网页里没出现" in html
    base = {"prompts_selected": ["0"], "custom": "", "tier": "low", "runs": "3", "targets_selected": ["s1::m1"]}
    client.post(url, data={**base, "spellings_selected": ["0", "1"]}, follow_redirects=False)
    assert job.brand.aliases == ["Aurora CRM", "JG"] and job.fingerprint
    plain = _confirm_url(client)
    other = _job_of(client, plain)
    client.post(plain, data=base, follow_redirects=False)
    assert other.brand.aliases == [] and other.fingerprint != job.fingerprint  # 别名是口径的一部分
