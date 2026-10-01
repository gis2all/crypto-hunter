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
#: 9 added `favorites.kept`: the row is now either a card the reader collected or a record a
#: CA search left behind. A search writes its card with kept = 0, so a token can be searched
#: and then collected without a second row for it; the shelf reads `kept = 1` only.
SCHEMA_VERSION = 10


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
                  ("favorites", "kept", "INTEGER NOT NULL DEFAULT 1"))

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
