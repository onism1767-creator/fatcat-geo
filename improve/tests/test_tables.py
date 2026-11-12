"""UA 表与「未纳入厂商」表:只收有官方文档的厂商,每条带文档地址与核对日期;条目 ID 与 Toast 注册表对得上。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from butter import registry_ids as ids
from butter import sources

UA = sources.load_ua_table()
EXCLUDED = sources.load_excluded_vendors()
TOAST_REGISTRY = Path(__file__).resolve().parents[3] / "你的网页被增强了快上" / "config" / "evidence_registry.yaml"


def test_every_entry_has_official_doc_url_and_check_date():
    assert len(UA) >= 25
    for e in UA:
        assert e.docs_url.startswith("https://"), e.name
        assert date(2026, 11, 1) <= date.fromisoformat(e.checked_on) <= date(2026, 12, 31), e.name
        assert e.checked_how and e.robots_stance and "\n" not in e.robots_stance
        assert e.robots_token.isascii() and " " not in e.robots_token
        assert (e.user_agent is None) == (e.user_agent_checked == "not_given"), e.name
    tokens = [e.robots_token.lower() for e in UA]
    assert len(tokens) == len(set(tokens))


def test_kimi_three_user_agents():
    kimi = {e.robots_token: e for e in UA if e.vendor.startswith("Kimi")}
    assert {k: v.purpose for k, v in kimi.items()} == {
        "Kimi-SearchBot": "search_index", "KimiBot": "training", "Kimi-User": "user_fetch"}
    assert all("kimi" in e.docs_url for e in kimi.values())


def test_covers_the_search_training_and_user_lists_from_research():
    by_purpose = {p: {e.robots_token for e in UA if e.purpose == p} for p in ("search_index", "training", "user_fetch", "ai_uses_token")}
    assert {"OAI-SearchBot", "Googlebot", "bingbot", "Claude-SearchBot", "PerplexityBot", "meta-webindexer", "Applebot",
            "Amzn-SearchBot", "DuckAssistBot", "YouBot", "MistralAI-Index", "Kimi-SearchBot"} == by_purpose["search_index"]
    assert {"GPTBot", "ClaudeBot", "meta-externalagent", "Amazonbot", "MistralAI-Training", "KimiBot"} == by_purpose["training"]
    assert {"Google-Extended", "Applebot-Extended"} == by_purpose["ai_uses_token"]
    assert {"ChatGPT-User", "Claude-User", "Perplexity-User", "Kimi-User", "Amzn-User", "meta-externalfetcher",
            "MistralAI-User"} == by_purpose["user_fetch"]


def test_excluded_vendors_table():
    names = {v.vendor for v in EXCLUDED}
    assert {"字节跳动", "腾讯", "百度", "阿里巴巴", "DeepSeek", "智谱", "MiniMax"} <= names
    for v in EXCLUDED:
        assert v.reason and v.source and date.fromisoformat(v.checked_on)
    in_table = " ".join(e.vendor + e.name for e in UA).lower()
    for banned in ("bytespider", "doubao", "yisou", "baiduspider", "deepseek", "sogou", "grok", "xai"):
        assert banned not in in_table, f"没有官方文档的 UA 不得进表:{banned}"


def test_registry_ids_are_well_formed():
    assert len(ids.BUTTER_EXTENSION) == 11 and not ids.BUTTER_EXTENSION & ids.TOAST_EXISTING
    assert all(i.startswith("EVIDENCE_") and i.endswith("_V1") for i in ids.ALL_IDS)


@pytest.mark.skipif(not TOAST_REGISTRY.exists(), reason="本机没有 Toast 仓库,跳过对照")
def test_toast_ids_exist_in_toast_registry_and_ours_do_not_collide():
    entries = yaml.safe_load(TOAST_REGISTRY.read_text(encoding="utf-8"))["entries"]  # 只读
    toast_ids = {e["id"] for e in entries}
    assert ids.TOAST_EXISTING <= toast_ids
    assert not ids.BUTTER_EXTENSION & toast_ids, "扩展条目不能与 Toast 现有条目重名"
