"""Stopping a round: 中止.

A stop arrives as an HTTP request on a request thread and has to reach a round that is
running on a worker thread. Nothing here is a rollback, because a round writes once, at the
end - so what a stop has to get right is that the round really ends where it stood: the rest
of the pull is not asked, no report is written, and the row says the operator stopped it.
"""

from __future__ import annotations

import time

import pytest

from app import cancel
from app.core.models import RunStatus, SafetyFacts, TokenMetrics
from app.pipeline import run as pipeline_run
from app.pipeline.decide import Engine
from app.store import db

ARC = "arc"
CHAIN = {"id": ARC, "label": "Arc", "pipeline": {"scan_limit": 500}, "sources": []}

#: The real driver, captured at import. tests/conftest.py replaces the module attribute with a
#: recorder for every test, because most tests are about what a request starts and must not
#: reach five sources; these are the tests about the round itself, so they call this one.
RUN_CHAIN = pipeline_run.run_chain


def _settings() -> dict:
    return {
        "schedule": {},
        "scoring": {"onchain_weight": 55, "offchain_weight": 45},
        "report": {"per_round": 2},
        "pipeline": {
            "enrich_limit": 2,
            "prefilter": {"market_cap_min_usd": 50_000, "market_cap_max_usd": 200_000,
                          "liquidity_min_usd": 8_000, "holders_min": 150},
            "safety": {},
        },
    }


def _row(ca: str, **kw) -> TokenMetrics:
    base = dict(ca=ca, symbol=ca[-4:].upper(), market_cap_usd=100_000, liquidity_usd=30_000,
                volume_24h_usd=10_000, holders=500, top10_pct=10.0, bundling_pct=1.0)
    base.update(kw)
    return TokenMetrics(**base)


class StoppingSource:
    """A scanner that asks for the round to stop as it answers.

    That is an operator pressing 中止 during the pull and nothing more exotic than that: the
    flag is what the request leaves behind, and the round is what reads it.
    """

    chains = (ARC,)

    def __init__(self, name: str, rows: list, run_id: str) -> None:
        self.name = name
        self.rows = rows
        self.run_id = run_id
        self.reads = 0

    def fetch_universe(self, chain_id: str, limit: int) -> list:
        self.reads += 1
        cancel.request(self.run_id)
        return list(self.rows)


class FillingSource:
    """A token-level source that asks for the stop while the pool is being filled.

    The bulk row a launchpad reports arrives with market numbers and no chip numbers, so the
    round buys those by name before the rules that read them run (`_top_up`). On Solana that
    fill is minutes long, and it was the one pass a stop had nowhere to land in: the flag was
    read only once the whole pool had been asked about.
    """

    chains = (ARC,)

    def __init__(self, name: str, rows: list, run_id: str) -> None:
        self.name = name
        self.rows = list(rows)
        self.run_id = run_id
        self.chips = 0
        self.safety = 0

    def fetch_universe(self, chain_id: str, limit: int) -> list:
        return list(self.rows)

    def fetch_chips(self, chain_id: str, ca: str):
        self.chips += 1
        # The press lands while these rows are already in flight. The sleep stands in for the
        # network, and it is what makes the window this test is about: the pool submits its
        # eight, the operator asks for a stop, and the other twenty-two are never asked.
        time.sleep(0.05)
        cancel.request(self.run_id)
        return (TokenMetrics(ca=ca, holders=500, top10_pct=10.0, bundling_pct=1.0),
                SafetyFacts())

    def fetch_token(self, chain_id: str, ca: str):
        """The gate's read. A stopped round must never pay for it."""
        self.safety += 1
        return TokenMetrics(ca=ca), SafetyFacts()


class QuietSource:
    """A source that was never asked anything. Its count is the assertion."""

    chains = (ARC,)

    def __init__(self, name: str) -> None:
        self.name = name
        self.reads = 0

    def fetch_universe(self, chain_id: str, limit: int) -> list:
        self.reads += 1
        return []


def test_a_stop_between_two_sources_costs_the_rest_of_the_pull():
    """The pull is four sources long, and the flag is read between them - not after them."""
    first = StoppingSource("gmgn", [_row("0xaaa")], "run-1")
    second = QuietSource("radardex")
    engine = Engine(CHAIN, sources=[first, second])
    try:
        with pytest.raises(cancel.RoundCancelled):
            engine.run(_settings(), "run-1")
    finally:
        cancel.clear("run-1")
    assert first.reads == 1
    assert second.reads == 0


def test_a_stopped_round_is_cancelled_and_writes_nothing(isolated_db, monkeypatch):
    """End to end, the way the worker drives it: the row says cancelled, and every table a
    published round fills is still empty."""
    from app.pipeline import decide

    created = pipeline_run.create_run(ARC)
    first = StoppingSource("gmgn", [_row("0xaaa"), _row("0xbbb")], created["id"])
    second = QuietSource("radardex")
    monkeypatch.setattr(decide.registry, "for_chain", lambda chain: [first, second])

    row = RUN_CHAIN(ARC, created["id"])

    assert row["status"] == RunStatus.CANCELLED.value
    # 用时 needs an end to stop at: cancelled is a state the page can read a duration from.
    assert row["finished_at"] is not None
    assert first.reads == 1 and second.reads == 0
    # The flag does not outlive the round that read it.
    assert not cancel.requested(created["id"])
    conn = db.connect()
    try:
        for table in ("reports", "snapshots", "run_items", "run_summary"):
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE run_id = ?" % table, (created["id"],)
            ).fetchone()["n"]
            assert count == 0, "%s has %d rows" % (table, count)
    finally:
        conn.close()


def test_a_round_stopped_before_it_starts_is_never_driven(isolated_db):
    """The scheduler starts whatever it finds at pending again, so a round stopped in the
    queue is settled as a row - and the worker that was going to drive it leaves it there."""
    created = pipeline_run.create_run(ARC)
    cancel.request(created["id"])
    row = RUN_CHAIN(ARC, created["id"])
    assert row["status"] == RunStatus.CANCELLED.value
    assert not cancel.requested(created["id"])


def test_a_stop_during_the_fill_costs_only_the_rows_in_flight():
    """The fill is the half of a round that spends minutes buying chip numbers for a whole
    pool, and a stop asked for inside it has to end the round there rather than after the
    rest of the pool: the gate would otherwise buy the same tokens' safety picture on its
    way to publishing nothing."""
    from app.pipeline import decide

    run_id = "run-fill"
    rows = [_row("0x%03x" % n, holders=None, top10_pct=None, bundling_pct=None)
            for n in range(30)]
    source = FillingSource("gmgn", rows, run_id)
    engine = Engine(CHAIN, sources=[source])
    try:
        with pytest.raises(cancel.RoundCancelled):
            engine.run(_settings(), run_id)
    finally:
        cancel.clear(run_id)
    # The rows the pool already had running are left to finish - that is what a stop costs -
    # and the other twenty-two are never asked.
    assert source.chips == decide.ENRICH_WORKERS
    assert source.safety == 0


def test_a_stopped_round_does_not_read_the_token_as_unreadable():
    """`gather` carries a failure as the row's own error, and a stop is not a failure: read
    as one, a stopped round puts "no readable links" on a card for a token it never asked
    about."""
    from app.report import research

    cancel.request("run-gather")
    try:
        row = {"ca": "0xaaa", "chain_id": ARC, "symbol": "AAA", "run_id": "run-gather"}
        with pytest.raises(cancel.RoundCancelled):
            research.gather(row)
    finally:
        cancel.clear("run-gather")


def test_a_ca_search_has_no_round_to_stop(monkeypatch):
    """A search is not a round and the row it drives carries no run id, so the same check is
    a no-op there rather than a stop nobody asked for."""
    from app.report import research

    monkeypatch.setattr(research, "_latest_metrics", lambda ca: ({}, {}))
    monkeypatch.setattr(research, "search_links", lambda *args, **kwargs: None)
    cancel.request("run-somewhere-else")
    try:
        found = research.gather({"ca": "0xaaa", "chain_id": ARC, "symbol": "AAA"})
    finally:
        cancel.clear("run-somewhere-else")
    assert found["links"] == []


def test_the_route_stops_a_round_nobody_has_started(client):
    created = client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    stopped = client.post("/api/v1/runs/%s/cancel" % created["id"])
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["status"] == RunStatus.CANCELLED.value
    assert body["finished_at"] is not None
    # Idempotent: a second press is the same answer, not an error.
    assert client.post("/api/v1/runs/%s/cancel" % created["id"]).json()["status"] == "cancelled"
    assert client.post("/api/v1/runs/nope/cancel").status_code == 404


def test_the_route_leaves_a_working_round_where_it_is(client):
    """The row is not relabelled under a round that is still working: the flag is the
    request, and the round is the thing that answers it - otherwise 用时 would stop at the
    click instead of at the moment the work really ended."""
    created = client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    pipeline_run._set_status(created["id"], RunStatus.COLLECTED.value)
    body = client.post("/api/v1/runs/%s/cancel" % created["id"]).json()
    assert body["status"] == RunStatus.COLLECTED.value
    assert cancel.requested(created["id"])
    cancel.clear(created["id"])
