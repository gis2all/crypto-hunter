"""Kept cards: the shelf is a copy, and it outlives what it was copied from.

Nothing on the shelf points at a round. A report row is deleted with its round, and a whole
report library can be dropped, so a card that was kept has to still read after both - which
is the difference between a shelf and a bookmark.

A copy, but not a snapshot: the shelf follows the token, so a round that writes that token
again replaces the card in place (`refresh_kept` calls `favorite_refresh`).
"""

import json

from app import config
from app.pipeline import run as pipeline_run
from app.report import assemble as report_assemble
from app.store import db

ARC = "arc"
CA = "0xabc0000000000000000000000000000000000001"
NOW = "2026-09-30T16:19:00+08:00"
LATER = "2026-10-05T09:00:00+08:00"


def _seed_round(conn, run_id, ca=CA, symbol="ABC", score=71.2, chain_id=ARC, at=NOW,
                config="default", status="researched"):
    """One round with one written card, at the shape `assemble.cards` reads back."""
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, status, scanned, dropped,"
        " candidates, selected, config_id, scheduled_at, started_at)"
        " VALUES (?, ?, ?, 'manual', ?, 10, 6, 3, 1, ?, ?, ?)",
        (run_id, run_id, chain_id, status, config, at, at),
    )
    conn.execute(
        "INSERT INTO run_items (run_id, ca, decision, score, rank, breakdown_json)"
        " VALUES (?, ?, 'selected', ?, 1, ?)",
        (run_id, ca, score, json.dumps({"total": score, "onchain": 66.0, "product": 55.0})),
    )
    conn.execute(
        "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json, created_at)"
        " VALUES (?, ?, ?, '{}', '{}', ?)",
        (run_id, ca, json.dumps({"ca": ca, "symbol": symbol, "market_cap_usd": 120000.0,
                                 "liquidity_usd": 30000.0}), at),
    )
    conn.execute(
        "INSERT INTO reports (run_id, ca, rank, verdict, status, payload_json, created_at)"
        " VALUES (?, ?, 1, ?, 'written', ?, ?)",
        (run_id, ca, "一句话结论",
         json.dumps({"verdict": "一句话结论",
                     "sections": {"叙事与官方关系": "和本链有关"}}), at),
    )
    conn.execute(
        "INSERT INTO run_summary (run_id, payload_json, created_at) VALUES (?, ?, ?)",
        (run_id, json.dumps({"batch_label": "2026-09-30 16:19 +08",
                             "scoring": {"onchain_weight": 55, "offchain_weight": 45}}), at),
    )


def _seed(conn, run_id, **kw):
    _seed_round(conn, run_id, **kw)
    conn.commit()


def _keep(client, run_id, ca=CA):
    return client.post("/api/v1/favorites", json={"run_id": run_id, "ca": ca})


def _shelf(client):
    resp = client.get("/api/v1/favorites")
    assert resp.status_code == 200
    return resp.json()


def test_keeping_a_card_copies_it_and_says_where_it_came_from(isolated_db, client):
    """The shelf answers with the card itself, not a pointer to the round it came from."""
    conn = db.connect()
    try:
        _seed(conn, "run-1")
    finally:
        conn.close()

    resp = _keep(client, "run-1")
    assert resp.status_code == 201
    row = resp.json()
    assert row["chain_id"] == ARC
    assert row["ca"] == CA
    assert row["origin"] == "round"
    assert row["origin_run_id"] == "run-1"
    # The shelf's own shape: to the minute, and no offset (assemble.py `_kept_label`).
    assert row["origin_label"] == "2026-09-30 16:19"
    assert row["config_id"] == "default"
    assert row["card"]["sym"] == "ABC"
    assert [s["text"] for s in row["card"]["verdict"]] == ["一句话结论"]
    assert row["score"] == 71.2

    shelf = _shelf(client)
    assert [r["ca"] for r in shelf] == [CA]
    assert shelf[0]["card"]["sym"] == "ABC"


def test_the_card_outlives_the_report_library_it_came_from(isolated_db, client):
    """Drop the whole library and the kept card still reads: 删配置 does not take the shelf."""
    conn = db.connect()
    try:
        _seed(conn, "run-1")
    finally:
        conn.close()
    assert _keep(client, "run-1").status_code == 201

    config.forget_library(ARC, "default")

    conn = db.connect()
    try:
        left = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    finally:
        conn.close()
    assert left == 0, "the library really was dropped"
    shelf = _shelf(client)
    assert [r["card"]["sym"] for r in shelf] == ["ABC"]
    assert shelf[0]["origin_run_id"] == "run-1"


def test_keeping_the_same_token_again_replaces_the_card(isolated_db, client):
    """One CA, one shelf row. A later round replaces the payload instead of stacking up."""
    conn = db.connect()
    try:
        _seed(conn, "run-1", score=71.2)
        _seed(conn, "run-2", score=88.4)
    finally:
        conn.close()

    assert _keep(client, "run-1").status_code == 201
    again = _keep(client, "run-2")
    assert again.status_code == 201
    assert again.json()["origin_run_id"] == "run-2"

    shelf = _shelf(client)
    assert len(shelf) == 1, "the same token is one row, not two"
    assert shelf[0]["score"] == 88.4
    assert shelf[0]["card"]["score"] == 88.4
    assert shelf[0]["origin_run_id"] == "run-2"


def test_taking_a_card_off_the_shelf(isolated_db, client):
    """取消收藏 is one call, and a second one is not an error."""
    conn = db.connect()
    try:
        _seed(conn, "run-1")
    finally:
        conn.close()
    assert _keep(client, "run-1").status_code == 201

    first = client.delete("/api/v1/favorites/%s/%s" % (ARC, CA))
    assert first.status_code == 200
    assert first.json()["removed"] is True
    assert _shelf(client) == []
    assert client.delete("/api/v1/favorites/%s/%s" % (ARC, CA)).json()["removed"] is False


def test_a_chain_with_no_report_library_can_still_be_kept(isolated_db, client):
    """The shelf is keyed by chain, not by the chains this app screens: a card from anywhere
    keeps its own chain id and is listed beside the rest."""
    conn = db.connect()
    try:
        _seed(conn, "run-1")
        _seed(conn, "run-sol", ca="SoLanaCa", symbol="SOLX", chain_id="solana")
    finally:
        conn.close()

    assert _keep(client, "run-1").status_code == 201
    assert _keep(client, "run-sol", ca="SoLanaCa").status_code == 201

    shelf = _shelf(client)
    assert {r["chain_id"] for r in shelf} == {ARC, "solana"}
    assert shelf[0]["card"]["sym"] in {"ABC", "SOLX"}


def test_a_card_that_was_never_written_cannot_be_kept(isolated_db, client):
    """404 says which of the two things is missing: the round, or the card in it."""
    conn = db.connect()
    try:
        _seed(conn, "run-1")
    finally:
        conn.close()

    missing_card = _keep(client, "run-1", ca="0xdead")
    assert missing_card.status_code == 404
    assert "报告卡" in missing_card.json()["detail"]
    assert client.post("/api/v1/favorites",
                       json={"run_id": "nope", "ca": CA}).status_code == 404
    assert client.get("/api/v1/favorites/%s/%s" % (ARC, CA)).status_code == 404


def test_a_round_writes_its_newer_card_onto_the_shelf(isolated_db, client):
    """A kept card stands for the token, so the round that writes it again is the one the
    shelf ends up showing - without the reader having to come back and ask."""
    conn = db.connect()
    try:
        _seed(conn, "run-1", score=71.2)
        _seed(conn, "run-2", score=88.4, at=LATER, status="selected")
    finally:
        conn.close()
    assert _keep(client, "run-1").status_code == 201
    # A stamp from before the newer round, so "it did not move" is readable rather than a
    # comparison against a clock that ticks in the same second.
    conn = db.connect()
    try:
        conn.execute("UPDATE favorites SET saved_at = '2026-09-30T19:21:45+08:00'")
    finally:
        conn.close()

    # What a round does at the end of its own run: the round ends, not the reader's day.
    pipeline_run._finish("run-2")

    shelf = _shelf(client)
    assert len(shelf) == 1, "one CA is one row, replaced rather than stacked"
    row = shelf[0]
    assert row["origin_run_id"] == "run-2"
    assert row["origin_label"] == "2026-10-05 09:00"
    assert row["card"]["score"] == 88.4
    assert row["score"] == 88.4
    assert row["saved_at"] == "2026-09-30T19:21:45+08:00", "when it was kept, not when it moved"
    assert client.get("/api/v1/favorites/%s/%s" % (ARC, CA)).json()["origin_run_id"] == "run-2"


def test_a_round_touches_only_its_own_chain_and_its_own_settings(isolated_db, client):
    """Another chain's round, another set of settings, and a round older than the kept card
    all write a different card rather than a newer one."""
    conn = db.connect()
    try:
        _seed(conn, "run-1", score=71.2)
        _seed(conn, "run-older", score=64.0, at="2026-09-01T09:00:00+08:00")
        _seed(conn, "run-other-chain", score=90.0, chain_id="solana", at=LATER)
        _seed(conn, "run-other-config", score=93.0, at=LATER, config="cfg2")
    finally:
        conn.close()
    assert _keep(client, "run-1").status_code == 201

    for run_id in ("run-older", "run-other-chain", "run-other-config"):
        report_assemble.refresh_kept(run_id)

    row = _shelf(client)[0]
    assert row["origin_run_id"] == "run-1"
    assert row["card"]["score"] == 71.2


def test_a_round_adds_nothing_to_the_shelf(isolated_db, client):
    """A card nobody kept belongs to the round. Writing it onto the shelf would be the round
    deciding what the reader is following."""
    conn = db.connect()
    try:
        _seed(conn, "run-1", ca="0xkept", symbol="KEPT")
        _seed(conn, "run-2", ca=CA, symbol="OTHER", at=LATER)
    finally:
        conn.close()
    assert _keep(client, "run-1", ca="0xkept").status_code == 201

    assert report_assemble.refresh_kept("run-2") == 0
    assert [r["ca"] for r in _shelf(client)] == ["0xkept"]


def test_a_token_this_round_did_not_write_keeps_its_card(isolated_db, client):
    """Falling out of the report is not the same as getting a worse card: a round that never
    wrote this token leaves the shelf alone."""
    conn = db.connect()
    try:
        _seed(conn, "run-1", ca=CA, symbol="ABC")
        _seed(conn, "run-2", ca="0xother", symbol="OTHER", at=LATER)
    finally:
        conn.close()
    assert _keep(client, "run-1").status_code == 201

    report_assemble.refresh_kept("run-2")

    assert _shelf(client)[0]["origin_run_id"] == "run-1"
