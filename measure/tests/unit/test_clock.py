"""页面与结果文件上的时刻(report/clock.py,用户 2026-10-29 定):电脑当地时间 + 时区标记;库里的时间不动。
零网络零数据库。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

import report.clock
from report.clock import local_date, local_time, zone_label

MOMENT = datetime(2026, 10, 14, 3, 4, tzinfo=UTC)


@pytest.mark.parametrize("offset, label", [
    (timedelta(hours=8), "UTC+8"), (timedelta(hours=-5), "UTC-5"), (timedelta(hours=5, minutes=30), "UTC+5:30"),
    (timedelta(hours=-3, minutes=-30), "UTC-3:30"), (timedelta(0), "UTC"),
])
def test_zone_label_names_the_offset(offset, label):
    assert zone_label(MOMENT.astimezone(timezone(offset))) == label


def test_times_are_shown_in_the_local_zone_with_its_label(monkeypatch):
    assert local_time(MOMENT) == "2026-10-14 11:04 UTC+8"  # 测试统一当 UTC+8(tests/unit/conftest.py)
    monkeypatch.setattr(report.clock, "local_zone", lambda: timezone(timedelta(hours=-7)))
    assert local_time(MOMENT) == "2026-10-13 20:04 UTC-7"
    assert local_date(MOMENT) == "2026-10-13"  # 只要日期的地方也按当地日期(跨日时不会差一天)


def test_the_real_computer_zone_is_used_when_not_pinned(monkeypatch):
    """不钉时区时按这台电脑的时区:和 Python 自己换算的本机时间一致。"""
    monkeypatch.setattr(report.clock, "local_zone", lambda: None)
    local = MOMENT.astimezone()
    assert local_time(MOMENT) == f"{local:%Y-%m-%d %H:%M} {zone_label(local)}"
