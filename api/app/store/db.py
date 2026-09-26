"""SQLite connection handling: WAL, foreign keys, schema bootstrap."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
#: 2 added `lexicon.baseline_safety`, with a data migration to fill it for entries that
#: predate the column - see `_backfill_safety_baseline`.
#: 3 added `dexscreener_url` to every snapshot payload. The column-based migrations above
#: cannot see it (it lives inside `metrics_json`), so this one is keyed on the version the
#: database was on - see `_backfill_dex_url`.
SCHEMA_VERSION = 3


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does nothing to a
#: table that already exists, and the database on this machine is the one that proved it.
_ADDED_COLUMNS = (("runs", "progress", "INTEGER NOT NULL DEFAULT 0"),
                  ("runs", "progress_label", "TEXT"),
                  ("lexicon", "baseline_mcap", "REAL"),
                  ("lexicon", "baseline_holders", "INTEGER"),
                  ("lexicon", "baseline_at", "TEXT"),
                  ("lexicon", "baseline_safety", "TEXT"),
                  ("lexicon", "refresh_reason", "TEXT"),
                  ("lexicon", "research_state", "TEXT NOT NULL DEFAULT 'none'"),
                  ("lexicon", "research_note", "TEXT"),
                  ("tokens", "peak_market_cap_usd", "REAL"),
                  ("tokens", "peak_market_cap_at", "TEXT"))


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Apply schema.sql (idempotent), add missing columns, record the schema version."""
    own = conn is None
    conn = conn or connect()
    # Read before the write at the end of this function: a migration that asks which version
    # the database was on has to ask it before the answer is overwritten.
    was = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    fresh: set[tuple[str, str]] = set()
    for table, column, kind in _ADDED_COLUMNS:
        have = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, kind))
            fresh.add((table, column))
    # Only on the round that adds the column: the rows that already exist need the
    # verdict they were written against, or the refresh rule that reads it never fires.
    if ("lexicon", "baseline_safety") in fresh:
        filled = _backfill_safety_baseline(conn)
        log.info("lexicon: stamped %d entries with the safety verdict they were written against", filled)
    if ("tokens", "peak_market_cap_usd") in fresh:
        seeded = _backfill_token_peaks(conn)
        log.info("tokens: seeded %d peak market caps from the snapshot history", seeded)
    if was < 3:
        stamped = _backfill_dex_url(conn)
        log.info("snapshots: wrote a DexScreener page into %d rows that predate the field", stamped)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    if own:
        conn.close()
    return conn


def _backfill_safety_baseline(conn: sqlite3.Connection) -> int:
    """Stamp existing entries with the safety verdict they were written against.

    A comparison against a column nobody ever filled never fires, so the column has to
    arrive with history rather than empty. The verdict is not guessed: it is read back off
    the snapshot nearest that entry's own timestamp, which is the reading the writer had in
    hand while it wrote those words. A row with no snapshot from its own era keeps a blank,
    and a blank never fires - the honest answer when the evidence is gone.

    Imported here rather than at module level: this is the data layer, and the wording of a
    verdict belongs to `core.lexicon`. The import is one-way, so there is no cycle.
    """
    from ..core.lexicon import safety_signature

    rows = conn.execute("SELECT ca, updated_at FROM lexicon").fetchall()
    filled = 0
    for row in rows:
        snap = conn.execute(
            "SELECT safety_json FROM snapshots WHERE ca = ? AND created_at <= ?"
            " ORDER BY created_at DESC LIMIT 1", (row["ca"], row["updated_at"])
        ).fetchone()
        signature = safety_signature(json.loads(snap["safety_json"] or "{}")) if snap else ""
        if not signature:
            continue
        conn.execute("UPDATE lexicon SET baseline_safety = ? WHERE ca = ?",
                     (signature, row["ca"]))
        filled += 1
    return filled


def _backfill_token_peaks(conn: sqlite3.Connection) -> int:
    """Seed 每 CA 的最高市值 from the snapshots that already exist.

    The rule that reads this column compares today against it, so a column that arrived
    empty would take a token own first appearance as its high and never fire. What exists
    to seed it with is the snapshot record: what each round saw while the token was inside
    this chain band. That is a lower bound on the true high and never an invented one.
    Rows with nothing worth reading are left blank, and a blank never fires.

    `json_extract` does the reading inside SQLite rather than parsing every payload in
    Python: the table is the wrong size for a per-row round trip.
    """
    cursor = conn.execute(
        "UPDATE tokens SET peak_market_cap_usd = ("
        "  SELECT MAX(CAST(json_extract(s.metrics_json, '$.market_cap_usd') AS REAL))"
        "  FROM snapshots s JOIN runs r ON r.id = s.run_id"
        "  WHERE s.ca = tokens.ca AND r.chain_id = tokens.chain_id)"
        " WHERE peak_market_cap_usd IS NULL"
    )
    return cursor.rowcount or 0


def _backfill_dex_url(conn: sqlite3.Connection) -> int:
    """Give snapshots taken before the field existed the same page today’s rows carry.

    The URL is a pure function of (chain, CA) - `pipeline.collect` fills it that way for every
    row it writes - so the rows already on disk can be handed the identical value. Without
    it, a round read back from the database shows a footer one tracker short while a round
    run after this change shows three, which is the same round reading two different ways.

    The chain comes from the run each snapshot belongs to rather than from a constant: this
    table holds every chain the app has scanned, and a link built with the wrong chain slug
    lands on a page about nothing. A row with no CA is skipped - `||` over a NULL would write
    a JSON null, and a null is not a page.
    """
    cursor = conn.execute(
        "UPDATE snapshots SET metrics_json = json_set(metrics_json, '$.dexscreener_url',"
        " 'https://dexscreener.com/' || runs.chain_id || '/' || snapshots.ca)"
        " FROM runs WHERE runs.id = snapshots.run_id AND snapshots.ca IS NOT NULL"
        " AND json_extract(snapshots.metrics_json, '$.dexscreener_url') IS NULL"
    )
    return cursor.rowcount or 0
