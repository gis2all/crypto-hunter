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
#: 4 replaced the lexicon's two judgement columns with the five off-chain scores, gave it
#: the round it belongs to, and dropped the cross-round refresh bookkeeping: an entry now
#: lives for exactly one round.
#: 5 gave every card block its own text column and dropped the ad-hoc trio (`mech_text`,
#: `pros`, `cons`): `narr_text` / `prod_text` / `substance_text` / `team_text` / `social`
#: are the five 链下调研 blocks, in the order the card prints them, and the five `f_*`
#: judgements beside them are the same five things. 本期点评 retired with them.
#: 6 collapsed the research pass into the round: research now runs before anything is
#: written, so the queue-and-restate bookkeeping (`research_state`, `research_note`) is
#: gone and the lexicon row is written once, already final.
SCHEMA_VERSION = 6


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
                  ("lexicon", "run_id", "TEXT"),
                  ("lexicon", "f_completeness", "REAL"),
                  ("lexicon", "f_substance", "REAL"),
                  ("lexicon", "f_team", "REAL"),
                  ("lexicon", "f_narrative", "REAL"),
                  ("lexicon", "f_community", "REAL"),
                  ("lexicon", "substance_text", "TEXT"),
                  ("lexicon", "team_text", "TEXT"),
                  ("tokens", "peak_market_cap_usd", "REAL"),
                  ("tokens", "peak_market_cap_at", "TEXT"))

#: Columns the previous lexicon carried. Nothing reads them, a fresh database never
#: creates them, and leaving them behind on an old one is a table that lies about what
#: the code uses - so they are dropped once, on the upgrade that removed their readers.
#: A SQLite too old to drop a column simply keeps them; nothing depends on either way.
_DROPPED_COLUMNS = (("lexicon", "baseline_mcap"),
                    ("lexicon", "baseline_holders"),
                    ("lexicon", "baseline_at"),
                    ("lexicon", "baseline_safety"),
                    ("lexicon", "refresh_reason"),
                    ("lexicon", "review_state"),
                    ("lexicon", "narrative"),
                    ("lexicon", "product"),
                    ("lexicon", "mech_text"),
                    ("lexicon", "pros"),
                    ("lexicon", "cons"),
                    ("lexicon", "research_state"),
                    ("lexicon", "research_note"))

#: Indexes over columns that are gone. SQLite refuses to drop a column an index still names
#: ("error in index ... after drop column"), and that refusal is caught below - so without
#: this the column would quietly survive on the one database that had the index. DROP INDEX
#: IF EXISTS is idempotent, so this runs on every start like the rest of the migration.
_DROPPED_INDEXES = ("idx_lexicon_state",)


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
    for name in _DROPPED_INDEXES:
        conn.execute("DROP INDEX IF EXISTS %s" % name)
    for table, column in _DROPPED_COLUMNS:
        have = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if column in have:
            try:
                conn.execute("ALTER TABLE %s DROP COLUMN %s" % (table, column))
            except sqlite3.Error:  # pragma: no cover - old SQLite, nothing depends on it
                pass
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
