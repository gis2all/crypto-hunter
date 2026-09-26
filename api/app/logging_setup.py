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

import logging
import os

#: Everything this project logs lives under one namespace, so one handler covers it and
#: uvicorn's own records keep going where they went before.
NAMESPACE = "crypto-hunter"


def setup(level: int | None = None) -> logging.Logger:
    """Attach one stderr handler to the `crypto-hunter` namespace. Idempotent."""
    logger = logging.getLogger(NAMESPACE)
    logger.setLevel(level if level is not None else _level())
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

