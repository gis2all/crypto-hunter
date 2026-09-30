"""One screening round: orchestration only, the maths lives in core/.

A round is a job, not a request. `run_chain` walks it from `pending` to `written` on
whichever worker thread called it, and every stage boundary it crosses is a real state
transition, so the report page's SSE stream shows the round moving instead of jumping
from `pending` to a finished page.

The model is not part of the truth. When the LLM is unreachable the round still finishes:
the entries are skipped, so those tokens stay out of the ranking - which is this chain's
own policy for a token that has no entry - and the round says so in its own note rather
than shipping prose nobody decided.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from uuid import uuid4

from .. import cancel, config
from ..core.models import RunStatus
from ..sources import http
from ..report import lexicon, research
from ..store import db
from . import progress, states
from .decide import Engine
from .write import store_funnel, store_reports, store_round, store_span

log = logging.getLogger("crypto-hunter.pipeline")

#: What a delta reads off the previous round's picks. Named here, next to the read, so a
#: rename of a snapshot field cannot quietly turn every delta into a blank.
PREVIOUS_FIELDS = ("market_cap_usd", "liquidity_usd", "volume_24h_usd", "holders")


def _row(run_id: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def create_run(chain_id: str, trigger: str = "manual",
               scheduled_at: str | None = None,
               config_id: str | None = None) -> dict:
    """Create a round.

    A scheduled slot has a deterministic run_key, so a tick that fires twice creates one
    round: the second insert is ignored and the first round is returned. The key carries
    the set of settings too - a chain's sets share a clock, and each slot owes each of
    them its own round rather than one round between them.
    """
    cfg = config.config_entry(chain_id, config_id)  # KeyError for unknown chains
    run_id = uuid4().hex[:12]
    if scheduled_at:
        run_key = f"{chain_id}:{cfg['id']}:{scheduled_at}"
    else:
        run_key = f"{chain_id}:{cfg['id']}:{trigger}:{run_id}"
    # The settings this round will read are pinned here, at creation, not at the moment it
    # happens to start: a save that lands while the round waits in the queue must not move
    # a knob under a round already written down. `assemble` reads the same column back to
    # archive the round under the calibre it actually ran with.
    version_id = config.latest_settings_version(chain_id, cfg["id"])
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO runs (id, run_key, chain_id, config_id, trigger, status,"
            " scheduled_at, started_at, settings_version_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, run_key, chain_id, cfg["id"], trigger, RunStatus.PENDING.value,
             scheduled_at, db.now_iso(), version_id),
        )
        row = conn.execute("SELECT * FROM runs WHERE run_key = ?", (run_key,)).fetchone()
    finally:
        conn.close()
    if row is None:  # pragma: no cover - nothing can delete the row in between
        raise RuntimeError(f"run {run_key} disappeared between insert and select")
    return dict(row)


def pending_runs() -> list[dict]:
    """Rounds nothing has started yet, oldest first.

    `pending` is the only state that means "never touched": the first thing a round does
    is move itself on, so a row still sitting here belongs to a process that went away
    between claiming the slot and running it.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, chain_id, config_id FROM runs WHERE status = ? ORDER BY started_at",
            (RunStatus.PENDING.value,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def abandoned_runs() -> list[dict]:
    """Rounds the process before this one stopped reading mid-flight.

    Everything that is neither `pending` nor terminal. `pending` has its own answer -
    nobody ever read it, so it can simply be run - while these were being read and stopped
    being read. The archive also still carries rows from the state machine before this one,
    at statuses no worker will ever move on; they are the same shape and get the same
    answer, which is the only way a row like that ever stops looking like it is running.

    Liveness itself is not asked here: at startup nothing can be in flight, and while a
    round is reading, the executor is the one that knows (`schedule`).
    """
    conn = db.connect()
    try:
        marks = ",".join("?" for _ in states.TERMINAL)
        rows = conn.execute(
            "SELECT * FROM runs WHERE status NOT IN (%s) AND status != ?" % marks,
            (*states.TERMINAL, RunStatus.PENDING.value),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def close_abandoned(run_id: str) -> str:
    """Put a leftover round where its own output says it belongs; return that status.

    A round that stored reports did finish - the write is the last thing before the status
    moves on - so it closes as `researched`, which is what a finished round is, and its
    cards stay readable. One that stored none never got that far, so it closes as `failed`
    with the reason on it. One answer for both would either call a finished round broken or
    a broken round finished.

    A status this state machine does not know (`sent`, from before the enum existed) has no
    legal transition at all and is written straight (the note at the end says why). Leaving
    it is the one thing that does not work: the row keeps reading as a round still running.
    """
    row = _row(run_id)
    conn = db.connect()
    try:
        stored = conn.execute(
            "SELECT 1 FROM reports WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    target = RunStatus.RESEARCHED.value if stored is not None else RunStatus.FAILED.value
    reason = None if stored is not None else (
        "上一进程中途退出（进程重启），"
        "这一轮已中断，未写出报告卡")
    if states.can(row["status"], target):
        _set_status(run_id, target, reason)
        return target
    # A status this machine does not have (`sent`, from the enum before this one). `can`
    # guards rounds that are in flight - the machine must not be able to move one where it
    # should not go - and a status it does not know cannot be in flight: no worker
    # understands it, so nothing is holding this row. Written straight, or it stays
    # non-terminal for good and reads as a round still running to anyone who scrolls back.
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (target, reason, db.now_iso(), run_id),
        )
    finally:
        conn.close()
    return target


def scheduled_run(chain_id: str, config_id: str, slot: str) -> dict | None:
    """The round for one cron slot under one set of settings, or None when it has one."""
    conn = db.connect()
    try:
        seen = conn.execute(
            "SELECT 1 FROM runs WHERE run_key = ?", (f"{chain_id}:{config_id}:{slot}",)
        ).fetchone()
    finally:
        conn.close()
    if seen is not None:
        return None
    return create_run(chain_id, trigger="schedule", scheduled_at=slot,
                      config_id=config_id)


def _set_progress(run_id: str, percent: int, label: str) -> None:
    """Where the round is, for the bar. Written on every checkpoint and cheap by design:
    it is the only thing on the page that moves while a round is twenty minutes long.

    The percentage only ever goes up (`MAX`): the bands are not in wall-clock order - the
    draft band (86-96) is filled before the score band (82-86) - so a plain assignment made
    the bar walk backwards on any round that drafted lexicon entries. The label still moves:
    it says what the round is doing now, which is not a high-water mark.
    """
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE runs SET progress = MAX(progress, ?), progress_label = ? WHERE id = ?",
            (max(0, min(100, int(percent))), label, run_id),
        )
    finally:
        conn.close()


def _set_status(run_id: str, status: str, error: str | None = None) -> None:
    current = _row(run_id)["status"]
    if current == status:
        return
    if not states.can(current, status):
        raise ValueError(f"illegal transition {current} -> {status}")
    conn = db.connect()
    try:
        if error is not None:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error[:1000], db.now_iso(), run_id),
            )
        elif status in states.TERMINAL:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, db.now_iso(), run_id),
            )
        else:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
    finally:
        conn.close()


def _roster_symbols(conn, cas: list[str]) -> dict[str, str]:
    """CA -> name, from the token roster rather than from the round that saw it.

    A snapshot carries the symbol its round knew, and a round that never learned a name
    carries none. 退出 is the row that has nothing else to fall back on: by the time it is
    drawn the token is out of the report, and only this table still knows what it was.
    """
    if not cas:
        return {}
    marks = ",".join("?" * len(cas))
    rows = conn.execute(
        "SELECT ca, symbol FROM tokens WHERE ca IN (%s) AND symbol IS NOT NULL" % marks,
        cas,
    ).fetchall()
    return {str(row["ca"]).lower(): row["symbol"] for row in rows}


def peak_market_caps(chain_id: str) -> dict[str, float]:
    """The highest market cap this project has seen for each CA on a chain.

    距高回撤 compares today against this. It is not a pool high and not a price high but the
    highest number any source has reported for the token in a round - and it is written for
    every scanned token, so a token that ran above this chain band and came back still
    carries the high it reached.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ca, peak_market_cap_usd FROM tokens"
            " WHERE chain_id = ? AND peak_market_cap_usd IS NOT NULL",
            (chain_id,),
        ).fetchall()
    finally:
        conn.close()
    return {str(row["ca"]).lower(): float(row["peak_market_cap_usd"]) for row in rows}


def previous_picks(chain_id: str, run_id: str, config_id: str | None = None) -> list[dict]:
    """The last written round's picks, with the numbers a delta is computed from.

    The last round of *this* set of settings: a second set on the same chain keeps its own
    library, and reading the first set's picks as "last round" would report a token as
    new-in or dropped-out against a universe it was never in.
    """
    conn = db.connect()
    try:
        last = conn.execute(
            "SELECT id FROM runs WHERE chain_id = ? AND config_id = ? AND id <> ?"
            " AND selected > 0"
            " ORDER BY COALESCE(scheduled_at, started_at) DESC LIMIT 1",
            (chain_id, config.config_entry(chain_id, config_id)["id"], run_id),
        ).fetchone()
        if last is None:
            return []
        out: list[dict] = []
        rows = conn.execute(
            "SELECT ca FROM reports WHERE run_id = ? ORDER BY rank", (last["id"],)
        ).fetchall()
        names = _roster_symbols(conn, [str(row["ca"]).lower() for row in rows])
        for item in rows:
            snap = conn.execute(
                "SELECT metrics_json FROM snapshots WHERE run_id = ? AND ca = ?",
                (last["id"], item["ca"]),
            ).fetchone()
            metrics = json.loads(snap["metrics_json"]) if snap is not None else {}
            out.append(
                {
                    "ca": str(item["ca"]).lower(),
                    "symbol": metrics.get("symbol") or names.get(str(item["ca"]).lower()),
                    **{key: metrics.get(key) for key in PREVIOUS_FIELDS},
                }
            )
        return out
    finally:
        conn.close()


def _finish(run_id: str) -> None:
    """The last two steps of a round, in one place so the rule can be read at once.

    `written` is not the end of anything: it says the rows are in, and a round that stops
    there reads as still running. There is no outlet to wait for - the delivery is the
    stored report, and whatever reads it does so from the rows - so the round goes straight
    to `researched`, which is also the state that writes `finished_at` and gives the page a
    stopwatch to stop. Without this the run panel would show 正在运行 and a growing 已用 on
    every round forever.
    """
    _set_status(run_id, RunStatus.WRITTEN.value)
    _set_status(run_id, RunStatus.RESEARCHED.value)


def stop_run(run_id: str) -> dict:
    """Record that the operator stopped this round, and hand the row back.

    One place, because the state has to read the same however the stop arrived: while the
    round waited in the queue, at a checkpoint inside a stage, or from an answer abandoned in
    flight. Nothing is rolled back here that was not already written - see `run_chain`.
    """
    _set_status(run_id, RunStatus.CANCELLED.value)
    return _row(run_id)


def fail_run(run_id: str, error: str) -> None:
    """Put a round that never got going into a terminal state.

    A run created for a cron slot and then not driven looks exactly like a run that is
    still working, and an operator cannot tell the two apart from the page. Whoever
    fails to start a round owns saying so.
    """
    if _row(run_id)["status"] in states.TERMINAL:
        return
    _set_status(run_id, RunStatus.FAILED.value, error)


def run_chain(chain_id: str, run_id: str, config_id: str | None = None) -> dict:
    """Drive one round to `written`. Synchronous: the caller owns the thread."""
    reasons: list[str] = []

    def entries(targets: list[dict], tell) -> dict[str, dict]:
        """This round's off-chain entry for every token it has to judge.

        The reading pass, and it is where the words come from: each token gets one model
        call against the pages the search could find for it. A token that cannot be read
        or cannot be written about is not a failed round - it has no entry, and without an
        entry it has no score, so it stays out of the ranking and the note says why.
        """
        return research.research_entries(chain, targets, run_id, notes=reasons,
                                        on_done=tell)

    try:
        # The tallies are per round, so the log line at the end describes this round and
        # not whatever the process has been doing since it started.
        http.stats(reset=True)
        row = _row(run_id)
        # Which of the chain's sets of settings this round belongs to comes off the row and
        # not off the caller: the row is what the archive, the口径 line and the report
        # library are read through, so it is the only answer worth having.
        config_id = row.get("config_id") or config_id
        chain = config.chain(chain_id, config_id)  # KeyError for unknown chains
        # The first line of a round, and the one that proves the log is wired up: a round
        # that leaves no trace of itself is how "why was that round slow" went unanswerable.
        log.info("round %s started on %s", run_id, chain_id)
        # Two ways this round is already over before this thread touches it. The operator
        # stopped it while it waited in the queue - and a round with no worker never reads the
        # flag, so the row itself carries the request (`stop_run`) - or the stop landed
        # between the queue and here, in which case the flag is the only trace. Neither may
        # start a round that has ended: the scheduler hands every round it finds at `pending`
        # to a worker, and it must not be able to resurrect a stop.
        if row["status"] in states.TERMINAL:
            cancel.clear(run_id)
            return row
        if cancel.requested(run_id):
            return stop_run(run_id)
        settings = config.settings_payload(chain_id, row.get("settings_version_id"),
                                           config_id)
        engine = Engine(
            chain,
            drafts=entries,
            previous=previous_picks(chain_id, run_id, config_id),
            peaks=peak_market_caps(chain_id),
        )
        # The stopwatch spans the whole round, not just the engine: the write happens out here,
        # and a phase list that stopped at `selected` is why the log line and the wall clock
        # used to disagree by the better part of a minute.
        clock = progress.Timer(
            run_id,
            # The stopwatch already knows each phase's wall clock; this is what puts it on
            # the page as it happens, instead of leaving it in a log line nobody reads
            # until a round has gone wrong.
            on_span=lambda phase, seconds: store_span(run_id, phase, seconds),
        )
        result = engine.run(
            settings, run_id,
            on_stage=lambda status: _set_status(run_id, status),
            on_progress=lambda percent, label: _set_progress(run_id, percent, label),
            clock=clock,
            on_funnel=lambda **fields: store_funnel(run_id, **fields),
        )
        result.notes.extend(reasons)

        # The lexicon holds one round and only one: the entries this round wrote. Last
        # round's rows go now, so nothing can be read as if it were this round's opinion.
        # A cleanup that throws is a round that still published: logged, never raised.
        try:
            for_removal = lexicon.forget_other_rounds(chain_id, config_id, run_id)
            if for_removal:
                result.notes.append("词条清理：移除上一轮 %d 条" % for_removal)
        except Exception:
            log.exception("round %s could not clear the previous lexicon", run_id)

        conn = db.connect()
        try:
            conn.execute("BEGIN")
            store_round(conn, result)
            store_reports(conn, result)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        clock.mark("write")
    except cancel.RoundCancelled:
        # Not a failure: the operator stopped a round that was going where it was told to go.
        # The last checkpoint is the reason a stopped round can still be read - the progress
        # label it stopped under stays on the row, and the phases before it keep their
        # stopwatch readings.
        log.info("round %s stopped by the operator", run_id)
        clock.emit()
        return stop_run(run_id)
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        _set_status(run_id, RunStatus.FAILED.value, f"storage error: {exc!r}")
        clock.emit()
        return _row(run_id)
    except Exception as exc:
        _set_status(run_id, RunStatus.FAILED.value, repr(exc)[:500])
        clock.emit()
        return _row(run_id)
    finally:
        # Whatever the round ended as, it is over: the next round has its own id, and a flag
        # left behind would be a lie about a round that no longer exists.
        cancel.clear(run_id)

    clock.emit()
    for line in http.stats_lines():
        log.info("round %s host %s", run_id, line)
    _finish(run_id)
    return _row(run_id)
