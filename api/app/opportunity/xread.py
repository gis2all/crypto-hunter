"""X-only: the query list, the slice plan, and the subprocess that answers one query.

The module read pages as well until 2026-10-18, when the user narrowed it to one source. What is
left is the source that can actually *find* things: a query, not an account.

Three things live here, and they are here rather than in the shared engine for one reason: this
lane owns its own X budget.

- **The query list.** A watchlist row of kind `query` carries a raw X query (with `from:`, quoted
  phrases, `-is:retweet` and so on) plus the window it is asked over.
- **The slice plan.** One X request answers at most forty posts. A query asked over a day is
  therefore asked in slices - `since_time:` / `until_time:` pairs walked back from now - so the
  cap is a slice length rather than a ceiling on what the query can find. The slice length is a
  real choice: too long and a busy phrase is truncated at forty, too short and one query eats the
  whole scan's budget (see `opportunity/README.md` · 切片与预算).
- **One subprocess per query**, with the session in its environment and a cancel flag polled
  every 100ms. The screening engine's `search` is not reused: it is keyed to a token address and
  would return nothing for a phrase, which is exactly the bug this file replaces.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from ..search.engines.xsearch import XEngine, rows_of
from ..search import x_sessions
from ..store import db

log = logging.getLogger("crypto-hunter.opportunity")

#: Seconds between two X calls - the module's own lane, because X is reached through a
#: subprocess and never through `http.get`, so the shared pacer has no entry for it.
#:
#: Measured 2026-10-18: at 2.5s a scan of forty reads started drawing 429s around the thirteenth
#: consecutive call, and the CLI's own retry (5.7s, then 11.4s, then 21.8s) turned each of those
#: into forty seconds of waiting - so the slower interval is the faster scan.
X_INTERVAL = 5.0

#: One request's ceiling, and the CLI's own default.
QUERY_MAX = 40

#: How long one call may take before it is killed. A sliced query is a small window by
#: construction, so a call that hangs is a call that is not coming back.
QUERY_TIMEOUT = 90.0

#: The default window and slice for a query row, in hours. 24/4 is six calls per query: a day of
#: a phrase is asked in four-hour pieces, which keeps a busy phrase under the forty-post ceiling
#: while leaving room in a scan for several queries.
WINDOW_HOURS = 24
SLICE_HOURS = 4

#: The hard cap on how many slices one query may become. A row that asked for a 20-day window in
#: hourly slices would be 480 calls - which is what the reference implementation's own presets
#: do (measured: `days<=20` with the default 60-minute slice expands to exactly 480 queries), and
#: it is the difference between a scan and a rate-limit incident.
MAX_SLICES = 12

_LOCK = threading.Lock()
_NEXT_AT = [0.0]


def throttle() -> None:
    """Kept as the tests' seam; the spacing now happens at session hand-out."""
    from . import settings

    x_sessions.pool().set_interval(float(settings.value("x_interval", X_INTERVAL)))


def cli() -> str:
    """The client this lane runs: the same one the round uses, resolved the same way."""
    return XEngine().cli


def credentials() -> tuple[str, str]:
    """The borrowed session. A refusal here names what is missing rather than answering empty."""
    engine = XEngine()
    return engine.credentials()


def plan_slices(window_hours: int, slice_hours: int, *, now: datetime | None = None,
                max_slices: int = MAX_SLICES) -> list[tuple[int, int]]:
    """The `(since, until)` pairs one query is asked over, newest first.

    Unix seconds because that is what the search operators take. The walk starts at now and goes
    back, so the newest slice is asked first: if the budget or the rate limit cuts the plan short,
    what was read is the part that matters most.
    """
    window = max(1, int(window_hours or WINDOW_HOURS))
    step = max(1, int(slice_hours or SLICE_HOURS))
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(hours=window)
    out: list[tuple[int, int]] = []
    cursor = end
    while cursor > start and len(out) < max(1, int(max_slices)):
        piece = max(start, cursor - timedelta(hours=step))
        out.append((int(piece.timestamp()), int(cursor.timestamp())))
        cursor = piece
    return out


def slice_query(query: str, since: int, until: int) -> str:
    """One query, bounded to one slice. The operators are X's own."""
    return "%s since_time:%d until_time:%d" % (str(query or "").strip(), since, until)


def run(query: str, *, limit: int = QUERY_MAX, cancel: threading.Event | None = None,
        timeout: float = QUERY_TIMEOUT, runner=None, session=None) -> list:
    """One search request, as `TweetRow`s. Raises on a bad exit.

    `runner` is the seam the tests use: it takes the argv and returns the payload the client would
    have printed (already parsed). The normalisation happens **here**, on both paths, because the
    bug this replaces was exactly a path that skipped it - the query lane returned the client's
    raw dicts while `scan.post_row` reads attributes off a `TweetRow`, so every row of every query
    was dropped as empty: the first X-only scan spent 36 slice calls and stored nothing
    (measured 2026-10-18).

    The real path polls the cancel flag every 100ms and kills the child, because a scan a reader
    asked to stop must not keep spending the session.
    """
    argv = [cli(), "search", query, "-t", "Latest", "--max", str(int(limit)), "--json"]
    started = time.monotonic()
    if runner is not None:
        payload = runner(argv)
        label = "runner"
    else:
        text, label = _stdout(argv, cancel=cancel, timeout=timeout, session=session)
        payload = json.loads(text or "[]")
    raw = payload.get("data") if isinstance(payload, dict) else payload
    # The client's rows as `TweetRow`, through the same normaliser the screening engine uses.
    rows = rows_of(list(raw or []))
    # One line per request, so a round's lanes can be read off the log: which session spent it,
    # what was asked, and how long it took. INFO rather than DEBUG: this is the line an operator
    # opens the log for, and the file above keeps it only while the level allows.
    log.info("x: %s search %r -> %d rows in %dms", label, _brief(query), len(rows),
             (time.monotonic() - started) * 1000)
    return rows


def _brief(query: str, width: int = 70) -> str:
    """The query without the slice stamps, short enough for one log line."""
    text = " ".join(str(query or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _stdout(argv: list[str], *, cancel: threading.Event | None, timeout: float,
            session=None) -> tuple[str, str]:
    """One subprocess, as the text the client printed, and the session that carried it."""
    pool = x_sessions.pool()
    own = session is None
    current = session or pool.acquire(priority=1)
    token, ct0 = current.token, current.ct0
    env = {**os.environ, "TWITTER_AUTH_TOKEN": token, "TWITTER_CT0": ct0,
           "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    marked = False
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", env=env,
                                   **creation)
        started = time.monotonic()
        while True:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.communicate()
                raise RuntimeError("scan cancelled")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if (time.monotonic() - started) >= timeout:
                    process.kill()
                    stdout, stderr = process.communicate()
                    pool.failed(current)
                    marked = True
                    raise RuntimeError("x cli timed out after %ss" % int(timeout))
        if process.returncode != 0:
            detail = " ".join((stderr or "").split()[-24:])
            kind = _failure_kind(detail)
            if kind == "rate":
                pool.rate_limited(current)
            elif kind == "dead":
                pool.dead(current)
            else:
                pool.failed(current)
            marked = True
            raise RuntimeError("x cli rc=%s: %s" % (process.returncode, detail))
        pool.ok(current)
        marked = True
        return stdout, current.label
    finally:
        if not marked and (cancel is None or not cancel.is_set()):
            pool.failed(current)
        if own:
            pool.release(current)


def _failure_kind(detail: str) -> str:
    """429 rests a session, 401 retires it, everything else is a plain failure."""
    text = str(detail or "").lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate"
    if "401" in text or "unauthorized" in text or "could not authenticate" in text \
            or "cookie" in text:
        return "dead"
    return "other"


def search_sliced(query: str, *, window_hours: int | None = None,
                  slice_hours: int | None = None, limit: int = QUERY_MAX,
                  cancel: threading.Event | None = None, runner=None) -> dict:
    """One query over its whole window, as rows plus what the reading cost.

    A slice that fails is retried once, then counted - never an exception: a rate-limited slice
    must not throw away the slices that already answered, which is the same rule the source loop
    follows one level up. The pool has already marked the session that failed, so the retry asks
    for another lane; that is what makes a 429 cost one call instead of a whole time window.
    """
    from . import settings

    plan = plan_slices(window_hours or settings.value("window_hours", WINDOW_HOURS),
                       slice_hours or settings.value("slice_hours", SLICE_HOURS))
    rows: list = []
    failed: list[str] = []
    calls = 0
    for since, until in plan:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("scan cancelled")
        text = slice_query(query, since, until)
        for attempt in (1, 2):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("scan cancelled")
            calls += 1
            try:
                rows.extend(run(text, limit=limit, cancel=cancel, runner=runner))
                break
            except Exception as exc:
                if cancel is not None and cancel.is_set():
                    raise
                if attempt == 1:
                    log.info("opportunity: slice %s..%s of %r failed (%r); retrying once "
                             "on another session", since, until, query, exc)
                    continue
                failed.append(str(exc))
                log.info("opportunity: slice %s..%s of %r did not answer: %r",
                         since, until, query, exc)
    # `slices` is the calls this query actually spent, retries included: it is the number the
    # round's budget is charged for.
    return {"rows": rows, "slices": calls, "failed": failed}


def query_row(source: dict) -> tuple[str, int, int, int]:
    """The four numbers a query row carries, with the defaults filled in."""
    return (str(source.get("target") or "").strip(),
            int(source.get("window_hours") or WINDOW_HOURS),
            int(source.get("slice_hours") or SLICE_HOURS),
            int(source.get("max_results") or QUERY_MAX))


def touched(target: str) -> None:
    db.opportunity_source_touched(target)
