"""Macro track: rules are pinned, the slot is idempotent, the API reads."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.macro import config as macro_config
from app.macro import rules, service
from app.macro import fetch as macro_fetch
from app.pipeline import schedule
from app.store import db


def _stub_items():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        {"url": "https://example.com/a",
         "title": "Bitcoin ETF inflows hit record as BTC breaks highs",
         "summary": "Spot bitcoin ETFs drew record inflows as bitcoin broke highs.",
         "published_at": now},
        {"url": "https://example.com/b",
         "title": "Record BTC ETF inflows as bitcoin breaks highs",
         "summary": "A second outlet on the same bitcoin ETF story.",
         "published_at": now},
        {"url": "https://example.com/a",
         "title": "Bitcoin ETF inflows hit record as BTC breaks highs",
         "summary": "The same URL crawled twice.",
         "published_at": now},
        {"url": "https://example.com/c",
         "title": "Sunny weather expected across the valley this weekend",
         "summary": "A local forecast with no markets in it.",
         "published_at": now},
    ]


@pytest.fixture()
def stubbed(monkeypatch):
    monkeypatch.setattr(macro_fetch, "fetch", lambda src, timeout=20.0: _stub_items())

    def fake_json(messages, **kw):
        return {"score": 4.0, "title_zh": "test", "summary_zh": "test",
                "reason": "test", "project": "Bitcoin",
                "sector": "CEX", "morph": "data"}

    monkeypatch.setattr(service.llm_client, "complete_json", fake_json)
    monkeypatch.setattr(service.llm_client, "configured", lambda: True)
    return True


def test_macro_config_loads_sources(isolated_db):
    cfg = macro_config.load_macro()
    assert cfg["cadence"]["collect_minutes"] == 60
    assert macro_config.sync_sources(cfg) == 5
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM macro_sources").fetchone()["n"]
    finally:
        conn.close()
    assert n == 5


def test_fingerprint_prefers_url():
    same = rules.fingerprint("https://example.com/a?x=1", "Bitcoin story")
    again = rules.fingerprint("https://example.com/a?y=2", "Bitcoin story retold")
    other = rules.fingerprint("https://example.com/b", "Bitcoin story")
    assert same == again
    assert same != other
    assert rules.fingerprint(None, "Bitcoin story") == rules.fingerprint("", "Bitcoin story")


def test_prescreen_keeps_crypto_drops_noise():
    ok, _ = rules.prescreen("Bitcoin ETF inflows hit record", "spot bitcoin etfs",
                            max_age_days=14)
    assert ok
    ok, reason = rules.prescreen("Sunny weather expected across the valley", "forecast",
                                 max_age_days=14)
    assert (not ok) and reason == "off_topic"
    ok, reason = rules.prescreen("hi", "bitcoin", max_age_days=14)
    assert (not ok) and reason == "too_short"


def test_combine_scores_veto():
    _, picked = rules.combine_scores(4.0, 4.0, threshold=3.5, min_single=2.5)
    assert picked
    _, picked = rules.combine_scores(5.0, 1.0, threshold=3.5, min_single=2.5)
    assert not picked
    _, picked = rules.combine_scores(3.0, 3.0, threshold=3.5, min_single=2.5)
    assert not picked


def test_heat_decay():
    assert rules.heat([0.0, 24.0], half_life_hours=24.0) == pytest.approx(1.5)
    assert rules.heat([], half_life_hours=24.0) == 0.0


def test_should_merge_groups_one_story():
    assert rules.should_merge("Bitcoin ETF inflows hit record as BTC breaks highs",
                               "Record BTC ETF inflows as bitcoin breaks highs - CoinDesk")
    assert not rules.should_merge("Bitcoin ETF inflows hit record as BTC breaks highs",
                                   "Sunny weather expected across the valley this weekend")


def test_collect_dedups(isolated_db, stubbed):
    first = service.collect_once()
    assert first["new"] == 3
    second = service.collect_once()
    assert second["new"] == 0


def test_score_selects_and_leaves_noise(isolated_db, stubbed):
    service.collect_once()
    out = service.score_pending()
    assert out["selected"] == 2
    conn = db.connect()
    try:
        rows = conn.execute("SELECT title, selected, reason FROM macro_items").fetchall()
    finally:
        conn.close()
    by_title = {row["title"]: (row["selected"], row["reason"]) for row in rows}
    assert by_title["Sunny weather expected across the valley this weekend"] == (0, "off_topic")


def test_cluster_groups_one_story(isolated_db, stubbed):
    service.collect_once()
    service.score_pending()
    first = service.cluster_unassigned()
    assert first["created"] == 1
    second = service.cluster_unassigned()
    assert second == {"attached": 0, "created": 0}
    conn = db.connect()
    try:
        events = conn.execute("SELECT * FROM macro_events").fetchall()
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0]["heat"] > 0
    assert events[0]["items_count"] == 2


def test_slot_is_idempotent(isolated_db, stubbed):
    first = service.run_slot("2026010108")
    second = service.run_slot("2026010108")
    assert first["slot"] == "2026010108"
    assert second["selected"] == first["selected"]
    assert service.slot_done("2026010108")
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM macro_runs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_macro_tick_queues_once(isolated_db, stubbed, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    moment = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
    assert schedule.macro_tick(moment) == "2026010209"
    assert schedule.macro_tick(moment) is None


def test_digest_assembles_without_llm(isolated_db, stubbed, monkeypatch):
    service.collect_once()
    service.score_pending()
    def boom(messages, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(service.llm_client, "complete", boom)
    payload = service.build_digest(day=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    assert payload["intro"] == ""
    assert payload["sections"]
    assert payload["events"] == []


def test_macro_api_end_to_end(client, stubbed):
    resp = client.post("/api/v1/macro/collect")
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True
    items = client.get("/api/v1/macro/items").json()
    assert len(items) == 3
    picked = client.get("/api/v1/macro/items", params={"selected_only": True}).json()
    assert len(picked) == 2
    events = client.get("/api/v1/macro/events").json()
    assert len(events) == 1
    event_id = events[0]["id"]
    detail = client.get("/api/v1/macro/events/%s" % event_id).json()
    assert len(detail["items"]) == 2
    topics = client.get("/api/v1/macro/topics").json()
    assert any(topic["kind"] == "sector" for topic in topics)
    found = client.get("/api/v1/macro/search", params={"q": "bitcoin"}).json()
    assert len(found) == 2
    kept = client.post("/api/v1/macro/favorites", json={"event_id": event_id})
    assert kept.status_code == 201
    shelf = client.get("/api/v1/macro/favorites").json()
    assert len(shelf) == 1
    gone = client.delete("/api/v1/macro/favorites/%s" % event_id).json()
    assert gone == {"removed": True}