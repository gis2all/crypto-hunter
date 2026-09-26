"""The round ticker: one worker, one round at a time.

A round starts two ways. The operator presses 运行, which POSTs a run and hands the id to
`submit`; or the cron in the chain's own settings comes round, which the ticker checks
every 30 seconds. Both go through the same single-threaded executor, so a manual round
and a scheduled one never overlap - a round that runs long delays the next, it does not
race it.

Idempotence belongs to the database, not to the ticker: `runs.run_key` is unique per
(chain, slot), so a tick that fires twice, or a process restarted inside the same minute,
creates one round and leaves the rest alone.

There is a second worker, for the research pass. It is separate on purpose: research spends
minutes on other people servers, and a round waiting behind it would be a round that got
slow because somebody else website was slow. The ticker hands it work and moves on.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from ..report import research
from . import run as pipeline_run

log = logging.getLogger("crypto-hunter.schedule")

#: How often the ticker looks at the clock. Anything under a minute only costs a lookup:
#: a slot that has already been claimed is skipped.
TICK_SECONDS = 30

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_thread: threading.Thread | None = None
_stopping = threading.Event()

#: The research worker, and the flag that says it is busy. One pass at a time: two passes
#: would double the load on the same hosts for no gain, and the cards inside a pass are
#: already read side by side (see `research.WORKERS`).
_research_lock = threading.Lock()
_research_executor: ThreadPoolExecutor | None = None
#: How many entries one research pass takes. A round publishes five cards and the pass reads
#: them at the same time, so one tick covers a whole round instead of leaving the last cards
#: to wait half a minute for the next one. Still a cap: a pass that never ends is a pass
#: nobody can watch.
RESEARCH_BATCH = 6


def executor() -> ThreadPoolExecutor:
    """The round worker. One thread: every source paces itself per host anyway, and a
    second round in parallel would only make the first one slower."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round")
    return _executor


def submit(chain_id: str, run_id: str) -> Future:
    """Queue one round. Returns the future so a caller can wait on it."""
    return executor().submit(_work, chain_id, run_id)


def _work(chain_id: str, run_id: str) -> dict:
    """Run one round, and make a failure the caller cannot see visible.

    A future nobody inspects swallows its own exception, so a round that broke before its
    first state transition would sit at `pending` forever and read like one still working.
    The round's own state is where that has to show up.
    """
    try:
        return pipeline_run.run_chain(chain_id, run_id)
    except Exception as exc:
        log.exception("round %s on %s failed to start", run_id, chain_id)
        try:
            pipeline_run.fail_run(run_id, repr(exc)[:500])
        except Exception:  # pragma: no cover - nothing left to try
            log.exception("could not record the failure of round %s", run_id)
        raise


def research_executor() -> ThreadPoolExecutor:
    """The research worker. Its own pool so it can never sit in front of a round."""
    global _research_executor
    with _lock:
        if _research_executor is None:
            _research_executor = ThreadPoolExecutor(max_workers=1,
                                                   thread_name_prefix="research")
    return _research_executor


def research_tick() -> bool:
    """Hand the queued entries to the research worker, if it is free.

    Skipped rather than queued when the worker is busy: the lock is the whole scheduler, and
    a pass that is still running will pick up whatever is left on the next tick anyway.
    Returns whether a pass was started.
    """
    if not _research_lock.acquire(blocking=False):
        return False
    try:
        research_executor().submit(_research_work)
    except Exception:  # pragma: no cover - a pool that cannot start work
        _research_lock.release()
        raise
    return True


def _research_work() -> list[dict]:
    """One research pass, then the state transitions it can now justify.

    The round goes to `researched` here rather than in the round: the round thread that
    published is long gone, and this is the only place that knows a pass has finished.
    """
    try:
        done = research.work_once(RESEARCH_BATCH)
        for chain_id in {row.get("chain_id") for row in done if row.get("chain_id")}:
            for run_id in pipeline_run.researched_runs(str(chain_id)):
                pipeline_run.mark_researched(run_id)
                log.info("round %s is researched", run_id)
        return done
    except Exception:  # a broken pass must not take the worker down
        log.exception("research pass failed")
        return []
    finally:
        _research_lock.release()


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
    """Start the rounds that are due. Returns the run ids it started."""
    started: list[str] = []
    for chain in config.enabled_chains():
        slot = due_slot(chain, now)
        if slot is None:
            continue
        row = pipeline_run.scheduled_run(chain["id"], slot)
        if row is None:
            continue
        submit(chain["id"], row["id"])
        started.append(row["id"])
        log.info("round %s due for %s at %s", row["id"], chain["id"], slot)
    return started


def recover() -> list[str]:
    """Start the rounds a previous process left at `pending`.

    The ticker looks at the clock, not at the table: a slot it has already claimed is
    never claimed again, in this process or the next one. So a process that is restarted
    between the tick that created a round and the worker that would have run it leaves a
    round nobody will ever start - and a page that reads "待运行" until someone looks at
    the database. Re-submitting is safe: `run_key` is unique per slot, so the row cannot
    be doubled, and a round that cannot start a second time is failed by `_work` rather
    than left looking like it is still waiting.

    Returns the run ids it took on.
    """
    taken: list[str] = []
    for row in pipeline_run.pending_runs():
        submit(row["chain_id"], row["id"])
        taken.append(row["id"])
        log.warning("round %s on %s was left pending by a previous process: started again",
                    row["id"], row["chain_id"])
    return taken


def _loop() -> None:
    while not _stopping.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:  # a broken tick must not take the ticker down
            log.exception("tick failed")
        try:
            research_tick()
        except Exception:  # same rule for the second worker
            log.exception("research tick failed")


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
    # A claim does not survive the process that made it, so whatever was `running` when this
    # process was not is put back in the line before the ticker starts looking.
    requeued = research.requeue_running()
    if requeued:
        log.warning("%d entries were left mid-research by a previous process: queued again",
                    requeued)


def stop() -> None:
    global _executor, _research_executor, _thread
    _stopping.set()
    thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout=5)
    with _lock:
        pool, _executor = _executor, None
        research_pool, _research_executor = _research_executor, None
    if pool is not None:
        pool.shutdown(wait=False)
    # `wait=False` on the research pool too: a pass in flight is holding the lock, and a
    # shutdown that waited for it would make closing the app as slow as the slowest page.
    if research_pool is not None:
        research_pool.shutdown(wait=False)
