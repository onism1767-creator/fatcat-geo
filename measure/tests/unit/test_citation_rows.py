"""单条回答页与结果文件共用的引用行(S8.3):序号按引用顺序编。来源归属记录里的 position 是第三方网页在哪里提到你
(title / lead / body),原来被当成序号,一碰到正文提到你的引用就整页 500(小米真跑 2026-11-03 查到)。零网络零数据库。"""

from report.response_view import citation_rows
from tests.unit.test_response_page import _run


def test_citation_rows_number_citations_in_order_whatever_the_page_position_says():
    run = _run("p", run_id="c1", index=1)
    attribution = {"cited": [
        {"url": "https://a.example/x", "source_class": "THIRD_PARTY_MENTIONS", "position": "body"},
        {"url": "https://b.example/y", "source_class": "THIRD_PARTY_MENTIONS", "position": "title"},
        {"url": "https://c.example/z", "source_class": "OTHER", "position": None},
    ]}
    rows = citation_rows(run, attribution)
    assert [r.position for r in rows] == [1, 2, 3]
    assert [r.kind for r in rows] == ["THIRD_PARTY_MENTIONS", "THIRD_PARTY_MENTIONS", "OTHER"]
