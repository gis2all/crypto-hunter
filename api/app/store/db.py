"""SQLite connection handling: WAL, foreign keys, schema bootstrap."""

from __future__ import annotations

import hashlib
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
#: 9 added `favorites.kept`: the row is now either a card the reader collected or a record a
#: CA search left behind. A search writes its card with kept = 0, so a token can be searched
#: and then collected without a second row for it; the shelf reads `kept = 1` only.
#: 10 added 新机会's six tables (watchlist, raw material, entries, steps, scans). They are new
#: tables, so `schema.sql` alone was enough for a database that had never seen them - but not
#: for one that had: the entry table gained `heat` / `heat_basis` a scan later, and that is
#: what `_ADDED_COLUMNS` is for.
#: 11 gave the entry its own `ecosystem` / `source_type`: the page filters by both, and both
#: live on the watchlist row rather than on the entry - so they are copied in at write time and
#: backfilled once for the rows that predate them.
SCHEMA_VERSION = 11


def _backfill_opportunity_sources(conn: sqlite3.Connection) -> int:
    """Copy each entry's ecosystem and source type off the watchlist row it came from.

    The path is three hops and every one of them is a stored fact rather than a guess: an entry
    keeps the post's URL as `source_ref`, a post knows which watchlist target found it, and that
    target knows its own ecosystem and kind. Rows whose material is gone (a source that was
    deleted, a page that was replaced) keep NULL - which the page reads as 未判定, the same as
    anywhere else.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(opportunities)")}
    if not {"source_kind", "source_ref"} <= columns:
        # A table that predates those columns has no entries this could fill, and asking anyway
        # is an error rather than an empty answer.
        return 0
    cursor = conn.execute(
        "UPDATE opportunities SET"
        " ecosystem = (SELECT s.ecosystem FROM opportunity_sources s"
        "              WHERE s.target = (SELECT p.source_target FROM opportunity_posts p"
        "                                WHERE p.url = opportunities.source_ref)),"
        " source_type = (SELECT s.kind FROM opportunity_sources s"
        "                WHERE s.target = (SELECT p.source_target FROM opportunity_posts p"
        "                                  WHERE p.url = opportunities.source_ref))"
        " WHERE source_kind = 'post' AND ecosystem IS NULL AND source_ref IS NOT NULL")
    return cursor.rowcount or 0


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
                  ("favorites", "config_id", "TEXT"),
                  ("favorites", "kept", "INTEGER NOT NULL DEFAULT 1"),
                  #: 新机会的两列：热度与它旁边的原始数字。加在这里而不是只写进 schema.sql，
                  #: 是因为线上那份库的表是在这两列存在之前建的 —— `CREATE TABLE IF NOT EXISTS`
                  #: 对已存在的表什么也不做，第一次真跑就是这样在抽取阶段撞上的。
                  ("opportunities", "heat", "REAL"),
                  ("opportunities", "heat_basis", "TEXT"),
                  ("opportunities", "ecosystem", "TEXT"),
                  ("opportunities", "user_state", "TEXT NOT NULL DEFAULT 'active'"),
                  ("opportunities", "user_state_at", "TEXT"),
                  ("opportunity_sources", "window_hours", "INTEGER NOT NULL DEFAULT 24"),
                  ("opportunity_sources", "slice_hours", "INTEGER NOT NULL DEFAULT 4"),
                  ("opportunity_sources", "max_results", "INTEGER NOT NULL DEFAULT 40"),
                  ("opportunity_posts", "score", "REAL"),
                  ("opportunity_posts", "rules_json", "TEXT"),
                  ("opportunity_posts", "urls_json", "TEXT"),
                  ("opportunity_posts", "project_name", "TEXT"),
                  ("opportunity_posts", "trace_level", "TEXT"),
                  ("opportunity_posts", "trace_json", "TEXT"),
                  ("opportunity_posts", "traced_at", "TEXT"),
                  ("opportunities", "source_type", "TEXT"),
                  ("opportunity_sources", "last_read_at", "TEXT"))

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
    if was < 11:
        # Keyed on the version rather than on the column being fresh: the column and the value
        # arrive together, and a database that already has the column may still have rows that
        # were written before anything filled it.
        filled = _backfill_opportunity_sources(conn)
        if filled:
            log.info("opportunities: filled ecosystem / source_type on %d rows", filled)
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
    """Every collected card, newest first, payload included.

    The payload ships with the row because the shelf draws the card itself: a second request
    per card would be the page stitching one screen out of many calls, which is the thing
    this API does not do. A shelf is tens of rows.

    Ordered by when the reader kept the thing rather than by when the payload last changed:
    a round that writes a newer card for a token somebody kept replaces the payload in place
    (`favorite_refresh`), and a shelf that re-sorted itself every round would move rows the
    reader never touched.

    `kept = 1` is the whole of the shelf's filter. The same table also holds the cards a CA
    search left behind (kept = 0), which are nobody's collection - they are the reader's own
    `最近搜索`, and they must not appear here.
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM favorites WHERE kept = 1"
            " ORDER BY saved_at DESC, ca DESC").fetchall()]
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
    """Put a card here for a token: collect it, or leave a search record.

    Replacing is the whole of the update path. `saved_at` is deliberately left where it was
    on an update - see `favorites` - and `updated_at` is what says the payload changed.

    `kept` comes from the caller: the reader's star passes 1 (and the default is 1), a CA
    search passes 0. The update deliberately does not carry it - re-searching a token that is
    on the shelf must not quietly take it off, and the only write allowed to change that
    column is `favorite_set_kept`, which is the reader saying so.
    """
    now = now_iso()
    kept = 1 if row.get("kept") is None else int(row["kept"])
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO favorites (chain_id, ca, symbol, origin, origin_run_id, origin_label,"
            " score, rank, config_id, kept, payload_json, saved_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, ca) DO UPDATE SET"
            " symbol = excluded.symbol, origin = excluded.origin,"
            " origin_run_id = excluded.origin_run_id, origin_label = excluded.origin_label,"
            " score = excluded.score, rank = excluded.rank, config_id = excluded.config_id,"
            " payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            (row["chain_id"], row["ca"], row.get("symbol"), row.get("origin") or "round",
             row.get("origin_run_id"), row.get("origin_label") or "", row.get("score"),
             row.get("rank"), row.get("config_id"), kept, row["payload_json"], now, now),
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

    A card a CA search left behind has no set of settings at all (`config_id` is NULL), and
    a round may take it over whatever settings it ran under: the search read the token
    without a settings context, so the round's card is strictly the more informed one. The
    row takes that card - run, config and body - but not its `origin`: a searched row stays a
    searched row, because 最近搜索 reads that column and the shelf reads `kept`, so a row that
    flipped to `origin = 'round'` without being kept was on neither page - searched once, then
    invisible until the reader searched it again (10.10). `kept` is left alone either way.

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
                "UPDATE favorites SET symbol = ?, origin_run_id = ?,"
                " origin_label = ?, score = ?, rank = ?, config_id = ?, payload_json = ?,"
                " updated_at = ?"
                " WHERE chain_id = ? AND ca = ? AND (config_id IS NULL OR config_id IS ?)"
                " AND coalesce(origin_label, '') < ?",
                (row.get("symbol"), row.get("origin_run_id"), label, row.get("score"),
                 row.get("rank"), row.get("config_id"),
                 row["payload_json"], now,
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


def favorite_set_kept(chain_id: str, ca: str, kept: bool) -> bool:
    """Collect a card that is already on file, or put it back to a search record.

    The star on a searched token has no round to copy a card from - the card is already here,
    because the search wrote it - so collecting that one is this write and nothing else.
    Answers whether there was a row to change.
    """
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE favorites SET kept = ?, updated_at = ? WHERE chain_id = ? AND ca = ?",
            (1 if kept else 0, now_iso(), chain_id, ca),
        )
        return bool(cursor.rowcount)
    finally:
        conn.close()


def lookups(limit: int = 50) -> list[dict]:
    """The cards CA searches left behind, newest first, payload included.

    `origin = 'search'`, not `kept = 0`: a searched token the reader then collected stays on
    this list - it is what they looked up, which does not stop being true - and it also shows
    up on the shelf. Ordered by `origin_label`, which is the stamp every row here prints as
    its 生成时间: the list then runs in the order of the times it shows.

    Deliberately not `saved_at`. That one is frozen at a row's first write, so a token searched
    twice stays where the first search left it while printing the second one's time - the list
    reads as unsorted, an older stamp above a newer one, with neither number wrong. And
    `saved_at` is the shelf's 收藏时间: a search may not move it.
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM favorites WHERE origin = 'search'"
            " ORDER BY coalesce(origin_label, '') DESC, ca DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        conn.close()


def lookup_forget(chain_id: str, ca: str) -> bool:
    """Drop one search record and the card that came with it.

    Only a record: `origin = 'search'` and `kept = 0` are both in the WHERE, so this is not
    a second way to take a card off the shelf. A row the reader starred is a favourite, and
    the only write that ends that is the star itself - `favorite_set_kept` says so for the
    other direction of this pair. The manager greys those rows out rather than letting the
    press be made to be told why (ConfigManager does the same for 默认 / 生效).

    Answers whether there was a record to drop: a second press is not an error.
    """
    conn = connect()
    try:
        cursor = conn.execute(
            "DELETE FROM favorites WHERE chain_id = ? AND ca = ?"
            " AND origin = 'search' AND kept = 0", (chain_id, ca))
        return bool(cursor.rowcount)
    finally:
        conn.close()


def lookups_clear() -> int:
    """Drop every record that is not also a card on the shelf. Says how many went.

    The same WHERE as `lookup_forget` without the key: one write for the whole list, so a
    reader emptying 最近搜索 is not one round trip per row.
    """
    conn = connect()
    try:
        cursor = conn.execute("DELETE FROM favorites WHERE origin = 'search' AND kept = 0")
        return cursor.rowcount
    finally:
        conn.close()


def job_put(job: dict) -> dict:
    """Write one CA search down: the stages it has reached, and what it answered.

    The whole row every time, the same way favorite_put works: the runner holds the job in
    memory while it runs - it owns the stage clock - and this is the seam where that state
    becomes readable to the page polling for it. The stage list goes in as JSON because it
    is a list the page draws, not something anything here queries by.
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO search_jobs (id, ca, chain_id, status, stage, stages_json, error,"
            " row_json, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET"
            " chain_id = excluded.chain_id, status = excluded.status, stage = excluded.stage,"
            " stages_json = excluded.stages_json, error = excluded.error,"
            " row_json = excluded.row_json, finished_at = excluded.finished_at",
            (job["id"], job["ca"], job.get("chain_id"), job["status"], job.get("stage"),
             json.dumps(job.get("stages") or [], ensure_ascii=False), job.get("error"),
             json.dumps(job["row"], ensure_ascii=False) if job.get("row") else None,
             job["started_at"], job.get("finished_at")),
        )
        conn.commit()
    finally:
        conn.close()
    return job


def _job_out(row) -> dict:
    """One stored job as the page reads it: the stage list and the card, decoded."""
    job = dict(row)
    job["stages"] = json.loads(job.pop("stages_json") or "[]")
    job["row"] = json.loads(job.pop("row_json") or "null")
    return job


def job_get(job_id: str) -> dict | None:
    """One search job, as the page polls it."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM search_jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else _job_out(row)


def job_latest() -> dict | None:
    """The newest search job this app ran, or None when it never ran one.

    `started_at` is the search's own clock and every row carries the same offset, so ordering
    by it as text is ordering by time; the id breaks a tie inside one second.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM search_jobs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return None if row is None else _job_out(row)


# ============================================================================================
# 新机会（api/app/opportunity/）。SQL 住在这里的理由与上面每一节相同：数据模型只有一处，
# 功能模块不自己拼语句。原始材料（posts）只增不改，条目与 watchlist 可编辑。
# ============================================================================================


def opportunity_sources() -> list[dict]:
    """The whole watchlist, in the order it was added."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM opportunity_sources ORDER BY kind, target").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def opportunity_sources_enabled() -> list[dict]:
    """The rows a scan will read, least recently read first.

    One scan reads a bounded number of sources (`opportunity/scan.py` MAX_SOURCES), because a
    hundred timelines at the X throttle is minutes of sleeping and a rate limit waiting to
    happen. Ordering by `last_read_at` is what makes the bound a rotation rather than a
    starvation: the rows the last scan did not reach are the ones this scan starts with.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM opportunity_sources WHERE enabled = 1"
            " ORDER BY COALESCE(last_read_at, '') ASC, kind, target").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def opportunity_source_touched(target: str) -> None:
    """Record that a scan read this source, whatever it found."""
    conn = connect()
    try:
        conn.execute("UPDATE opportunity_sources SET last_read_at = ? WHERE target = ?",
                     (now_iso(), target))
    finally:
        conn.close()


def opportunity_source_put(rows: list[dict]) -> int:
    """Add watchlist rows, ignoring the ones already there. Returns how many went in."""
    if not rows:
        return 0
    written = 0
    conn = connect()
    try:
        for row in rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO opportunity_sources"
                " (target, kind, ecosystem, cluster, enabled, window_hours, slice_hours,"
                "  max_results, note, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["target"], row["kind"], row.get("ecosystem") or "multi",
                 row.get("cluster"), 1 if row.get("enabled", True) else 0,
                 int(row.get("window_hours") or 24), int(row.get("slice_hours") or 4),
                 int(row.get("max_results") or 40), row.get("note"), row["added_at"]),
            )
            written += cursor.rowcount or 0
    finally:
        conn.close()
    return written


def opportunity_post_put(row: dict) -> bool:
    """Store one post. False when it was already there - the same post arrives every scan.

    `classified` and `extracted_at` are not in the insert list on purpose: a post already read
    keeps the reading it was given, and a re-scan must not blank it back to "not asked".
    """
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO opportunity_posts"
            " (tweet_id, handle, author_name, text, lang, metrics_json, score, rules_json,"
            "  urls_json, posted_at, url, source_target, first_seen_scan)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["tweet_id"], row["handle"], row.get("author_name"), row["text"], row.get("lang"),
             json.dumps(row.get("metrics") or {}, ensure_ascii=False), row.get("score"),
             row.get("rules_json"), json.dumps(list(row.get("urls") or []), ensure_ascii=False),
             row.get("posted_at"), row.get("url"),
             row.get("source_target"), row.get("first_seen_scan")),
        )
        return bool(cursor.rowcount)
    finally:
        conn.close()


def opportunity_trace_queue(limit: int = 40) -> list[dict]:
    """Leads waiting to be traced, best score first.

    A lead is a post that was classified as an activity or a project, came from a **query** row
    (an account we do not watch - one of ours is authoritative already), and has not been traced
    yet. Ordered by the free gate's score: the trace is the expensive step, so the leads it reads
    first are the ones the gate thought most promising.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT p.tweet_id AS ref, p.handle, p.text, p.url, p.urls_json, p.project_name,"
            " p.source_target, p.classified FROM opportunity_posts p"
            " JOIN opportunity_sources s ON s.target = p.source_target"
            " WHERE p.trace_level IS NULL AND s.kind = 'query'"
            " AND p.classified IN ('activity', 'project')"
            # 有名字的先溯：名字是二次搜索的输入，没名字的线索只能靠帖里的链接与账号，
            # 而社区帖基本两样都没有（2026-10-19 实测：没名字的线索一条 5 秒、全 C）。
            " ORDER BY (p.project_name IS NULL), COALESCE(p.score, 0) DESC, p.posted_at DESC"
            " LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def opportunity_name_put(ref: str, name: str) -> None:
    """Store the project name the classification pass read.

    It is the merge key for topics and the query for the trace's second search, so dropping it -
    which is what the first version did - leaves both of them with nothing to work from.
    """
    if not str(name or "").strip():
        return
    conn = connect()
    try:
        conn.execute("UPDATE opportunity_posts SET project_name = ? WHERE tweet_id = ?",
                     (str(name).strip(), ref))
    finally:
        conn.close()


def opportunity_group_put(assignments: list[dict], irrelevant: list[str]) -> None:
    """Write a completed batch grouping in one transaction.

    The grouping stage has already validated that every post has a decision. Either all of
    those decisions land, or none does: a half-written group map would make the next scan
    treat the remaining posts as ungrouped while the page showed a partial project.
    """
    stamp = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN")
        for row in assignments:
            conn.execute(
                "UPDATE opportunity_posts SET classified = ?, classified_at = ?,"
                " project_name = ? WHERE tweet_id = ? AND classified IS NULL",
                (row["kind"], stamp, row["name"], row["ref"]),
            )
        for ref in irrelevant:
            conn.execute(
                "UPDATE opportunity_posts SET classified = 'other', classified_at = ?,"
                " project_name = NULL WHERE tweet_id = ? AND classified IS NULL",
                (stamp, ref),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def opportunity_trace_put(tweet_id: str, level: str, payload: dict) -> None:
    """Record one lead's trace: its level and everything that was read to decide it.

    Written once per lead per scan that traced it. A later scan may trace it again (a project
    without a home today may have one tomorrow), and the newer verdict replaces the older one -
    the pages come with it, so what is stored always describes the same reading.
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE opportunity_posts SET trace_level = ?, trace_json = ?, traced_at = ?"
            " WHERE tweet_id = ?",
            (level, json.dumps(payload, ensure_ascii=False), now_iso(), tweet_id))
    finally:
        conn.close()


def opportunity_materials(unclassified: bool = True, limit: int = 40) -> list[dict]:
    """Material waiting for one of the two model passes, newest first.

    Posts only. The module read pages as well until 2026-10-18, when the user narrowed it to
    one source - X - and the page reader and its table went with the rest (see
    `opportunity/README.md` · 发现). A post never changes, so its verdict is kept; the cap is on
    material rather than on a table, because a pass that means to read twelve things reads
    twelve.
    """
    conn = connect()
    try:
        if unclassified:
            rows = conn.execute(
                "SELECT tweet_id AS ref, 'post' AS source_kind, handle AS source, text,"
                " posted_at AS at, url, source_target, metrics_json, score, trace_level,"
                " trace_json FROM opportunity_posts"
                # 按免费闸的分排序，不按时间：每轮只问 60 条，按时间排会让**旧的但高分**的帖子
                # 永远排不上（2026-10-19 实测：重建后 DarkHorse 的两条高分帖被饿死，用户发现
                # "之前发现过、现在看不到"）。闸已经给每条打过分，这 60 个名额应该花在最好的上面。
                " WHERE classified IS NULL"
                " ORDER BY COALESCE(score, 0) DESC, posted_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT tweet_id AS ref, 'post' AS source_kind, handle AS source, text,"
                " posted_at AS at, url, source_target, metrics_json, project_name,"
                " trace_level, trace_json"
                " FROM opportunity_posts"
                " WHERE classified IN ('activity', 'project') AND extracted_at IS NULL"
                " ORDER BY COALESCE(score, 0) DESC, posted_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        # The heat of an entry is its source post's own engagement, so the numbers travel with
        # the material.
        item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
        out.append(item)
    return out


def opportunity_mark(ref: str, source_kind: str, *, classified: str | None = None,
                     extracted: bool = False) -> None:
    """Write one material's verdict, or that the expensive pass has read it."""
    table = "opportunity_posts"
    key = "tweet_id" if source_kind == "post" else "url"
    sets, values = [], []
    if classified is not None:
        sets += ["classified = ?", "classified_at = ?"]
        values += [classified, now_iso()]
    if extracted:
        sets.append("extracted_at = ?")
        values.append(now_iso())
    if not sets:
        return
    conn = connect()
    try:
        conn.execute("UPDATE %s SET %s WHERE %s = ?" % (table, ", ".join(sets), key),
                     (*values, ref))
    finally:
        conn.close()


def opportunity_put(entry: dict, steps: list[dict]) -> None:
    """Write one entry and its steps. One transaction, because a half-written step list is a
    set of instructions nobody can follow.

    `discovered_at` is the exception to "a re-scan rewrites what it can re-derive": it keeps
    the earliest value the row ever had, because it is this project's own observation and the
    list is sorted by it.
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO opportunities (slug, kind, category, title, organizer, official_url,"
            " summary, trust, trust_basis, ecosystem, source_type, risk, barrier, maturity,"
            " started_at, deadline, status, reason, heat, heat_basis, discovered_at, source_kind,"
            " source_ref, extract_version, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slug) DO UPDATE SET"
            " kind = excluded.kind, category = excluded.category, title = excluded.title,"
            " organizer = excluded.organizer, official_url = excluded.official_url,"
            " summary = excluded.summary, trust = excluded.trust,"
            " trust_basis = excluded.trust_basis, ecosystem = excluded.ecosystem,"
            " source_type = excluded.source_type, risk = excluded.risk,"
            " barrier = excluded.barrier, maturity = excluded.maturity,"
            " started_at = excluded.started_at, deadline = excluded.deadline,"
            " status = excluded.status, reason = excluded.reason,"
            " heat = excluded.heat, heat_basis = excluded.heat_basis,"
            " source_kind = excluded.source_kind, source_ref = excluded.source_ref,"
            " extract_version = excluded.extract_version, updated_at = excluded.updated_at",
            (entry["slug"], entry["kind"], entry.get("category"), entry["title"],
             entry.get("organizer"), entry.get("official_url"), entry.get("summary"),
             entry.get("trust") or "unjudged", entry.get("trust_basis"),
             entry.get("ecosystem"), entry.get("source_type"), entry.get("risk"),
             entry.get("barrier"), entry.get("maturity"), entry.get("started_at"),
             entry.get("deadline"), entry.get("status") or "candidate", entry.get("reason"),
             entry.get("heat"), entry.get("heat_basis"), entry["discovered_at"],
             entry.get("source_kind"), entry.get("source_ref"),
             entry.get("extract_version"), now_iso()),
        )
        conn.execute("DELETE FROM opportunity_steps WHERE slug = ?", (entry["slug"],))
        for index, step in enumerate(steps or []):
            conn.execute(
                "INSERT INTO opportunity_steps (slug, ordinal, title, body, source_url, source_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entry["slug"], index, step.get("title"), step["body"], step.get("source_url"),
                 step.get("source_at")),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def opportunity_items(status: str | None = None, kind: str | None = None,
                      limit: int = 200) -> list[dict]:
    """Every entry with its steps, newest discovery first.

    The un-promoted rows come back too: "why is this not on the list" is a question the page
    has to be able to answer, and a query that only ever returns successes cannot.
    """
    conn = connect()
    try:
        where, values = [], []
        if status:
            where.append("status = ?")
            values.append(status)
        if kind:
            where.append("kind = ?")
            values.append(kind)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            "SELECT * FROM opportunities%s ORDER BY discovered_at DESC, slug LIMIT ?"
            % clause, (*values, limit)).fetchall()
        items = [dict(row) for row in rows]
        if items:
            marks = ",".join("?" for _ in items)
            steps = conn.execute(
                "SELECT * FROM opportunity_steps WHERE slug IN (%s) ORDER BY slug, ordinal"
                % marks, tuple(item["slug"] for item in items)).fetchall()
            grouped: dict[str, list[dict]] = {}
            for step in steps:
                grouped.setdefault(step["slug"], []).append(dict(step))
            for item in items:
                item["steps"] = grouped.get(item["slug"], [])
    finally:
        conn.close()
    return items


def opportunity_retire(slug: str, reason: str) -> None:
    """Take one entry off the list without deleting it, and say why.

    Only the status moves: the row keeps its steps, its sources and its heat, because "this
    ended" is a fact about a thing that existed, not a reason to forget it.
    """
    conn = connect()
    try:
        conn.execute("UPDATE opportunities SET status = 'retired', reason = ?, updated_at = ?"
                     " WHERE slug = ?", (reason, now_iso(), slug))
    finally:
        conn.close()


#: What the reader may set. `active` is the default a fresh entry gets.
USER_STATES = ("active", "archived", "deleted")


def opportunity_user_state(slug: str, state: str) -> bool:
    """归档 / 删除 / 恢复 one entry. False when the slug is not one of ours.

    Only these three columns move: the pipeline's own reading (`status`, `reason`, the steps) is
    left exactly as it was, because "I archived it" must not erase "why it was not on the list".
    """
    if state not in USER_STATES:
        raise ValueError("unknown user_state %r" % (state,))
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE opportunities SET user_state = ?, user_state_at = ?, updated_at = ?"
            " WHERE slug = ?", (state, now_iso(), now_iso(), slug))
        return bool(cursor.rowcount)
    finally:
        conn.close()


def opportunity_heat_inputs() -> list[dict]:
    """Every entry that has a source post, with that post's own numbers.

    Heat is derived, not judged: it is a function of one stored post's metrics and the current
    scale. So when the scale moves, the existing rows are recomputed rather than left holding a
    number the code no longer produces - which is the difference between a derived column and a
    recorded one.
    """
    conn = connect()
    try:
        # Joined on the post's **url**, because that is what an entry keeps as `source_ref`: a
        # reader clicks through to the post, so the column holds a link rather than a row key.
        rows = conn.execute(
            "SELECT o.slug AS slug, p.metrics_json AS metrics_json FROM opportunities o"
            " JOIN opportunity_posts p ON p.url = o.source_ref"
            " WHERE o.source_kind = 'post'").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append({"slug": row["slug"],
                    "metrics": json.loads(row["metrics_json"] or "{}")})
    return out


def opportunity_set_heat(slug: str, value: float | None, basis: str | None) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE opportunities SET heat = ?, heat_basis = ? WHERE slug = ?",
                     (value, basis, slug))
    finally:
        conn.close()


def opportunity_item(slug: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM opportunities WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        steps = conn.execute(
            "SELECT * FROM opportunity_steps WHERE slug = ? ORDER BY ordinal", (slug,)).fetchall()
        item["steps"] = [dict(step) for step in steps]
    finally:
        conn.close()
    return item


def opportunity_counts() -> dict[str, int]:
    """How many entries sit in each state, for the page's own header."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT status, kind, count(*) AS n FROM opportunities GROUP BY status, kind").fetchall()
        posts = conn.execute("SELECT count(*) AS n FROM opportunity_posts").fetchone()
        sources = conn.execute("SELECT count(*) AS n FROM opportunity_sources").fetchone()
    finally:
        conn.close()
    out = {"posts": posts["n"], "sources": sources["n"]}
    for row in rows:
        out["%s:%s" % (row["status"], row["kind"])] = row["n"]
    return out


def opportunity_scans(limit: int = 20) -> list[dict]:
    """The scans so far, newest first: the 归档 tab and the time strip read this."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, status, stage, started_at, finished_at, counters_json, error"
            " FROM opportunity_scans ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        item["counters"] = json.loads(item.pop("counters_json") or "{}")
        out.append(item)
    return out


def opportunity_queues(limit: int = 6) -> dict:
    """What is still waiting, in three piles: not classified, classified but not extracted, and
    leads whose trace found no official home.

    This is the answer to "I saw this project earlier, where is it" - the rebuild and the caps mean
    a post can sit in a queue for scans, and a queue nobody can see is a queue that looks like a
    loss (user, 2026-10-19).
    """
    conn = connect()
    try:
        waiting = conn.execute(
            "SELECT count(*) AS n FROM opportunity_posts WHERE classified IS NULL").fetchone()["n"]
        top_waiting = conn.execute(
            "SELECT tweet_id AS ref, handle, text, COALESCE(score, 0) AS score"
            " FROM opportunity_posts WHERE classified IS NULL"
            " ORDER BY COALESCE(score, 0) DESC, posted_at DESC LIMIT ?", (limit,)).fetchall()
        pending = conn.execute(
            "SELECT count(*) AS n FROM opportunity_posts"
            " WHERE classified IN ('activity', 'project') AND extracted_at IS NULL").fetchone()["n"]
        top_pending = conn.execute(
            "SELECT tweet_id AS ref, handle, text, COALESCE(score, 0) AS score, project_name"
            " FROM opportunity_posts"
            " WHERE classified IN ('activity', 'project') AND extracted_at IS NULL"
            " ORDER BY COALESCE(score, 0) DESC, posted_at DESC LIMIT ?", (limit,)).fetchall()
        failed = conn.execute(
            "SELECT count(*) AS n FROM opportunity_posts WHERE trace_level = 'C'").fetchone()["n"]
    finally:
        conn.close()
    return {
        "unclassified": {"count": waiting, "top": [dict(row) for row in top_waiting]},
        "unextracted": {"count": pending, "top": [dict(row) for row in top_pending]},
        "trace_failed": {"count": failed},
    }


def opportunity_scan_put(job: dict) -> dict:
    """Write one scan row, whole. The page polls this and nothing else."""
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO opportunity_scans"
            " (id, status, stage, stages_json, counters_json, error, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job["id"], job["status"], job.get("stage"),
             json.dumps(job.get("stages") or [], ensure_ascii=False),
             json.dumps(job.get("counters") or {}, ensure_ascii=False), job.get("error"),
             job["started_at"], job.get("finished_at")),
        )
    finally:
        conn.close()
    return job


def _scan_out(row) -> dict:
    scan = dict(row)
    scan["stages"] = json.loads(scan.pop("stages_json") or "[]")
    scan["counters"] = json.loads(scan.pop("counters_json") or "{}")
    return scan


def opportunity_scan_get(scan_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM opportunity_scans WHERE id = ?", (scan_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else _scan_out(row)


def opportunity_scan_latest() -> dict | None:
    """The newest scan, so a page that did not start one still shows the last result."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM opportunity_scans ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return None if row is None else _scan_out(row)


# ============================================================================================
# 新机会 v2（2026-10-20）：实体 / 机会 / 源 / 关注规则 / 材料 / 证据 / 建设扩展。
# 旧的 opportunity_* 函数继续服务旧页面，直到切换项把它们删掉。
# ============================================================================================


def _stable_id(prefix: str, key: str) -> str:
    return prefix + "_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
