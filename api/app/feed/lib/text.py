"""小写字的一处口径（AIHOT 的 `lib/text.ts` + `apps/web/app/lib/format.ts`）。

页面上那几个说法——“刚刚”、“N 分钟前”、“今天”、去掉源名后面的
“（RSS）”——全部在这里，而不是各页各自写一遍：同一个时刻在列表里
是“3 小时前”、在详情里是“3 小时前”，才不会让人以为是两条新闻。
"""

from __future__ import annotations

import re

from .calendar import beijing_date, beijing_stamp, beijing_time, moment_of

_WS = re.compile(r"\s+")
#: 源名后面那个括号里的转述渠道（AIHOT 的 `shortSourceName`）。
_CHANNEL_SUFFIX = re.compile(r"（(RSS|网页|API)）")
_X_NAME = re.compile(r"^X[:：]\s*(.+?)\s*\(@[^)]+\)\s*$")
_PAREN = re.compile(r"（.*?）")


def collapse(text: str | None) -> str:
    """把连续空白压成一个空格，两头去掉。"""
    return _WS.sub(" ", (text or "").strip())


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + ellipsis


def short_source_name(name: str) -> str:
    """`X：Ethan Mollick (@emollick)` → `Ethan Mollick`；其他源去掉渠道后缀。"""
    found = _X_NAME.match((name or "").strip())
    if found:
        return _PAREN.sub("", found.group(1)).strip()
    return _CHANNEL_SUFFIX.sub("", (name or "").strip()).strip()


def source_initial(name: str) -> str:
    """头像里那一个字（AIHOT 的 `sourceInitial`）。"""
    stripped = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", short_source_name(name))
    return (stripped[:1] or "A").upper()


def relative_time(when, now) -> str:
    """刚刚 / N 分钟前 / N 小时前 / N 天前 / 北京日期。"""
    seconds = max(0, int((moment_of(now) - moment_of(when)).total_seconds()))
    if seconds < 60:
        return "刚刚"
    minutes = round(seconds / 60)
    if minutes < 60:
        return "%d 分钟前" % minutes
    hours = round(minutes / 60)
    if hours < 24:
        return "%d 小时前" % hours
    days = round(hours / 24)
    if days < 30:
        return "%d 天前" % days
    return beijing_date(when)


def day_label(day: str, today: str) -> str:
    """`2026-10-05` → `今天 · 10月5日`（AIHOT 的 `dayLabel`）。"""
    year, month, date = (int(part) for part in day.split("-"))
    base = "%d月%d日" % (month, date)
    if day == today:
        return "今天 · %s" % base
    from datetime import date as _date

    from .calendar import parse_day

    diff = (parse_day(today) - parse_day(day)).days
    if diff == 1:
        return "昨天 · %s" % base
    if year != int(today[:4]):
        return "%d年%s" % (year, base)
    return base


def full_datetime(when) -> str:
    return beijing_stamp(when)


def month_day_time(when) -> str:
    """`9月24日 10:51`（北京）。"""
    stamp = beijing_date(when)
    _, month, date = (int(part) for part in stamp.split("-"))
    return "%d月%d日 %s" % (month, date, beijing_time(when))
