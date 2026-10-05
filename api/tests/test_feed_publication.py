"""发布门与正文档位：一条从被读到到公开的那一段，和"印全文还是摘要"。

两件事都由 `feed/rules.py` + `feed/scope.py` 说，所有出口（页面 / RSS / sitemap / v1）
调同一份判断。这里测的是那条承诺本身 —— 其余每一条测试都在门开着的状态下跑
（`conftest.no_release_gate`），否则"写完立刻读"读到的永远是空页。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feed import rules, scope, views
from app.feed.sources import registry
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


def _row(external_id: str = "a-1", **extra) -> dict:
    row = {"source_id": "rss:a", "external_id": external_id, "source_kind": "rss",
           "identity_key": "other|", "title": "Nova bridge drained",
           "url": "https://a/%s" % external_id,
           "published_at": (NOW - timedelta(hours=1)).isoformat()}
    row.update(extra)
    return row


def test_a_fresh_row_is_hidden_until_the_gate_passes(isolated_db, monkeypatch):
    monkeypatch.setattr(rules, "RELEASE_DELAY_SECONDS", 180)
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put([_row()])

    # 门内：写完了，但还没到点。
    assert [item.discovery.external_id for item in views.collect(NOW, 72.0)[0]] == []
    # 门外：三分钟一过就公开。
    later = NOW + timedelta(minutes=4)
    assert [item.discovery.external_id for item in views.collect(later, 72.0)[0]] == ["a-1"]


def test_a_withdrawn_row_never_reads_back(isolated_db, monkeypatch):
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put([_row()])
    assert db.feed_item_withdraw("rss:a", "a-1")
    assert [item.discovery.external_id for item in views.collect(NOW, 72.0)[0]] == []
    # 下架不是删除：行还在，只是不再公开。
    row = db.feed_item_get("rss:a", "a-1")
    assert row and row["withdrawn_at"]
    assert not scope.listed(row, NOW)


def test_body_mode_is_summary_unless_the_roster_allows_fulltext():
    plain = registry.of("rss:a")          # 清单里没写 site_fulltext
    assert rules.body_mode_of(plain, "ok", True) == "summary"

    class Allowed:
        site_fulltext = True
        syndicate_fulltext = True

    allowed = Allowed()
    assert rules.body_mode_of(allowed, "ok", True) == "full"
    # 许可了但这一版没读到正文：还是摘要 —— "能印全文"不等于"这一版有全文"。
    assert rules.body_mode_of(allowed, "missing", False) == "summary"
    assert rules.may_redistribute(allowed, "full") is True
    assert rules.may_redistribute(allowed, "summary") is False
    assert rules.may_redistribute(plain, "full") is False


def test_the_channel_key_splits_news_from_x():
    assert rules.channel_of("rss", False) == "news"
    assert rules.channel_of("x", False) == "x"
    assert rules.channel_of("rss", True) == "x"      # 带推文的源走 x 那一路
    assert set(rules.CHANNELS) == {"all", "news", "x", "firstParty"}


def test_the_release_gate_moves_a_stamp_by_the_configured_delay(monkeypatch):
    monkeypatch.setattr(rules, "RELEASE_DELAY_SECONDS", 180)
    stamp = rules.release_at(NOW.isoformat(timespec="seconds"))
    assert datetime.fromisoformat(stamp) == NOW + timedelta(seconds=180)
    assert rules.release_deadline(NOW) == (NOW - timedelta(seconds=180)).isoformat(timespec="seconds")
