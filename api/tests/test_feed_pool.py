"""列表的池子：分页游标、渠道筛选、搜索。

三件事共用一个窗口（`views.items_view`），所以谁也绕不开谁：游标读的是排序键，
渠道与搜索都是筛选 —— 先筛后排再切片，三处共用一条口径，页面的"翻到底"才不会
漏条或者重复。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feed import views
from app.feed.sources import registry
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


def _seed(count: int = 50) -> None:
    rows = []
    for index in range(count):
        rows.append({
            "source_id": "rss:a",
            "external_id": "a-%02d" % index,
            "source_kind": "rss",
            "identity_key": "other|",
            "title": "token-%02d 公告" % index,
            "url": "https://a/%02d" % index,
            # 从 1 分钟前开始: 与窗口上界同一刻的条目按定义不在窗口里 (read_between 半开区间).
            "published_at": (NOW - timedelta(minutes=index + 1)).isoformat(),
        })
    db.feed_items_put(rows)


def _ids(payload: dict) -> list[str]:
    return [row["id"] for day in payload["days"] for row in day["items"]]


def test_a_page_size_slices_the_window_and_the_cursor_walks_it(isolated_db):
    _seed(50)
    full = views.items_view(NOW, scope="all")
    assert full["shown"] == 50 and full["page_size"] == 0 and full["next_cursor"] == ""

    first = views.items_view(NOW, scope="all", page_size=40)
    assert first["shown"] == 40 and first["next_cursor"]
    assert first["filtered"] == 50

    second = views.items_view(NOW, scope="all", page_size=40, cursor=first["next_cursor"])
    assert second["shown"] == 10 and second["next_cursor"] == ""

    one, two = set(_ids(first)), set(_ids(second))
    assert not (one & two)                      # 不重复
    assert len(one | two) == 50                 # 不漏


def test_a_second_slice_does_not_lose_a_row_written_in_between(isolated_db):
    """游标不是 OFFSET：两页之间新来一条，第二页不会被挤掉一条（AIHOT 的 `lib/cursor.ts`）。"""
    _seed(45)
    first = views.items_view(NOW, scope="all", page_size=40)
    db.feed_items_put([{
        "source_id": "rss:a", "external_id": "a-new", "source_kind": "rss",
        "identity_key": "other|", "title": "新来的一条", "url": "https://a/new",
        "published_at": (NOW - timedelta(seconds=30)).isoformat(),
    }])
    second = views.items_view(NOW, scope="all", page_size=40, cursor=first["next_cursor"])
    assert second["shown"] == 5
    assert "rss:a:a-44" in _ids(second)
    # 新来那一条没有把第一页的最后一条挤到第二页 (OFFSET 会).
    assert "rss:a:a-39" not in _ids(second)
    assert "rss:a:a-new" not in _ids(second)


def test_the_channel_filter_counts_before_it_filters(isolated_db):
    # 名录里先有一行, 否则 `registry.of` 只回一个占位 (kind 按 rss 算), 这一条会被算成 news。
    db.feed_sources_put([{"id": "x:watcher", "name": "Chain Watcher", "kind": "x",
                          "tier": "T2", "interval_minutes": 60}])
    registry.forget()
    _seed(4)
    db.feed_items_put([{
        "source_id": "x:watcher", "external_id": "x-1", "source_kind": "x",
        "identity_key": "other|", "title": "$NOVA 有人在聊", "url": "https://x.test/1",
        "published_at": (NOW - timedelta(minutes=10)).isoformat(),
    }])
    everything = views.items_view(NOW, scope="all")
    counts = {row["key"]: row["count"] for row in everything["channels"]}
    assert counts["all"] == 5 and counts["news"] == 4 and counts["x"] == 1
    # 计数在筛选之前，所以点进 x 之后还能看见 news 有多少条。
    only_x = views.items_view(NOW, scope="all", channel="x")
    assert only_x["shown"] == 1
    assert {row["key"]: row["count"] for row in only_x["channels"]} == counts


def test_the_query_searches_title_and_source_name(isolated_db):
    _seed(3)
    hit = views.items_view(NOW, scope="all", query="token-01")
    assert [row["id"] for row in [r for d in hit["days"] for r in d["items"]]] == ["rss:a:a-01"]
    assert views.items_view(NOW, scope="all", query="没有这一条")["shown"] == 0
