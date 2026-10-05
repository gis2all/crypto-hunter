"""热度快照与榜单快照: 写下来的那一份和页面读回去的那一份, 必须是同一个数.

AIHOT 的 `story_heat_hourly` / `hot_rankings` (events/hot.ts). 两条最要紧的:
每一小时只落一行、落后源的那一格先记成不完整, 等源追上来再补一次.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feed import heat, snapshot_heat, views
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


def _items():
    """一个两家在说的事件 —— 热度入榜要至少两家, 而且至少一家是发布源."""
    return [
        {"source_id": "rss:a", "external_id": "a-1", "source_kind": "rss",
         "identity_key": "nova|", "title": "Nova bridge drained", "url": "https://a/1",
         "published_at": (NOW - timedelta(hours=2)).isoformat()},
        {"source_id": "rss:b", "external_id": "b-1", "source_kind": "rss",
         "identity_key": "nova|", "title": "Nova bridge drained", "url": "https://b/1",
         "published_at": (NOW - timedelta(hours=1)).isoformat()},
    ]


def _hour(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def test_the_snapshot_writes_one_row_per_story_for_the_hour(isolated_db, monkeypatch):
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put(_items())
    _items_read, stories = views.collect(NOW, heat.WINDOW_HOURS)
    assert stories

    result = snapshot_heat.snapshot(NOW)

    assert result["stories"] == len(stories)
    rows = db.feed_heat_hourly([story.identity_key for story in stories], _hour(NOW), _hour(NOW))
    # 这个读取口只给"完整"的小时, 所以行数齐就等于每一格都记成了完整.
    assert len(rows) == len(stories)
    assert all(row["heat"] > 0 for row in rows)


def test_the_stored_heat_is_the_number_the_card_prints(isolated_db, monkeypatch):
    """存的是页面那个单位 (AIHOT 的 `saveHeat` 也存 `heatIndex`): 图上的点和卡片上的数同源."""
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put(_items())
    snapshot_heat.snapshot(NOW)
    _items_read, stories = views.collect(NOW, heat.WINDOW_HOURS)
    stored = {row["story_id"]: row["heat"]
              for row in db.feed_heat_hourly([s.identity_key for s in stories], _hour(NOW), _hour(NOW))}
    for story in stories:
        assert stored[story.identity_key] == heat.index(story.heat)


def test_an_hour_with_a_behind_source_is_incomplete_and_is_repaired(isolated_db, monkeypatch):
    """落后源的那一格先记成不完整, 源追上来之后再补一次 (AIHOT `snapshotHeat` 的 `grace`)."""
    monkeypatch.setattr(db, "now_iso", lambda: (NOW - timedelta(hours=6)).isoformat(timespec="seconds"))
    db.feed_sources_put([{"id": "rss:a", "name": "A", "kind": "rss", "interval_minutes": 60}])
    db.feed_source_ok("rss:a", None, (NOW - timedelta(hours=5)).isoformat(timespec="seconds"))
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put(_items())

    older = NOW - timedelta(hours=1)
    snapshot_heat.snapshot(older)
    _items_read, stories = views.collect(older, heat.WINDOW_HOURS)
    keys = [story.identity_key for story in stories]
    assert keys
    stale = db.feed_heat_hourly_stale(_hour(older), _hour(NOW))
    assert [entry["hour"] for entry in stale] == [_hour(older)]
    assert set(stale[0]["story_ids"]) == set(keys)
    assert not db.feed_heat_hourly(keys, _hour(older), _hour(older))

    # 这个源在那一小时之后成功抓过: 那一格该补完了.
    db.feed_source_ok("rss:a", None, (NOW + timedelta(hours=1)).isoformat(timespec="seconds"))
    result = snapshot_heat.snapshot(NOW)

    assert result["repaired"] >= 1
    assert not db.feed_heat_hourly_stale(_hour(older), _hour(NOW))
    assert len(db.feed_heat_hourly(keys, _hour(older), _hour(older))) == len(keys)


def test_the_ranking_is_written_and_read_back(isolated_db, monkeypatch):
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put(_items())

    written = snapshot_heat.rank(NOW, limit=10)
    payload = snapshot_heat.hot_payload(NOW)

    assert written["entries"] >= 1
    assert payload["generated_at"] == NOW.isoformat(timespec="seconds")
    assert len(payload["stories"]) == written["entries"]
    assert db.feed_hot_ranking_latest()["id"] == written["id"]


def test_a_ranking_past_its_ttl_is_not_served(isolated_db, monkeypatch):
    """5 分钟之外的那一份不算数 (AIHOT 的 `hot.rank` 每 5 分钟一趟)."""
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put(_items())
    snapshot_heat.rank(NOW, limit=10)
    assert snapshot_heat.latest(NOW) is not None
    assert snapshot_heat.latest(NOW + timedelta(minutes=6)) is None


def test_a_ranking_from_another_rule_version_is_ignored(isolated_db, monkeypatch):
    """规则改了, 旧榜上的名次就不再是"今天的结果". 版本对不上就不读它."""
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_hot_ranking_new({"computed_at": NOW.isoformat(timespec="seconds"),
                             "rule_version": "heat-v0-old", "entries_json": "[]"})
    assert snapshot_heat.latest(NOW) is None


def test_the_sparkline_is_one_slot_per_hour_with_gaps(isolated_db):
    """25 个格子, 缺的小时留 `null` —— 补一个 0 会把"那小时没数据"画成"那小时很冷"."""
    key = "s:1"
    hour = NOW.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    db.feed_heat_snapshot_set(_hour(hour - timedelta(hours=3)),
                              [{"story_id": key, "heat": 7.0, "participants": 2,
                                "cohort": 2, "complete": True}])

    line = snapshot_heat.sparkline([key], NOW)[key]

    assert len(line) == heat.SPARK_HOURS + 1
    assert line[heat.SPARK_HOURS - 3] == 7.0
    assert line.count(None) == len(line) - 1
