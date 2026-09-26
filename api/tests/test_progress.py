"""The bar, and the state a round is allowed to stop in."""

import sqlite3

from app.pipeline import progress, run as pipeline_run
from app.store import db


def _walked_to_written(run_id: str) -> None:
    """The states a round really crosses, so a finish is tested from a legal state."""
    for status in ("collected", "scored", "selected"):
        pipeline_run._set_status(run_id, status)


def _row(run_id: str) -> dict:
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        conn.close()


def test_the_bands_cover_the_bar_without_a_gap_or_a_backwards_step():
    spans = sorted((low, high) for low, high, _words in progress.PHASES.values())
    assert spans[0][0] == 0
    assert spans[-1][1] == 100
    for (_low, high), (next_low, _next_high) in zip(spans, spans[1:]):
        assert next_low == high


def test_a_phase_fills_its_own_band_and_not_the_whole_bar():
    """The gate is most of a round: it has to move inside its band, not jump from 8 to 82."""
    assert progress.pct("gate", 0, 89) == 8
    assert progress.pct("gate", 89, 89) == 82
    assert 8 < progress.pct("gate", 45, 89) < 82
    assert progress.pct("pull", 1, 4) == 2


def test_a_phase_with_nothing_to_count_reads_as_done():
    assert progress.pct("gate", 0, 0) == 82


def test_every_phase_has_words_for_the_page():
    for phase in progress.PHASES:
        assert progress.label(phase)


def test_the_bar_is_written_where_the_page_reads_it(isolated_db):
    created = pipeline_run.create_run("arc")

    pipeline_run._set_progress(created["id"], 46, "安全判定")
    assert (_row(created["id"])["progress"], _row(created["id"])["progress_label"]) == (46, "安全判定")

    pipeline_run._set_progress(created["id"], 999, "生成报告")
    assert _row(created["id"])["progress"] == 100, "the page draws a scaleX, not a scrollbar"


def test_a_round_with_no_outlet_finishes_instead_of_waiting_to_send(isolated_db, monkeypatch):
    """The outlet module says nothing is wired yet, so there is no push to wait for. A round
    left at `written` would read as still running forever, with a stopwatch that never stops."""
    monkeypatch.setattr(pipeline_run.out, "configured", lambda: False)
    created = pipeline_run.create_run("arc")
    _walked_to_written(created["id"])

    pipeline_run._finish(created["id"])

    row = _row(created["id"])
    assert row["status"] == "sent"
    assert row["finished_at"], "the elapsed clock needs something to stop at"


def test_a_round_that_can_send_stops_before_the_send(isolated_db, monkeypatch):
    """With an outlet wired the round must wait at `written` for the push, not claim it was
    delivered: this is the branch the outlet module will flip when Telegram comes back."""
    monkeypatch.setattr(pipeline_run.out, "configured", lambda: True)
    created = pipeline_run.create_run("arc")
    _walked_to_written(created["id"])

    pipeline_run._finish(created["id"])

    row = _row(created["id"])
    assert row["status"] == "written"
    assert row["finished_at"] is None


def test_the_push_row_says_it_is_not_configured():
    from app import out

    assert out.configured() is False
    assert "未配置" in out.status()


def test_a_database_from_before_the_bar_gains_the_columns(tmp_path, monkeypatch):
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, and the
    database this project has been running on is exactly that case."""
    path = tmp_path / "old.db"
    db.init_db(db.connect(path))

    # What the database on this machine looked like before the bar existed.
    conn = db.connect(path)
    conn.execute("ALTER TABLE runs DROP COLUMN progress")
    conn.execute("ALTER TABLE runs DROP COLUMN progress_label")
    conn.close()

    db.init_db(db.connect(path))

    conn = db.connect(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    finally:
        conn.close()
    assert {"progress", "progress_label"} <= columns


def test_the_bar_never_walks_backwards(monkeypatch):
    """The bands are not in wall-clock order - the draft band (86-96) fills before the score
    band (82-86) - so a round that drafted entries used to knock the bar back ten points."""
    chain_id = "arc"
    row = pipeline_run.create_run(chain_id)
    run_id = row["id"]
    try:
        pipeline_run._set_progress(run_id, progress.pct("draft", 1, 1), progress.label("draft"))
        assert _row(run_id)["progress"] == 96

        pipeline_run._set_progress(run_id, progress.pct("score", 1, 1), progress.label("score"))

        after = _row(run_id)
        assert after["progress"] == 96, "the number is a high-water mark"
        assert after["progress_label"] == progress.label("score"), "the words still move"
    finally:
        conn = db.connect()
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
        conn.close()