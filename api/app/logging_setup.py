"""Our loggers, on stderr, at INFO.

`uvicorn.run(log_level="info")` only configures uvicorn's own loggers: it leaves the root
logger without a handler. A package logger that propagates to a handler-less root is
answered by `logging.lastResort`, which writes WARNING and above - so every `log.info` in
this codebase was being discarded, including the round's phase timings and its per-host
request tallies. The instrumentation existed and nobody could read it.

That is worth a module rather than a line: the question "why did that round take twenty
minutes" is answered by these lines and by nothing else. The database keeps what a round
decided, never how long each part took.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from pathlib import Path

#: Everything this project logs lives under one namespace, so one handler covers it and
#: uvicorn's own records keep going where they went before.
NAMESPACE = "crypto-hunter"

#: Where `scripts/serve-standalone.ps1` sends this process's stderr. The endpoint below reads it
#: so a reader can open one URL instead of hunting for the redirect; nothing else writes here.
LOG_DIR = Path(__file__).resolve().parents[2] / "log"
LOG_NAME = "api-run.err.log"


def log_path() -> Path:
    return LOG_DIR / LOG_NAME


class RingHandler(logging.Handler):
    """The last few hundred records, numbered, so a reader can ask for what it has not seen.

    The file is for a shell and dies with the process; this is for the page - structured, level
    and module filterable, and small enough to poll.
    """

    def __init__(self, size: int = 500) -> None:
        super().__init__()
        self._records: collections.deque = collections.deque(maxlen=size)
        self._next = 1
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        with self._lock:
            self._records.append((self._next, {
                "id": self._next,
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
                      + (",%03d" % int(record.msecs)),
                "level": record.levelname,
                "name": record.name,
                "message": message,
            }))
            self._next += 1

    def recent(self, since: int = 0, limit: int = 100, level: int | None = None,
               name: str = "") -> list[dict]:
        with self._lock:
            items = [item for ident, item in self._records if ident > since]
        if level is not None:
            items = [item for item in items
                     if logging.getLevelName(item["level"]) >= level]
        if name:
            items = [item for item in items if item["name"].startswith(name)]
        return items[-limit:] if limit > 0 else items


RING = RingHandler()


def setup(level: int | None = None) -> logging.Logger:
    """Attach one stderr handler to the `crypto-hunter` namespace. Idempotent."""
    logger = logging.getLogger(NAMESPACE)
    logger.setLevel(level if level is not None else _level())
    if RING not in logger.handlers:
        logger.addHandler(RING)
    if not any(getattr(h, "_ch_namespace", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._ch_namespace = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    # Not propagated: the root logger has no handler here, and uvicorn's config forbids
    # nothing - letting it through would double-print the moment someone adds one.
    logger.propagate = False
    return logger


def _level() -> int:
    name = (os.environ.get("CH_LOG_LEVEL") or "INFO").upper()
    return getattr(logging, name, logging.INFO)

