"""One screening round: orchestration only, the maths lives in core/.

A round is a job, not a request. `run_chain` walks it from `pending` to `written` on
whichever worker thread called it, and every stage boundary it crosses is a real state
transition, so the report page's SSE stream shows the round moving instead of jumping
from `pending` to a finished page.

The model is not part of the truth. When the LLM is unreachable the round still finishes:
drafts are skipped, so those tokens stay out of the ranking - which is this chain's own
policy for a token that has no entry - and the cards carry the round's facts with no
commentary rather than prose nobody decided.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from uuid import uuid4

from .. import cancel, config, llm, out
from ..core.models import RunStatus
from ..sources import http
from ..report import draft as report_draft
from ..report import research
from ..store import db
from . import progress, states
from .decide import Engine, RoundResult
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
               scheduled_at: str | None = None) -> dict:
    """Create a round.

    A scheduled slot has a deterministic run_key, so a tick that fires twice creates one
    round: the second insert is ignored and the first round is returned.
    """
    config.chain(chain_id)  # KeyError for unknown chains
    run_id = uuid4().hex[:12]
    run_key = f"{chain_id}:{scheduled_at}" if scheduled_at else f"{chain_id}:{trigger}:{run_id}"
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO runs (id, run_key, chain_id, trigger, status, scheduled_at,"
            " started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, run_key, chain_id, trigger, RunStatus.PENDING.value, scheduled_at,
             db.now_iso()),
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
            "SELECT id, chain_id FROM runs WHERE status = ? ORDER BY started_at",
            (RunStatus.PENDING.value,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def scheduled_run(chain_id: str, slot: str) -> dict | None:
    """The round for one cron slot, or None when that slot already has one."""
    conn = db.connect()
    try:
        seen = conn.execute(
            "SELECT 1 FROM runs WHERE run_key = ?", (f"{chain_id}:{slot}",)
        ).fetchone()
    finally:
        conn.close()
    if seen is not None:
        return None
    return create_run(chain_id, trigger="schedule", scheduled_at=slot)


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
        elif status in states.DELIVERED:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, db.now_iso(), run_id),
            )
        else:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
    finally:
        conn.close()


def lexicon_map(chain_id: str) -> dict[str, dict]:
    """Every entry this chain has, read once per round.

    Read once on purpose: an entry edited while a round is running must not change what
    that round scored. An entry drafted by this round reaches the engine through `drafts`,
    not through here.
    """
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM lexicon WHERE chain_id = ?", (chain_id,)).fetchall()
    finally:
        conn.close()
    return {str(row["ca"]).lower(): dict(row) for row in rows}


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


def previous_picks(chain_id: str, run_id: str) -> list[dict]:
    """The last written round's picks, with the numbers a delta is computed from."""
    conn = db.connect()
    try:
        last = conn.execute(
            "SELECT id FROM runs WHERE chain_id = ? AND id <> ? AND selected > 0"
            " ORDER BY COALESCE(scheduled_at, started_at) DESC LIMIT 1",
            (chain_id, run_id),
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


#: How many beaten candidates the writer sees beside the winners.
NEAR_MISS = 6


def near_misses(result: RoundResult, limit: int = NEAR_MISS) -> list:
    """The scored candidates that did not make the report, best first.

    The cut is the same one the page makes for its own 落选 table, so the written words and
    the table argue over one list instead of two.
    """
    picked = {item.ca.lower() for item in result.picked}
    rest = [item for item in result.scored if item.ca.lower() not in picked]
    rest.sort(key=lambda item: (item.score is not None, item.score), reverse=True)
    return rest[:limit]


def commentary(chain: dict, result: RoundResult, run_id: str) -> dict[str, str]:
    """The 本期点评 for the picks, or {} with the reason recorded on the round."""
    try:
        return report_draft.write_commentary(
            chain, result.picked, result.changes, result.notes, run_id,
            near_miss=near_misses(result),
        )
    except llm.LLMUnavailable as exc:
        result.notes.append(f"本期点评未生成：{exc}")
        return {}


def _finish(run_id: str) -> None:
    """The last two steps of a round, in one place so the rule can be read at once.

    `written` is not the end of anything: the state machine puts `sent` after it, and a
    round that stops at `written` reads as still running. With no outlet configured there
    is no push to wait for - the delivery is the stored report - so the round goes to `sent`,
    which is also the state that writes `finished_at` and gives the page a stopwatch to stop.
    Without this the run panel would show 正在运行 and a growing 已用 on every round forever.
    """
    _set_status(run_id, RunStatus.WRITTEN.value)
    if not out.configured():
        _set_status(run_id, RunStatus.SENT.value)


def mark_researched(run_id: str) -> None:
    """The last transition in the machine: the deep pass over this round is finished.

    Not a human step and not a delivery: it means every card the round published has an
    entry whose research state is terminal. Whoever drives the research worker owns saying
    so, because the round thread that published is long gone by then.
    """
    _set_status(run_id, RunStatus.RESEARCHED.value)


def researched_runs(chain_id: str) -> list[str]:
    """The `sent` rounds of one chain whose published cards have all been researched.

    Derived from the rows rather than counted in memory: a round is settled when each of its
    reports points at an entry in a terminal research state, which is still the right answer
    after a restart that forgot everything. `failed` counts as settled - a page that will not
    load must not hold a round open forever.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.id AS id, COUNT(*) AS total,"
            " SUM(CASE WHEN COALESCE(l.research_state, 'none') IN ('done', 'failed')"
            "     THEN 1 ELSE 0 END) AS settled"
            " FROM runs r JOIN reports p ON p.run_id = r.id"
            " LEFT JOIN lexicon l ON l.ca = p.ca"
            " WHERE r.status = ? AND r.chain_id = ?"
            " GROUP BY r.id",
            (RunStatus.SENT.value, chain_id),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["id"]) for row in rows if row["settled"] == row["total"]]


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


def run_chain(chain_id: str, run_id: str) -> dict:
    """Drive one round to `written`. Synchronous: the caller owns the thread."""
    reasons: list[str] = []

    def refresh(targets: list[dict], tell) -> dict[str, dict]:
        """Rewrite the entries whose refresh condition fired this round.

        A failure here costs no token its place: the old entry still scores and still prints,
        and the card says the words are older than the numbers instead. That is why this
        returns `{}` rather than raising - a model outage must not change the ranking.
        """
        try:
            return report_draft.refresh_lexicon(chain, targets, run_id, notes=reasons,
                                               on_batch=tell)
        except llm.LLMUnavailable as exc:
            reasons.append(f"词条重写未生成：{exc}")
            return {}

    def drafts(gaps: list[dict], tell) -> dict[str, dict]:
        """A first entry for every shortlisted token that has none.

        A model that refuses is not a failed round: the token simply has no entry, which
        is the same state it was in before, and the round's own note says why.
        """
        try:
            return report_draft.draft_lexicon(chain, gaps, run_id, notes=reasons, on_batch=tell)
        except llm.LLMUnavailable as exc:
            reasons.append(f"词条草稿未生成：{exc}")
            return {}

    try:
        # The tallies are per round, so the log line at the end describes this round and
        # not whatever the process has been doing since it started.
        http.stats(reset=True)
        chain = config.chain(chain_id)  # KeyError for unknown chains
        # The first line of a round, and the one that proves the log is wired up: a round
        # that leaves no trace of itself is how "why was that round slow" went unanswerable.
        log.info("round %s started on %s", run_id, chain_id)
        row = _row(run_id)
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
        settings = config.settings_payload(chain_id, row.get("settings_version_id"))
        engine = Engine(
            chain,
            lexicon=lexicon_map(chain_id).get,
            drafts=drafts,
            refresh=refresh,
            previous=previous_picks(chain_id, run_id),
            peaks=peak_market_caps(chain_id),
        )
        # The stopwatch spans the whole round, not just the engine: the commentary and the
        # write happen out here, and a phase list that stopped at `selected` is why the log
        # line and the wall clock used to disagree by the better part of a minute.
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
        _set_progress(run_id, progress.pct("commentary", 0, 1), progress.label("commentary"))
        written = commentary(chain, result, run_id)
        clock.mark("commentary")
        _set_progress(run_id, progress.pct("commentary", 1, 1), progress.label("commentary"))

        # 词条库有进有出. The draft stage writes an entry for every judged token because the
        # product block needs one before a token can be ranked; this is the other half of
        # that rule. It runs before the round is stored, so the round's own picks are kept
        # by `keep` rather than by the `selected` rows this round has not written yet. A
        # prune that throws is a round that still published: logged, never raised.
        try:
            pruned = report_draft.prune_lexicon(
                chain_id, keep=[item.ca for item in result.scored],
            )
            if pruned:
                result.notes.append("词条库清理：删除 %d 条从未入选的档案" % pruned)
        except Exception:
            log.exception("round %s could not prune its lexicon", run_id)

        conn = db.connect()
        try:
            conn.execute("BEGIN")
            store_round(conn, result)
            store_reports(conn, result, written)
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
    # After `sent`, and only after: the queue is a row write, so the round is over before the
    # first page is fetched. The reading itself is somebody else thread. A queue that throws
    # is a round that still published, so it is logged, not raised.
    try:
        lined = research.queue(chain, result)
        if lined:
            log.info("round %s queued %d entries for research", run_id, len(lined))
    except Exception:
        log.exception("round %s could not queue its research", run_id)
    return _row(run_id)
