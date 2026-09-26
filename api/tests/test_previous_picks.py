"""What the last round left behind: the picks a delta is computed against."""

import json

from app.store import db

CA = "0x0bb3befba323578dad33efb6356c69b557787777"


def _seed(conn, *, run_id: str, run_key: str, symbol_in_snapshot, roster_symbol):
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, status, selected, started_at)"
        " VALUES (?, ?, 'arc', 'manual', 'sent', 1, '2026-09-24T20:00:00+08:00')",
        (run_id, run_key),
    )
    conn.execute(
        "INSERT INTO reports (run_id, ca, rank, status, payload_json, created_at)"
        " VALUES (?, ?, 1, 'sent', '{}', '2026-09-24T20:05:00+08:00')",
        (run_id, CA),
    )
    metrics = {"ca": CA, "market_cap_usd": 120_000.0}
    if symbol_in_snapshot is not None:
        metrics["symbol"] = symbol_in_snapshot
    conn.execute(
        "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json, created_at)"
        " VALUES (?, ?, ?, '{}', '{}', '2026-09-24T20:05:00+08:00')",
        (run_id, CA, json.dumps(metrics)),
    )
    if roster_symbol is not None:
        conn.execute(
            "INSERT INTO tokens (ca, chain_id, symbol, created_at, updated_at)"
            " VALUES (?, 'arc', ?, '2026-09-01T00:00:00+08:00', '2026-09-01T00:00:00+08:00')",
            (CA, roster_symbol),
        )
    conn.commit()


def _pick(isolated_db, **seed):
    from app.pipeline import run as pipeline_run

    conn = db.connect()
    try:
        _seed(conn, run_id="last", run_key="last", **seed)
    finally:
        conn.close()
    picks = pipeline_run.previous_picks("arc", "this-one")
    assert len(picks) == 1
    return picks[0]


def test_the_symbol_comes_from_the_snapshot_that_saw_it(isolated_db):
    assert _pick(isolated_db, symbol_in_snapshot="MEMESPAD", roster_symbol=None)["symbol"] == \
        "MEMESPAD"


def test_a_snapshot_without_a_name_falls_back_to_the_roster(isolated_db):
    """A round that never learned the name leaves the snapshot without one, and 退出 is
    exactly the row about a token the report no longer carries - the roster is the only
    place left that knows what it was called."""
    assert _pick(isolated_db, symbol_in_snapshot=None, roster_symbol="MEMESPAD")["symbol"] == \
        "MEMESPAD"


def test_no_name_anywhere_is_still_no_name(isolated_db):
    """Nothing to show is not a reason to invent one: the caller decides what an unnamed
    pick looks like."""
    assert _pick(isolated_db, symbol_in_snapshot=None, roster_symbol=None)["symbol"] is None


def test_the_numbers_the_delta_needs_ride_along(isolated_db):
    pick = _pick(isolated_db, symbol_in_snapshot="MEMESPAD", roster_symbol=None)
    assert pick["ca"] == CA.lower()
    assert pick["market_cap_usd"] == 120_000.0

