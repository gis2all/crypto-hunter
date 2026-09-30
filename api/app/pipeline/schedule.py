"""The round ticker: one worker, one round at a time.

A round starts two ways. The operator presses 运行, which POSTs a run and hands the id to
`submit`; or the cron in the chain's own settings comes round, which the ticker checks
every 30 seconds. Both go through the same single-threaded executor, so a manual round
and a scheduled one never overlap - a round that runs long delays the next, it does not
race it.

Idempotence belongs to the database, not to the ticker: `runs.run_key` is unique per
(chain, slot), so a tick that fires twice, or a process restarted inside the same minute,
creates one round and leaves the rest alone.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from . import run as pipeline_run

log = logging.getLogger("crypto-hunter.schedule")

#: How often the ticker looks at the clock. Anything under a minute only costs a lookup:
#: a slot that has already been claimed is skipped.
TICK_SECONDS = 30

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_thread: threading.Thread | None = None
_stopping = threading.Event()

#: The rounds this process is reading right now: run id -> (chain id, set of settings).
#:
#: Liveness is the executor's to know, and a row in the table is not evidence of it. It used
#: to be asked of the table - "any run not in a terminal state" - which is a question with
#: no true answer: a process that dies mid-round leaves a non-terminal row behind for good,
#: and the archive still carries rows from the state machine before this one that no worker
#: will ever move on. Measured 2026-09-30: both chains read as busy, and no scheduled round
#: had started since the check went in the evening before. Production is one process on one
#: port (see `main.py`), so this process's own rounds are the whole truth.
_live: dict[str, tuple[str, str | None]] = {}


def executor() -> ThreadPoolExecutor:
    """The round worker. One thread: every source paces itself per host anyway, and a
    second round in parallel would only make the first one slower."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round")
    return _executor


def _forget(run_id: str) -> None:
    with _lock:
        _live.pop(run_id, None)


def busy_chains() -> set[str]:
    """The chains with a round in flight. A queued one counts: it is about to read.

    A chain's sets of settings pull from the same sources, at the same rate limits, off the
    same node, so the ticker starts one of them at a time. This is what it asks to know
    which chain to leave alone this tick.
    """
    with _lock:
        return {chain_id for chain_id, _config_id in _live.values()}


def live_configs(chain_id: str) -> list[str]:
    """The chain's sets of settings that have a round in flight right now.

    Asked before a set of settings is deleted: a round already reading is a run row that
    would be deleted out from under the worker that owns it. Only this process reads, so
    only this process's rounds can be in the way.
    """
    with _lock:
        return sorted({config_id for owner, config_id in _live.values()
                       if owner == chain_id and config_id})


def submit(chain_id: str, run_id: str, config_id: str | None = None) -> Future:
    """Queue one round. Returns the future so a caller can wait on it.

    The round is marked live before it is queued, not after: the worker starts the moment
    it is submitted, and a tick landing in between has to see it.
    """
    pool = executor()
    with _lock:
        _live[run_id] = (chain_id, config_id)
    try:
        future = pool.submit(_work, chain_id, run_id, config_id)
    except BaseException:
        _forget(run_id)
        raise
    future.add_done_callback(lambda _future: _forget(run_id))
    return future


def _work(chain_id: str, run_id: str, config_id: str | None = None) -> dict:
    """Run one round, and make a failure the caller cannot see visible.

    A future nobody inspects swallows its own exception, so a round that broke before its
    first state transition would sit at `pending` forever and read like one still working.
    The round's own state is where that has to show up.
    """
    try:
        return pipeline_run.run_chain(chain_id, run_id, config_id)
    except Exception as exc:
        log.exception("round %s on %s failed to start", run_id, chain_id)
        try:
            pipeline_run.fail_run(run_id, repr(exc)[:500])
        except Exception:  # pragma: no cover - nothing left to try
            log.exception("could not record the failure of round %s", run_id)
        raise


def parse_slots(cron: str) -> tuple[set[int], set[int]] | None:
    """The (minutes, hours) of an `m h * * *` expression, or None when it is another shape.

    Only the subset the settings page itself generates is understood: `*`, a list, and
    `start-end/step`. Anything else returns None and the chain simply has no scheduled
    rounds - a cron half-understood is worse than no cron, because it fires at times
    nobody asked for.
    """
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        return None
    minutes = _field(fields[0], 60)
    hours = _field(fields[1], 24)
    if minutes is None or hours is None:
        return None
    return minutes, hours


def _field(field: str, limit: int) -> set[int] | None:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) <= 0:
                return None
            step = int(raw_step)
        if part == "*":
            start, end = 0, limit - 1
        elif "-" in part:
            low, _, high = part.partition("-")
            if not (low.isdigit() and high.isdigit()):
                return None
            start, end = int(low), int(high)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None
        if start > end or end >= limit:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def due_slot(chain: dict, now: datetime | None = None) -> str | None:
    """The slot `now` stands in, or None. Slots are hour-aligned, so a round's run_key
    says which slot it belongs to and not which second the ticker noticed it."""
    now = now or datetime.now().astimezone()
    schedule = chain.get("schedule") or {}
    slots = parse_slots(str(schedule.get("cron") or ""))
    if not slots:
        return None
    minutes, hours = slots
    local = now.astimezone(ZoneInfo(str(schedule.get("timezone") or "UTC")))
    if local.minute not in minutes or local.hour not in hours:
        return None
    return local.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def tick(now: datetime | None = None) -> list[str]:
    """Start the rounds that are due. Returns the run ids it started.

    One round per set of settings per slot, and at most one set of settings per chain:
    `enabled_configs` hands back one per chain, and the `busy` set below is the second
    line - a chain already reading waits for the next tick instead of being started twice.
    They pull from the same sources at the same rate limits off the same node, so two at
    once would only make the first one slower.
    """
    started: list[str] = []
    busy = busy_chains()
    for chain in config.enabled_configs():
        if chain["id"] in busy:
            continue
        slot = due_slot(chain, now)
        if slot is None:
            continue
        row = pipeline_run.scheduled_run(chain["id"], chain["config_id"], slot)
        if row is None:
            continue
        submit(chain["id"], row["id"], chain["config_id"])
        busy.add(chain["id"])
        started.append(row["id"])
        log.info("round %s due for %s:%s at %s", row["id"], chain["id"],
                 chain["config_id"], slot)
    return started


def recover() -> list[str]:
    """Take on the rounds the process before this one left behind.

    Two kinds are left behind and they want opposite answers. A round still at `pending`
    was never read: the ticker looks at the clock rather than the table, so that slot is
    never claimed again and the round would wait forever for a worker nobody will send.
    Re-submitting is safe - `run_key` is unique per slot, so the row cannot be doubled, and
    a round that cannot start a second time is failed by `_work` rather than left looking
    like it is still waiting. A round at any other non-terminal status *was* being read and
    stopped being read; after this restart nothing owns it, so it is closed out instead of
    being counted up on the page for the rest of the week.

    Returns the run ids it started again.
    """
    taken: list[str] = []
    for row in pipeline_run.pending_runs():
        submit(row["chain_id"], row["id"], row["config_id"])
        taken.append(row["id"])
        log.warning("round %s on %s was left pending by a previous process: started again",
                    row["id"], row["chain_id"])
    for row in pipeline_run.abandoned_runs():
        closed = pipeline_run.close_abandoned(row["id"])
        log.warning("round %s on %s was left at %s by a previous process: closed as %s",
                    row["id"], row["chain_id"], row["status"], closed)
    return taken


def _loop() -> None:
    while not _stopping.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:  # a broken tick must not take the ticker down
            log.exception("tick failed")


def start() -> None:
    """Start the ticker. Idempotent, so a second call does not leave two of them behind.

    Recovery runs here rather than in the loop: it is a startup problem (the rounds the
    process before us left behind), not a recurring one.
    """
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stopping.clear()
        _thread = threading.Thread(target=_loop, name="schedule", daemon=True)
        _thread.start()
    # Outside the lock: `recover` submits, and `submit` takes the same lock to build the
    # executor, which `threading.Lock` does not allow twice from one thread.
    recover()


def stop() -> None:
    global _executor, _thread
    _stopping.set()
    thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout=5)
    with _lock:
        pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False)
