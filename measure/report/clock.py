"""页面与结果文件上的时刻:一律显示这台电脑的当地时间,并带时区标记(用户 2026-10-29 定)。

本工具只在用户自己的电脑上跑(只认本机主机名),「当地」就是这台电脑的时区。库里存的、原始数据文件里的时间不动(本来就带时区)。
原来页面按 UTC 显示(S8.3),同一页里却有几处直接按本机时间打印、都没标时区,看的人分不清。
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo


def local_zone() -> tzinfo | None:
    """None = 这台电脑的时区(每个时刻按它当时的偏移换算,夏令时也对);测试里换成固定时区。"""
    return None


def zone_label(moment: datetime) -> str:
    """时区标记:UTC+8、UTC-5、UTC+5:30;零偏移写 UTC。"""
    minutes = int((moment.utcoffset() or timedelta(0)).total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'+' if minutes > 0 else '-'}{hours}" + (f":{rest:02d}" if rest else "")


def local_time(moment: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """当地时间 + 时区标记,如「2026-10-14 11:04 UTC+8」:报告转给别的时区的人看也不会看错。"""
    local = moment.astimezone(local_zone())
    return f"{local.strftime(fmt)} {zone_label(local)}"


def local_date(moment: datetime) -> str:
    """只要日期的地方(官网名单日期、校准日期):按当地日期,不带标记。"""
    return moment.astimezone(local_zone()).strftime("%Y-%m-%d")
