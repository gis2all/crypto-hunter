"""可见性的读侧（AIHOT 的 `publication/scope.ts`）。

`rules.py` 说“什么样的东西能印”，这一层说“这一行现在能不能读”。
分开是因为两个问题的变量不同：前者只看材料的属性，后者还要看时间。
"""

from __future__ import annotations

from datetime import datetime

from .lib import calendar as cal

#: 一条贴上了“不给看”的标记。它不是删除 —— 已经转发出去的链接要能解释为什么没了。
def is_withdrawn(row: dict) -> bool:
    return bool((row.get("withdrawn_at") or "").strip())


def released_at(row: dict) -> str:
    """这一行什么时候过的门；空表示这一行比这个列还早（老行不抦）。"""
    return (row.get("released_at") or "").strip()


def is_released(row: dict, now: datetime) -> bool:
    """过了发布门没有。空的那一列当“老行”处理：不能因为一次升级把整库藏起来。

    还有一条：读者的钟比这一行的发现时刻**早**时，这一行一定已经公开 —— 那是有人在读历史
    （档案、离线快照、测试里的固定时钟），而不是有人在浏览还没到点的东西。少了这一条，一个
    早于写入时刻的时钟会把整库读成空页。
    """
    stamp = released_at(row)
    if not stamp:
        return True
    seen = (row.get("seen_at") or "").strip()
    if seen:
        try:
            if cal.moment_of(seen) > now:
                return True
        except ValueError:
            pass
    return cal.moment_of(stamp) <= now


def listed(row: dict, now: datetime) -> bool:
    """这一行能不能被读到：没下架 + 过了门。每个出口都走它。"""
    return not is_withdrawn(row) and is_released(row, now)


def listed_sql(alias: str = "") -> str:
    """同一条规则的 SQL 写法，供需要在库里筛的那几条路用。

    两个 `?` 都是读者那一刻：第一个判“这一行比读者的钟还新”（读历史，放行），
    第二个判发布门（`rules.release_deadline`）。
    两处写法必须同步 —— 所以它们在同一个模块里，不是两个文件里。
    """
    prefix = "%s." % alias if alias else ""
    return ("%swithdrawn_at IS NULL AND (%sseen_at > ? OR %sreleased_at IS NULL"
            " OR %sreleased_at <= ?)" % (prefix, prefix, prefix, prefix))
