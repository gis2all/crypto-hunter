"""The ticker: which minute fires, and that one slot fires once."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.pipeline import schedule

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _chain(cron="0 2,5,8 * * *"):
    return {
        "id": "arc",
        "label": "Arc",
        "enabled": True,
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
    monkeypatch.setattr(schedule, "submit", lambda chain_id, run_id: started.append(run_id))
    monkeypatch.setattr(schedule.config, "enabled_chains", lambda: [_chain()])
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
        lambda chain_id, run_id: calls.append((chain_id, run_id, threading.current_thread().name)),
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

    def boom(chain_id, run_id):
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
    monkeypatch.setattr(schedule, "submit", lambda chain_id, run_id: taken.append(run_id))

    assert schedule.recover() == [left["id"]]
    assert taken == [left["id"]], "only the round nothing has touched"


def test_a_chain_with_no_interval_has_no_cron_of_its_own():
    assert schedule.due_slot({"id": "arc", "schedule": {}}, datetime(2026, 9, 24, 5, 0, tzinfo=SHANGHAI)) is None
