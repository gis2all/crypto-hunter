"""The ticker: which minute fires, and that one slot fires once."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.pipeline import schedule
from app.store import db

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _chain(cron="0 2,5,8 * * *"):
    """One chain under one set of settings, in the shape the ticker walks."""
    return {
        "id": "arc",
        "label": "Arc",
        "enabled": True,
        "config_id": "default",
        "schedule": {"cron": cron, "timezone": "Asia/Shanghai", "interval_hours": 3},
    }


def test_only_the_listed_hours_fire():
    assert schedule.due_slot(_chain(), datetime(2026, 9, 24, 5, 0, 20, tzinfo=SHANGHAI)) == "2026-09-24T05:00:00+08:00"
    assert schedule.due_slot(_chain(), datetime(2026, 9, 24, 6, 0, 0, tzinfo=SHANGHAI)) is None


def test_a_slot_is_hour_aligned_so_the_second_does_not_matter():
    early = schedule.due_slot(_chain(), datetime(2026, 9, 24, 5, 0, 3, tzinfo=SHANGHAI))
    late = schedule.due_slot(_chain(), datetime(2026, 9, 24, 5, 0, 55, tzinfo=SHANGHAI))
    assert early == late == "2026-09-24T05:00:00+08:00"


def test_another_clock_is_read_in_the_chains_own_timezone():
    """02:00 Shanghai is 18:00 UTC the day before; the slot has to belong to the chain."""
    chain = _chain()
    utc = datetime(2026, 9, 23, 18, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert schedule.due_slot(chain, utc) == "2026-09-24T02:00:00+08:00"


def test_steps_and_ranges_are_understood():
    assert schedule.parse_slots("0 */3 * * *")[1] == {0, 3, 6, 9, 12, 15, 18, 21}
    assert schedule.parse_slots("30 2-4 * * *") == ({30}, {2, 3, 4})


def test_a_cron_this_code_does_not_understand_schedules_nothing():
    assert schedule.parse_slots("0 2 * * 1") is None
    assert schedule.parse_slots("nonsense") is None
    assert schedule.parse_slots("0 2,99 * * *") is None
    assert schedule.parse_slots("*/0 2 * * *") is None


def test_a_slot_runs_once_however_often_the_ticker_looks(isolated_db, monkeypatch):
    started = []
    monkeypatch.setattr(schedule, "submit",
                        lambda chain_id, run_id, config_id=None: started.append(run_id))
    monkeypatch.setattr(schedule.config, "enabled_configs", lambda: [_chain()])
    now = datetime(2026, 9, 24, 5, 0, 10, tzinfo=SHANGHAI)
    assert len(schedule.tick(now)) == 1
    assert schedule.tick(now + timedelta(seconds=30)) == []
    assert len(started) == 1


def test_the_worker_runs_what_is_submitted_on_its_own_thread(isolated_db, monkeypatch):
    """The queue is real: `submit` hands the round to a worker thread, so the request that
    asked for it does not have to wait for five sources."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    monkeypatch.setattr(
        schedule.pipeline_run, "run_chain",
        lambda chain_id, run_id, config_id=None:
        calls.append((chain_id, run_id, threading.current_thread().name)),
    )
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round")
    monkeypatch.setattr(schedule, "executor", lambda: pool)
    try:
        schedule.submit("arc", "run-1").result(timeout=10)
    finally:
        pool.shutdown(wait=True)
    assert calls and calls[0][:2] == ("arc", "run-1")
    assert calls[0][2].startswith("round")


def test_a_round_that_cannot_start_is_marked_failed_not_left_pending(isolated_db, monkeypatch):
    """A future nobody inspects swallows its exception, which would leave a run looking
    like it is still working. The state is where that has to show up."""
    from app.pipeline import run as pipeline_run
    from app.store import db

    created = pipeline_run.create_run("arc")

    def boom(chain_id, run_id, config_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(schedule.pipeline_run, "run_chain", boom)
    try:
        schedule.submit("arc", created["id"])
    except RuntimeError:
        pass
    conn = db.connect()
    try:
        status = conn.execute("SELECT status, error FROM runs WHERE id = ?", (created["id"],)).fetchone()
    finally:
        conn.close()
    assert status["status"] == "failed"
    assert "boom" in (status["error"] or "")


def test_a_round_a_restart_left_pending_is_taken_on_again(isolated_db, monkeypatch):
    """The ticker looks at the clock, so a slot already claimed is never claimed again: a
    round left pending by a process that went away would never be started by anyone."""
    from app.pipeline import run as pipeline_run

    left = pipeline_run.create_run("arc")
    done = pipeline_run.create_run("arc")
    pipeline_run._set_status(done["id"], "collected")
    taken = []
    monkeypatch.setattr(schedule, "submit",
                        lambda chain_id, run_id, config_id=None: taken.append(run_id))

    assert schedule.recover() == [left["id"]]
    assert taken == [left["id"]], "only the round nothing has touched"

    # And the one that *was* being read is closed, not left looking like it still is: after
    # a restart nothing owns it, so the page would otherwise count up for the rest of the
    # week. It stored no reports, so it did not finish, and it says so.
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status, error, finished_at FROM runs WHERE id = ?", (done["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"
    assert row["finished_at"], "a round with no finish time reads as one still going"
    assert "中断" in row["error"]


def test_a_round_nobody_owns_is_not_a_chain_with_a_round_in_flight(isolated_db, monkeypatch):
    """The ticker used to ask the table "is any round not terminal?" - and a process that
    dies mid-round leaves one behind for good, so every chain with one read as busy and no
    scheduled round ever started again (2026-09-30: both chains, eleven hours of slots)."""
    from app.pipeline import run as pipeline_run

    stale = pipeline_run.create_run("arc")
    pipeline_run._set_status(stale["id"], "collected")
    monkeypatch.setattr(schedule, "_live", {})
    assert schedule.busy_chains() == set()
    assert schedule.live_configs("arc") == []

    started = []
    monkeypatch.setattr(schedule, "submit",
                        lambda chain_id, run_id, config_id=None: started.append(run_id))
    monkeypatch.setattr(schedule.config, "enabled_configs", lambda: [_chain()])
    now = datetime(2026, 9, 24, 5, 0, 10, tzinfo=SHANGHAI)
    assert len(schedule.tick(now)) == 1, "a leftover must not hold the chain out of the tick"
    assert len(started) == 1


def test_a_leftover_that_stored_reports_did_finish(isolated_db):
    """Which terminal state a leftover gets is read off its own output: the write is the
    last thing before the status moves on, so a round holding cards did finish."""
    from app.pipeline import run as pipeline_run

    done = pipeline_run.create_run("arc")
    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET status = 'written' WHERE id = ?", (done["id"],))
        conn.execute(
            "INSERT INTO reports (run_id, ca, rank, status, payload_json, created_at)"
            " VALUES (?, '0xaa', 1, 'written', '{}', ?)",
            (done["id"], db.now_iso()),
        )
    finally:
        conn.close()
    assert pipeline_run.close_abandoned(done["id"]) == "researched"
    assert pipeline_run.abandoned_runs() == []


def test_a_leftover_from_a_state_machine_that_is_gone_is_closed_too(isolated_db):
    """`sent` is not in the enum any more, so `states.can` has no answer for it - and a
    row left at it keeps reading as a round still running to anyone who scrolls back."""
    from app.pipeline import run as pipeline_run

    old = pipeline_run.create_run("arc")
    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET status = 'sent' WHERE id = ?", (old["id"],))
    finally:
        conn.close()
    assert pipeline_run.close_abandoned(old["id"]) == "failed"
    assert pipeline_run.abandoned_runs() == []

def test_a_round_of_this_process_is_a_chain_with_a_round_in_flight(isolated_db, monkeypatch):
    """The other half: a round that *is* being read has to hold its chain out of the tick,
    or two rounds would pull the same sources off the same node at once."""
    monkeypatch.setattr(schedule, "_live", {"abc123": ("arc", "default")})
    assert schedule.busy_chains() == {"arc"}
    assert schedule.live_configs("arc") == ["default"]
    assert schedule.live_configs("robinhood") == []


def test_a_chain_with_no_interval_has_no_cron_of_its_own():
    assert schedule.due_slot({"id": "arc", "schedule": {}}, datetime(2026, 9, 24, 5, 0, tzinfo=SHANGHAI)) is None
