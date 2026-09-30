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
#: 7 gave a chain more than one set of settings: runs and settings versions carry a
#: config_id, and the two lexicon tables are keyed by it so one CA can hold one entry per
#: set of settings instead of one entry for the chain.
#: 8 added `favorites`: cards the reader kept. A kept card is a copy of the assembled card
#: plus where it came from, keyed by (chain_id, ca) - the key carries no config_id, because a
#: card can come from a search on a chain this app does not screen, and nothing cascades
#: into it. Which set of settings the round ran under is kept as a value, not as a key.
SCHEMA_VERSION = 8


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
                  ("runs", "config_id", "TEXT NOT NULL DEFAULT 'default'"),
                  ("settings_versions", "config_id", "TEXT NOT NULL DEFAULT 'default'"),
                  ("lexicon", "run_id", "TEXT"),
                  ("lexicon", "config_id", "TEXT NOT NULL DEFAULT 'default'"),
                  ("lexicon_sources", "config_id", "TEXT NOT NULL DEFAULT 'default'"),
                  ("lexicon", "f_completeness", "REAL"),
                  ("lexicon", "f_substance", "REAL"),
                  ("lexicon", "f_team", "REAL"),
                  ("lexicon", "f_narrative", "REAL"),
                  ("lexicon", "f_community", "REAL"),
                  ("lexicon", "substance_text", "TEXT"),
                  ("lexicon", "team_text", "TEXT"),
                  ("tokens", "peak_market_cap_usd", "REAL"),
                  ("tokens", "peak_market_cap_at", "TEXT"),
                  ("favorites", "config_id", "TEXT"))

#: Columns an earlier shape carried. Nothing reads them, a fresh database never creates
#: them, and leaving them behind on an old one is a table that lies about what the code
#: uses - so they are dropped once, on the upgrade that removed their readers. (Most are
#: the previous lexicon's; `favorites.caliber` is a string the shelf no longer prints.)
#: A SQLite too old to drop a column simply keeps them; nothing depends on either way.
_DROPPED_COLUMNS = (("lexicon", "baseline_mcap"),
                    ("favorites", "caliber"),
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
    # After the column adds and before anything reads a lexicon: a table keyed by CA alone
    # cannot hold two sets of settings' judgements about the same token.
    _rekey_lexicon_for_configs(conn)
    _rekey_lexicon_sources_for_configs(conn)
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


def _rekey_lexicon_for_configs(conn: sqlite3.Connection) -> int:
    """Rewrite the lexicon so one CA can carry one entry per set of settings.

    The table was keyed by CA, which is the right key for a chain with one set of settings
    and the wrong one the moment a chain has two: the second set's round writes the same
    CA the first set's card was written from, and the row it lands on is the first set's.
    SQLite cannot move a primary key, so the table is rebuilt and the existing rows are
    carried over under `default` - the one set of settings every row on disk belongs to.

    Idempotent by reading the key back: a table that already carries `config_id` inside its
    primary key is left alone, which is also what makes a fresh database a no-op here.
    """
    info = {row["name"]: row["pk"] for row in conn.execute("PRAGMA table_info(lexicon)")}
    if not info.get("ca"):
        return 0          # no such table yet: `schema.sql` just made the right one
    if info.get("config_id"):
        return 0          # already keyed by it, so the rows are already told apart
    before = conn.execute("SELECT COUNT(*) AS n FROM lexicon").fetchone()["n"]
    conn.executescript("""
        DROP TABLE IF EXISTS lexicon_rekeyed;
        CREATE TABLE lexicon_rekeyed (
          ca             TEXT NOT NULL,
          chain_id       TEXT NOT NULL DEFAULT 'arc',
          config_id      TEXT NOT NULL DEFAULT 'default',
          run_id         TEXT,
          symbol         TEXT,
          tag            TEXT,
          f_completeness REAL,
          f_substance    REAL,
          f_team         REAL,
          f_narrative    REAL,
          f_community    REAL,
          social         TEXT,
          concl          TEXT,
          narr_text      TEXT,
          prod_text      TEXT,
          substance_text TEXT,
          team_text      TEXT,
          watch          TEXT,
          scenario       TEXT,
          evidence_json  TEXT,
          updated_at     TEXT NOT NULL,
          PRIMARY KEY (chain_id, config_id, ca)
        );
        INSERT OR REPLACE INTO lexicon_rekeyed
          (ca, chain_id, config_id, run_id, symbol, tag, f_completeness, f_substance,
           f_team, f_narrative, f_community, social, concl, narr_text, prod_text,
           substance_text, team_text, watch, scenario, evidence_json, updated_at)
        SELECT ca, chain_id, COALESCE(config_id, 'default'), run_id, symbol, tag,
               f_completeness, f_substance, f_team, f_narrative, f_community, social,
               concl, narr_text, prod_text, substance_text, team_text, watch, scenario,
               evidence_json, updated_at
          FROM lexicon;
        DROP TABLE lexicon;
        ALTER TABLE lexicon_rekeyed RENAME TO lexicon;
        CREATE INDEX IF NOT EXISTS idx_lexicon_chain ON lexicon(chain_id, symbol);
    """)
    return before


def _rekey_lexicon_sources_for_configs(conn: sqlite3.Connection) -> int:
    """The same rekey for the pages behind a card: (chain, set of settings, CA, url).

    A card's claim points at the page it was read from, and the second set of settings on
    a chain reads the same pages about the same token. Keyed without the set of settings,
    the first set's cleanup pass would delete a row the second set's card still links to.
    """
    info = {row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(lexicon_sources)")}
    if not info.get("ca"):
        return 0          # no such table yet: `schema.sql` just made the right one
    if info.get("config_id"):
        return 0          # already keyed by it, so the rows are already told apart
    before = conn.execute("SELECT COUNT(*) AS n FROM lexicon_sources").fetchone()["n"]
    conn.executescript("""
        DROP TABLE IF EXISTS lexicon_sources_rekeyed;
        CREATE TABLE lexicon_sources_rekeyed (
          ca         TEXT NOT NULL,
          chain_id   TEXT NOT NULL,
          config_id  TEXT NOT NULL DEFAULT 'default',
          kind       TEXT NOT NULL,
          url        TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          note       TEXT,
          PRIMARY KEY (chain_id, config_id, ca, url)
        );
        INSERT OR REPLACE INTO lexicon_sources_rekeyed
          (ca, chain_id, config_id, kind, url, fetched_at, note)
        SELECT ca, chain_id, COALESCE(config_id, 'default'), kind, url, fetched_at, note
          FROM lexicon_sources;
        DROP TABLE lexicon_sources;
        ALTER TABLE lexicon_sources_rekeyed RENAME TO lexicon_sources;
    """)
    return before


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


def library_run_ids(chain_id: str, config_id: str) -> list[str]:
    """The rounds one set of settings has run, newest first.

    The id of a round is also the name of its raw responses under `data/raw/`, which is why
    dropping a report library reads them here rather than counting rows and guessing.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id FROM runs WHERE chain_id = ? AND config_id = ?"
            " ORDER BY scheduled_at DESC, id DESC",
            (chain_id, config_id),
        ).fetchall()
        return [row["id"] for row in rows]
    finally:
        conn.close()


def _library_size(conn: sqlite3.Connection, chain_id: str, config_id: str) -> dict[str, int]:
    """The four counts, on a connection the caller already holds."""

    def one(sql: str, *args: object) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "runs": one("SELECT COUNT(*) FROM runs WHERE chain_id = ? AND config_id = ?",
                    chain_id, config_id),
        "reports": one("SELECT COUNT(*) FROM reports r JOIN runs u ON u.id = r.run_id"
                       " WHERE u.chain_id = ? AND u.config_id = ?", chain_id, config_id),
        "entries": one("SELECT COUNT(*) FROM lexicon WHERE chain_id = ? AND config_id = ?",
                       chain_id, config_id),
        "versions": one("SELECT COUNT(*) FROM settings_versions"
                        " WHERE chain_id = ? AND config_id = ?", chain_id, config_id),
    }


def library_size(chain_id: str, config_id: str) -> dict[str, int]:
    """What one set of settings holds: rounds, cards, entries and saved versions.

    Read before the delete, by the page that asks a human to confirm one: "3 轮 / 24 张卡 /
    18 条词条" is the difference between a dialog someone reads and a dialog someone clicks
    through.
    """
    conn = connect()
    try:
        return _library_size(conn, chain_id, config_id)
    finally:
        conn.close()


def forget_config(chain_id: str, config_id: str) -> dict[str, int]:
    """Drop one set of settings' rows. Returns the same counts `library_size` reports.

    Everything that hangs off a round goes with it - `run_items`, `snapshots`, `run_summary`
    and `reports` all cascade from `runs` - plus the two lexicon tables and the saved
    settings versions, which belong to a set of settings rather than to a round.

    Two tables are left alone on purpose. `tokens` is keyed by CA and holds chain-level
    facts - the highest market cap any source has reported - and that is the same fact
    whichever set of settings is looking at it. `llm_calls` is the ledger of what a round
    spent; it is the one record that should outlive the thing it paid for.

    One transaction, because half a report library is not a state anything can read: the
    runs gone and the entries still there is a page that lists verdicts for rounds it can no
    longer show.
    """
    conn = connect()
    try:
        sizes = _library_size(conn, chain_id, config_id)
        conn.execute("BEGIN")
        conn.execute("DELETE FROM runs WHERE chain_id = ? AND config_id = ?",
                     (chain_id, config_id))
        conn.execute("DELETE FROM lexicon_sources WHERE chain_id = ? AND config_id = ?",
                     (chain_id, config_id))
        conn.execute("DELETE FROM lexicon WHERE chain_id = ? AND config_id = ?",
                     (chain_id, config_id))
        conn.execute("DELETE FROM settings_versions WHERE chain_id = ? AND config_id = ?",
                     (chain_id, config_id))
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return sizes



def favorites() -> list[dict]:
    """Every kept card, newest first, payload included.

    The payload ships with the row because the shelf draws the card itself: a second request
    per card would be the page stitching one screen out of many calls, which is the thing
    this API does not do. A shelf is tens of rows.

    Ordered by when the reader kept the thing rather than by when the payload last changed:
    a round that writes a newer card for a token somebody kept replaces the payload in place
    (`favorite_refresh`), and a shelf that re-sorted itself every round would move rows the
    reader never touched.
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM favorites ORDER BY saved_at DESC, ca DESC").fetchall()]
    finally:
        conn.close()


def favorite(chain_id: str, ca: str) -> dict | None:
    """One kept card, or None. The chain is part of the key: two chains can hold one CA."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM favorites WHERE chain_id = ? AND ca = ?",
                           (chain_id, ca)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def favorite_put(row: dict) -> dict:
    """Keep a card, or replace the one already kept for that token.

    Replacing is the whole of the update path. `saved_at` is deliberately left where it was
    on an update - see `favorites` - and `updated_at` is what says the payload changed.
    """
    now = now_iso()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO favorites (chain_id, ca, symbol, origin, origin_run_id, origin_label,"
            " score, rank, config_id, payload_json, saved_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, ca) DO UPDATE SET"
            " symbol = excluded.symbol, origin = excluded.origin,"
            " origin_run_id = excluded.origin_run_id, origin_label = excluded.origin_label,"
            " score = excluded.score, rank = excluded.rank, config_id = excluded.config_id,"
            " payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            (row["chain_id"], row["ca"], row.get("symbol"), row.get("origin") or "round",
             row.get("origin_run_id"), row.get("origin_label") or "", row.get("score"),
             row.get("rank"), row.get("config_id"), row["payload_json"], now, now),
        )
        saved = conn.execute("SELECT * FROM favorites WHERE chain_id = ? AND ca = ?",
                             (row["chain_id"], row["ca"])).fetchone()
        return dict(saved)
    finally:
        conn.close()


def favorite_refresh(rows: list[dict]) -> int:
    """Put newer cards in place of the kept ones they supersede; how many rows moved.

    This is the write a round makes to the shelf at its end, and it only ever *replaces*: a
    card for a token nobody kept belongs to the round, not to the shelf, and adding it would
    be the round deciding what the reader is following. Two guards keep a refresh from going
    backwards or sideways - the row has to be under the same set of settings (a card read
    under other settings is a different card, not a newer one), and the incoming card has to
    be newer than the one it replaces, so a round re-run out of order cannot put an older
    card back on the shelf.

    `saved_at` is left where it is: that stamp is when the reader kept the token, and this
    write is the round's. It is also why the shelf does not re-sort itself here.
    """
    if not rows:
        return 0
    now = now_iso()
    moved = 0
    conn = connect()
    try:
        for row in rows:
            label = row.get("origin_label") or ""
            cursor = conn.execute(
                "UPDATE favorites SET symbol = ?, origin = ?, origin_run_id = ?,"
                " origin_label = ?, score = ?, rank = ?, payload_json = ?, updated_at = ?"
                " WHERE chain_id = ? AND ca = ? AND config_id IS ?"
                " AND coalesce(origin_label, '') < ?",
                (row.get("symbol"), row.get("origin") or "round", row.get("origin_run_id"),
                 label, row.get("score"), row.get("rank"), row["payload_json"], now,
                 row["chain_id"], row["ca"], row.get("config_id"), label),
            )
            moved += cursor.rowcount
        return moved
    finally:
        conn.close()


def favorite_remove(chain_id: str, ca: str) -> bool:
    """Forget one kept card. Returns whether there was one: a second click is not an error."""
    conn = connect()
    try:
        cursor = conn.execute("DELETE FROM favorites WHERE chain_id = ? AND ca = ?",
                              (chain_id, ca))
        return bool(cursor.rowcount)
    finally:
        conn.close()
