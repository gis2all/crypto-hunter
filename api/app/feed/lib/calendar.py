"""北京日历的单一出处（AIHOT 的 `contracts/time.ts`）。

一份期刊的“今天”是北京的今天，不是 UTC 的。这个事实一旦在两个地方各自算一遍，
就会出现“日刊里收了一条晚上 8 点后的东西，而页面上那一天的分组里没有它”——
两边的切刀位置差了一遍。所以日期与周标签只在这里算。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: 北京时区。国内不用夏令时，固定 +8 就是它的全部规则（与 AIHOT 一致）。
BEIJING = timezone(timedelta(hours=8))

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def moment_of(when) -> datetime:
    """把任意时间形式归成带时区的 `datetime`。

    一个不带时区的字符串被当作 UTC 读（与 `materials.moment` 同一条规则）；
    而一个不带时区的 `datetime` 对象则不能猜 —— 它本身就没说。
    """
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    text = str(when or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def beijing(moment: datetime) -> datetime:
    return moment.astimezone(BEIJING)


def beijing_date(when) -> str:
    """`2026-10-05`。"""
    return beijing(moment_of(when)).strftime("%Y-%m-%d")


def beijing_time(when) -> str:
    """`21:07`。"""
    return beijing(moment_of(when)).strftime("%H:%M")


def beijing_stamp(when) -> str:
    """`2026-10-05 21:07`。"""
    return "%s %s" % (beijing_date(when), beijing_time(when))


def beijing_midnight(when) -> datetime:
    """这一天的北京 00:00，以北京时区表示。"""
    day = beijing(moment_of(when))
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def add_days(when, days: int) -> datetime:
    return moment_of(when) + timedelta(days=days)


def beijing_weekday(when) -> str:
    """周一到周日的中文单字。"""
    return WEEKDAYS[beijing(moment_of(when)).weekday()]


def iso_week_label(when) -> str:
    """`2026-W40`。"""
    year, week, _ = beijing(moment_of(when)).isocalendar()
    return "%04d-W%02d" % (year, week)


def iso_week_range(when) -> tuple[datetime, datetime]:
    """这一周的起止（周一 00:00 到下周一 00:00，北京）。"""
    day = beijing_midnight(when)
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=7)


def month_key(when) -> str:
    """`2026-10`。"""
    return beijing(moment_of(when)).strftime("%Y-%m")


def month_range(when) -> tuple[datetime, datetime]:
    """这一月的起止（北京）。"""
    day = beijing_midnight(when)
    start = day.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def day_range(when) -> tuple[datetime, datetime]:
    """这一天的起止（北京）。"""
    start = beijing_midnight(when)
    return start, start + timedelta(days=1)


def period_key(kind: str, when) -> str:
    """一期的名字: day → `2026-10-05`, week → `2026-W40`, month → `2026-10`。"""
    if kind == "week":
        return iso_week_label(when)
    if kind == "month":
        return month_key(when)
    return beijing_date(when)


def period_range(kind: str, when) -> tuple[datetime, datetime]:
    """一期的窗口。认不出的 kind 当 day（调用方自己先验）。"""
    if kind == "week":
        return iso_week_range(when)
    if kind == "month":
        return month_range(when)
    return day_range(when)


def day_key(when) -> str:
    return beijing_date(when)


def parse_day(text: str) -> date:
    """`2026-10-05` → `date`。不合法就抛 `ValueError`。"""
    return date.fromisoformat(text.strip())
