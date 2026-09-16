"""The chain registry, as the page reads it.

Rows are written by `chainverify` (registries + official docs) and read here. The cache keeps a
page read from doing one SQLite query per card; `reset()` is called after every sync.
"""

from __future__ import annotations

import time

from ..store import db

TTL_SECONDS = 60.0
_cache: tuple[float, dict[str, dict]] | None = None


def reset() -> None:
    global _cache
    _cache = None


def registry() -> dict[str, dict]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < TTL_SECONDS:
        return _cache[1]
    try:
        rows = db.chain_registry_all()
    except Exception:
        rows = {}
    _cache = (now, rows)
    return rows


def meta(name: object) -> dict | None:
    return registry().get(str(name or "").strip().lower())
