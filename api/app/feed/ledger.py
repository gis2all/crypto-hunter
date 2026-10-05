"""精选台账: 哪一条什么时候上过首页 (AIHOT 的 `selected_ledger`)。

AIHOT 在发布事务里给精选集合记一笔 `upsert` / `remove`，读侧据此知道"这条是什么时候
进首页的、又是什么时候下来的"。我们这边精选是每次请求重算的派生结果，没有发布事务
可挂 —— 所以账挂在**定时的一趟**上 (`jobs` 的 `selected-ledger`)，做法一样：拿现在的集合与上一
次的账对一遍，进的记 `upsert`、出的记 `remove`。

`visible_at` 与 `changed_at` 同刻：我们没有跳机的可见水位，一条进了集合就是当刻可见。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..store import db
from . import views

#: 对账重算多长窗口。与实时流一致 —— 精选本来就只在那个窗口里有意义。
WINDOW_HOURS = views.STREAM_HOURS


def item_key(item) -> str:
    """一条在台账上的稳定名字: 与跳转用的 id 同一个拼法."""
    return "%s:%s" % (item.discovery.source_id, item.discovery.external_id)


def sync(now: datetime | None = None) -> dict:
    """把现在的精选集合与上一次的账对一遍, 进的出的各记一笔。"""
    moment = now or datetime.now(timezone.utc)
    items, _ = views.collect(moment, WINDOW_HOURS)
    live = {item_key(item): item for item in items if item.selected}
    state = db.feed_selected_state_all()
    at = moment.isoformat(timespec="seconds")
    entered = left = 0
    for key in sorted(live):
        if not state.get(key):
            run_id = db.feed_selected_ledger_add(key, "upsert", at, at)
            db.feed_selected_state_put(key, 1, run_id)
            entered += 1
    for key, in_set in sorted(state.items()):
        if in_set and key not in live:
            run_id = db.feed_selected_ledger_add(key, "remove", at, at)
            db.feed_selected_state_put(key, 0, run_id)
            left += 1
    return {"entered": entered, "left": left, "in_set": len(live)}
