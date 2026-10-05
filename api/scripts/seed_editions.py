"""回填几期出刊, 好让日报 / 周报 / 月报三页都有"往期"可看.

用法:
    cd api && uv run python scripts/seed_editions.py         # 日报 21 期 + 周报 12 期 + 月报 8 期
    cd api && uv run python scripts/seed_editions.py 30      # 只改日报的期数

为什么是一条脚本而不是开机自动跑: 出刊是产品的动作, 不是启动的一部分. mock 的时间
分两种写法 (DESIGN.md 6.4 / 6.5): 大多数是相对写法, 所以把 `now` 挪到过去, 读出来的就是"那一期
的那一批" —— 回填不用另造数据, 代价是相对那批在每一期里长得一样; 另有 36 组带日期的归档条目,
每组钉在某一天的锚点前一两个小时, 所以每一期的头条仍然是它自己那天的 —— 往期不会是一份报
纸印 41 遍.

从最早的往回填, 不是从今天往前填: 期号是"同一种刊里第几期" (`service.run_once` 拿它当
`db.feed_issue_number` 的答案 +1), 先写新的会让旧的那些各自数到自己为止, 于是每一期都是第
一期. 顺序在这里是正确性的一部分, 不是排版.

写进的就是 `POST /feed/run` 写的那张表、那个形状, 所以回填出来的几期和真出刊的几期没有区别.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.feed import service  # noqa: E402

#: 每一种刊回填多少期. 日报多两期是因为左栏按月分组, 一期太少的月份看不出分组.
BACKFILL = (("day", 21), ("week", 12), ("month", 8))


def _at(now: datetime, kind: str, back: int) -> datetime:
    """第 back 期之前的那个时刻: 落在那一期的窗口里, 且离窗口尾端不远.

    20:00 +08 对三种刊都对: 在当天的尾部、在那一周的尾部、在那一月的开头几天里. 时刻本身不
    是"出刊时间"(现在还没有调度), 它只是把 mock 的相对时间挪到那一段里去.
    """
    moment = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if kind == "day":
        return moment - timedelta(days=back)
    if kind == "week":
        return moment - timedelta(weeks=back)
    year, month = moment.year, moment.month - back
    while month < 1:
        month += 12
        year -= 1
    return moment.replace(year=year, month=month, day=1)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else BACKFILL[0][1]
    now = datetime.now(timezone.utc)
    for kind, count in BACKFILL:
        if kind == "day":
            count = days
        # 最早的在前, 期号才对 (见模块开头).
        for back in reversed(range(count)):
            moment = _at(now, kind, back)
            row = service.run_once(now=moment, kind=kind)
            print("%-26s %-16s #%s entries=%s seen=%s"
                  % (row["id"], row["key"], row["issue_number"], row["entries"], row["seen"]))


if __name__ == "__main__":
    main()
