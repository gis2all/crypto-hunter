"""Stop a round that is already running.

A round is minutes of network the operator is watching, and the only way to stop one used
to be to kill the process - which is the same as never having started it, since nothing is
written until the last transaction. The request therefore travels through this module and
not through the database: an HTTP call cannot reach into a worker thread, so it leaves a
flag behind and the round stops at its own next checkpoint. One process, one flag set, one
round in flight at a time (`pipeline/schedule.py` runs the rounds on a single worker).

Flags are per run id and cleared when the round ends, so no round ever inherits another
one's stop. `raise_if` is called on that worker thread, so what it raises is a
`RoundCancelled` the round reads in its own `except` - never an exception escaping into a
thread pool.
"""

from __future__ import annotations

import threading
from typing import Callable


class RoundCancelled(RuntimeError):
    """The operator asked this round to stop; it stopped where it stood."""


_lock = threading.Lock()
_requested: set[str] = set()


def request(run_id: str) -> None:
    """Ask the round to stop. Returns at once: it stops at its own next checkpoint."""
    with _lock:
        _requested.add(run_id)


def requested(run_id: str | None) -> bool:
    """Whether this round has been asked to stop. `None` is not a round, and never stops."""
    if not run_id:
        return False
    with _lock:
        return run_id in _requested


def clear(run_id: str) -> None:
    """Forget the request. Called once when the round ends, whatever it ended as."""
    with _lock:
        _requested.discard(run_id)


def raise_if(run_id: str | None) -> None:
    """The checkpoint itself: one call per boundary a round can lose minutes in."""
    if requested(run_id):
        raise RoundCancelled("round %s was stopped by the operator" % run_id)


def check(run_id: str | None) -> Callable[[], None]:
    """`raise_if(run_id)` as a callable, for the passes that hand it to a thread pool.

    A pool cannot be asked to stop once it holds its whole queue, so the passes that fill one
    hand it this instead: the check runs on the pool's own thread, before the next task
    starts, and what it raises is what ends the pass. A caller with no round in hand - a CA
    search, a test - passes `None`, which is not a round and never stops.
    """
    return lambda: raise_if(run_id)
