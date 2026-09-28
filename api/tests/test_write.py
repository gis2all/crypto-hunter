"""What `store_round` writes back about a token high.

距高回撤 reads `tokens.peak_market_cap_usd`, and the write-back is what makes that
reading a real high instead of whatever our own rounds happened to sample - the cheap half
of the rule used to be a lower bound that only ever flagged and never dropped.
"""

from app.core.models import TokenMetrics
from app.pipeline import write
from app.pipeline.decide import Candidate, RoundResult
from app.store import db

CA = "0xabc0000000000000000000000000000000000001"


def _round(run_id, item, **settings):
    return RoundResult(
        chain_id="arc", run_id=run_id, universe=1,
        items={item.ca: item}, survivors=[item], scored=[item], picked=[item],
        rule_counts=[], sources=[], reports=[], changes=[], notes=[],
        settings=settings,
    )


def _run_row(conn, run_id):
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, status, selected, started_at)"
        " VALUES (?, ?, 'arc', 'manual', 'researched', 0, '2026-09-27T10:00:00+08:00')",
        (run_id, run_id),
    )


def _peak(conn, ca=CA):
    row = conn.execute(
        "SELECT peak_market_cap_usd FROM tokens WHERE ca = ?", (ca,)).fetchone()
    return row[0] if row else None


def test_a_source_high_is_written_back_as_the_recorded_peak(isolated_db):
    """GMGN own 历史最高市值 is the high 距高回撤 wants, and the write-back is
    what puts it where the next round reads it: a $113.8K token whose source high was
    $5.05M is remembered as a $5.05M high."""
    item = Candidate(
        ca=CA,
        metrics=TokenMetrics(ca=CA, symbol="ABC", market_cap_usd=113_800,
                             high_market_cap_usd=5_048_753),
        decision="selected",
    )
    conn = db.connect()
    try:
        _run_row(conn, "run-1")
        write.store_round(conn, _round("run-1", item, pipeline={"prefilter": {}}))
        conn.commit()
        assert _peak(conn) == 5_048_753
    finally:
        conn.close()


def test_the_peak_never_goes_back_down(isolated_db):
    """Once a round has seen the high it stays: a later round whose own sample is lower,
    or whose source says nothing, must not lower the column the rule compares against."""
    conn = db.connect()
    try:
        _run_row(conn, "run-1")
        high = Candidate(
            ca=CA,
            metrics=TokenMetrics(ca=CA, market_cap_usd=113_800,
                                 high_market_cap_usd=5_048_753),
            decision="selected",
        )
        write.store_round(conn, _round("run-1", high, pipeline={"prefilter": {}}))
        _run_row(conn, "run-2")
        later = Candidate(ca=CA, metrics=TokenMetrics(ca=CA, market_cap_usd=150_000),
                          decision="candidate")
        write.store_round(conn, _round("run-2", later, pipeline={"prefilter": {}}))
        conn.commit()
        assert _peak(conn) == 5_048_753
    finally:
        conn.close()
