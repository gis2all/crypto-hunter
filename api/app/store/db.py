"""SQLite connection handling: WAL, foreign keys, schema bootstrap."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .. import config
from ..feed import htmlbody, materials, rules

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
#: 10 added `feed_items` and `feed_editions`: the macro feed's two tables. A new domain, not
#: a new column - nothing in the radar reads or writes them, and nothing cascades either way.
#: 11 gave the feed's issues a period: `feed_editions.slot` (am | pm) became `kind` (day |
#: week | month) beside a `key`, and the table gained the three fields the archive list draws
#: a row from without reading a body. That one is a rebuild rather than a column change -
#: `_rebuild_feed_editions` says why.
#: 14 gave the feed's snapshot the words the model wrote: the Chinese title, the summary, the
#: reason, and (for entries past the curated line) the translated body with its own headings.
#: Rule-written text is still computed on read and never stored, so a row that no model has
#: reached keeps working - `llm_stage` is the column that says how far it got (feed/writing.py).
#: 12 added `feed_sources` and `feed_fetch_runs`: the feed stops reading a code-resident roster
#: and starts reading rows. A source becomes data (kind picks one of four fetchers, the rest is
#: config), and every source carries its own clock - cursor, fail count, health, next fetch.
#: Nothing to migrate: the roster is imported from `config/feed/sources.yaml` on first use, and
#: no other table references these two.
#: 18 added `feed_stories`, `feed_facts` and `feed_item_facts`: the grouping result. The three
#: together replace "every read re-runs the rule grouper" - a story is decided once, by the
#: model, and then read back (feed/stories.py). Nothing is migrated: an empty table means no
#: model has grouped anything yet, and the rule version stands in until one has.
#: 20 added `category_llm` and `tags_json`: the structure step's category and tags, which replaced
#: "every read re-runs the rule classifier" for the rows a model has actually looked at.
#: 24 added `feed_story_heat_hourly` and `feed_hot_rankings`: the hourly heat snapshot and the
#: ranking snapshot the pages read back (AIHOT's `story_heat_hourly` / `hot_rankings`). Both are
#: pure derived data - an empty table just means no snapshot has been taken yet.
#: 25 added `llm_receipts` and `llm_receipt_attempts`: the paid-request receipts (AIHOT's
#: `receipts` / `receipt_attempts`). Derived bookkeeping again - empty just means nothing paid
#: has been recorded yet.
#: 28 added content identity to `feed_items` (`excerpt`, `content_hash`, `revision`,
#: `llm_revision`) and the `feed_item_revisions` archive (AIHOT's `contentHash` / `revision` /
#: `article_revisions`). The version the migration is keyed on is the one that backfills
#: `llm_revision = revision` for rows already written: without it every row in the library would
#: look like a conclusion written for an older revision and the whole paid queue would run again.
#: 33 added `feed_quote_translations`: 被引用的那几句话单独译一份 (3.6)。
SCHEMA_VERSION = 33


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
_ADDED_COLUMNS = (("feed_items", "quoted_json", "TEXT"),
                  ("feed_items", "category_llm", "TEXT"),
                  ("feed_items", "tags_json", "TEXT"),
                  ("feed_items", "score_llm", "REAL"),
                  ("feed_items", "llm_scores_json", "TEXT"),
                  ("feed_items", "fact_json", "TEXT"),
                  ("feed_items", "subjects_json", "TEXT"),
                  ("feed_item_facts", "prompt_version", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_story_signals", "prompt_version", "TEXT NOT NULL DEFAULT ''"),
                  ("runs", "progress", "INTEGER NOT NULL DEFAULT 0"),
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
                  ("feed_items", "media_json", "TEXT NOT NULL DEFAULT '[]'"),
                  ("feed_items", "detail_at", "TEXT"),
                  ("tokens", "peak_market_cap_at", "TEXT"),
                  ("favorites", "config_id", "TEXT"),
                  ("favorites", "kept", "INTEGER NOT NULL DEFAULT 1"),
                  ("feed_items", "title_zh", "TEXT"),
                  ("feed_items", "summary_llm", "TEXT"),
                  ("feed_items", "reason_llm", "TEXT"),
                  ("feed_items", "body_zh", "TEXT"),
                  ("feed_items", "body_complete", "INTEGER NOT NULL DEFAULT 0"),
                  ("feed_sources", "language", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_items", "relevance", "TEXT"),
                  ("feed_items", "llm_stage", "TEXT"),
                  ("feed_items", "prompt_version", "TEXT"),
                  ("feed_items", "llm_at", "TEXT"),
                  ("feed_stories", "digest_inputs", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_stories", "digest_ids", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_stories", "merged_into", "INTEGER"),
                  #: 身份守卫的账目 (feed/writing.py): 只在真退过时才写, 空字符串表示清掉旧记录.
                  ("feed_items", "identity_guard", "TEXT"),
                  #: 内容身份与版本 (AIHOT contentHash / revision): 见 feed/materials.py
                  #: 与下面的 `_next_revision`.
                  ("feed_items", "excerpt", "TEXT"),
                  ("feed_items", "content_hash", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_items", "revision", "INTEGER NOT NULL DEFAULT 1"),
                  ("feed_items", "llm_revision", "INTEGER"),
                  #: 发布门/可见性/收录 (feed/rules.py + feed/scope.py).
                  #: 这条的分析是哪一种 (4.9b): 空 = 还没判过, `primary` = 只谈一件事,
                  #: `composite` = 把几件事装在一起 (盘点/日报). 复合的那一条不立事件。
                  ("feed_items", "analysis_scope", "TEXT NOT NULL DEFAULT ''"),
                  ("feed_items", "released_at", "TEXT"),
                  ("feed_items", "withdrawn_at", "TEXT"),
                  ("feed_items", "seo_indexed_at", "TEXT"),
                  ("feed_items", "seo_excluded_at", "TEXT"),
                  #: 源的内容许可 (AIHOT 的 SourceFacts): 不是运行态, 是清单上的话。
                  ("feed_sources", "visibility", "TEXT NOT NULL DEFAULT 'public'"),
                  ("feed_sources", "withdrawn_at", "TEXT"),
                  ("feed_sources", "site_fulltext", "INTEGER NOT NULL DEFAULT 0"),
                  ("feed_sources", "syndicate_fulltext", "INTEGER NOT NULL DEFAULT 0"),
                  #: 来源图标 (AIHOT 的 `sources/icons.ts`): 清单里写的, 或采集层找到的那一枚。
                  ("feed_sources", "icon_url", "TEXT"),
                  #: 上一次去找图标的时刻。站点没有图标很少会长出来, 所以失败要等 30 天再试,
                  #: 不是每一轮都去敲一次。
                  ("feed_sources", "icon_checked_at", "TEXT"))

#: Columns an earlier shape carried. Nothing reads them, a fresh database never creates
#: them, and leaving them behind on an old one is a table that lies about what the code
#: uses - so they are dropped once, on the upgrade that removed their readers. (Most are
#: the previous lexicon's; `favorites.caliber` is a string the shelf no longer prints.)
#: A SQLite too old to drop a column simply keeps them; nothing depends on either way.
_DROPPED_COLUMNS = (("feed_items", "lead_llm"),
                    ("feed_items", "sections_llm"),
                    ("lexicon", "baseline_mcap"),
                    ("favorites", "caliber"),
                    ("lexicon", "baseline_holders"),
                    ("lexicon", "baseline_at"),
                    ("lexicon", "baseline_safety"),
                    ("feed_stories", "digest_version"),
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
    _rebuild_feed_editions(conn)
    if was < 28:
        # 库里已经写好的那些行, 它们的字是接着当时那一版写的 (revision 当时只能是 1) ——
        # 不把它们认领, 每一行都会看起来像"结论写在旧版上", 而一整库重跑一遍
        # 付费档是一笔没人要求过的开销。
        written = conn.execute("UPDATE feed_items SET llm_revision = revision"
                              " WHERE llm_stage IS NOT NULL AND llm_revision IS NULL").rowcount
        if written:
            log.info("feed_items: marked %d written rows as belonging to their revision", written)
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


def _rebuild_feed_editions(conn: sqlite3.Connection) -> int:
    """Replace an issue table keyed by half a day with one keyed by a period. Drops the rows.

    A column-level ALTER cannot do this job: the old table carries `slot TEXT NOT NULL` and
    `UNIQUE (slot, window_to)`, neither of which the new rows can satisfy, and SQLite refuses
    to drop a column a table constraint names. So the table is rebuilt, and it has to be the
    whole table - there is no honest mapping from "the morning of the 3rd" to "the 3rd", since
    two old rows would collide into one new key and one of them would have to lose.

    The rows are not worth a migration that invents a rule for that: they exist only because
    `scripts/seed_editions.py` ran against the fixture, and running it again rebuilds them.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(feed_editions)")}
    if "slot" not in have:
        return 0
    dropped = conn.execute("SELECT COUNT(*) FROM feed_editions").fetchone()[0]
    conn.execute("DROP TABLE feed_editions")
    # The schema script already ran once this startup, and it skipped this table because the
    # old one was in the way. Run it again: everything in it is IF NOT EXISTS.
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    log.warning("feed_editions: rebuilt for issue periods, dropped %d half-day issues", dropped)
    return dropped


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


# --- 加密信息流（宏观） -----------------------------------------------------
# 两张表：feed_items（读到的每条原始记录）与 feed_editions（一期）。
# 这里没有第三张「事件」表：事件是窗口内的推导结果，整期随 payload_json 一起存，
# 一份真相好过两份。等页面需要跨窗口浏览事件时再加，那时它才有第二个读者。

#: 「第几期」= 同一份档案里，不比这一期晚的那些有几期。
#:
#: 派生而不是存下来：存下来的期号在每次回填之后都要全部重算一遍，而回填是从最早的往回填
#: 还是从今天往前填，算出来的号不一样 —— 一份"第几期"取决于写入顺序的档案，会在下一次
#: 回填时自相矛盾。数出来的那个永远是当期这个答案。
_ISSUE_NUMBER = ("(SELECT COUNT(*) FROM feed_editions older"
                 " WHERE older.kind = feed_editions.kind"
                 " AND older.window_to <= feed_editions.window_to)")


def _next_revision(conn: sqlite3.Connection, prev: sqlite3.Row | None, title: str,
                   body: str, excerpt: str) -> tuple[int, str]:
    """这一份内容该算第几版, 以及它的身份 (AIHOT `upsertMaterial`).

    回 `(revision, content_hash)`. 版本号变了就是"内容真的改过", 该重判一次。

    四条让版本不涨的规矩, 每一条都对应一种真实发生过的噪声:
    1. 归一空白后哈希一样 —— 换行与缩进的差别不是改版;
    2. 库里还没有基准 (`content_hash` 空, 导入的历史) —— 第一次报告只记基准,
       不算一次改版。不这么做, 一次导入会把源仍然列着的每一篇都送回付费分析;
    3. 这一版以前见过 —— 列表在两种写法之间来回换、页面轮换推广位, 都是同一篇文章;
    4. 只差在传输中丢掉的字符 —— 有些 feed 每读一次糊掉几个字, 没有这一条
       revision 会一直涨。
    """
    text = htmlbody.to_text(body or "")
    next_hash = materials.content_hash(title, text, excerpt)
    if prev is None:
        return 1, next_hash
    stored = prev["content_hash"] or ""
    if stored == next_hash or not stored:
        return prev["revision"], (stored or next_hash)
    seen = conn.execute(
        "SELECT 1 FROM feed_item_revisions"
        " WHERE source_id = ? AND external_id = ? AND content_hash = ? LIMIT 1",
        (prev["source_id"], prev["external_id"], next_hash)).fetchone()
    if seen:
        return prev["revision"], stored
    if (materials.same_barring_loss(prev["title"], title)
            and materials.same_barring_loss(htmlbody.to_text(prev["body"] or ""), text)
            and materials.same_barring_loss(prev["excerpt"], excerpt)):
        return prev["revision"], stored
    return prev["revision"] + 1, next_hash


_ITEM_COLUMNS = ("source_id", "external_id", "revision", "content_hash", "title", "body", "excerpt",
                 "released_at")


def _item_row(conn: sqlite3.Connection, source_id: str, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT source_id, external_id, revision, content_hash, title, body, excerpt, released_at"
        " FROM feed_items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id)).fetchone()


def _item_release_at(prev: sqlite3.Row | None, now: str) -> str | None:
    """这一行什么时候过发布门。

    已有的一行不重算: 一次重读不能把已经公开的东西又关回去, 而一个
    每轮都往后推的时刻等于永远不公开。
    """
    if prev is not None and "released_at" in prev.keys():
        return prev["released_at"]
    return rules.release_at(now)


def _archive_revision(conn: sqlite3.Connection, source_id: str, external_id: str, revision: int,
                      content_hash: str, title: str, body: str, excerpt: str,
                      moment: str) -> None:
    """把这一版留档 (AIHOT 的 `article_revisions`).

    存的是纯文本而不是 HTML: 这张表只回答"这一版长什么样",
    以及"这份渲染以前见过没有", 两个问题都只需要字。
    """
    conn.execute(
        "INSERT INTO feed_item_revisions (source_id, external_id, revision, content_hash,"
        " title, body_text, excerpt, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (source_id, external_id, revision) DO NOTHING",
        (source_id, external_id, revision, content_hash, title,
         htmlbody.to_text(body or ""), excerpt or None, moment))


def feed_items_put(rows: list[dict]) -> int:
    """存下这个窗口读到的原始记录，一行一个（来源，该来源自己的 id）。

    是 upsert 而不是 insert：同一个来源用同一个 id 重发昨天的记录，那是同一条记录；一张
    每次读取都多一行的表，会让「本轮读到多少条」变成「我们看了几次」。

    `identity_key` 也在更新之列，而且这是必须的：这一列是窗口学到的名字算出来的，词表长大
    之后同一条记录会归到更准的事件上，重跑一遍就该改写它 —— 原始记录自己没变，它属于哪件事
    变了。

    而"原始记录自己没变"现在是一个算出来的答案（`_next_revision`），不是一个假设：
    正文真的改了就是新版（`revision + 1`），库里那些模型写的字因此过期，下一轮重写。
    """
    if not rows:
        return 0
    now = now_iso()
    conn = connect()
    try:
        for row in rows:
            source_id = row["source_id"]
            external_id = row["external_id"]
            prev = _item_row(conn, source_id, external_id)
            # 这一次报告带来的那份内容: 源这次没给正文就留着库里那份
            # (AIHOT 的 `bodyText ?? existing.body_text`)。不然一条只在首轮给过正文的源,
            # 第二轮的列表更新会把已经补到的正文抹掉。
            body = row.get("body") or (prev["body"] if prev is not None else "") or ""
            excerpt = row.get("excerpt") or (prev["excerpt"] if prev is not None else "") or ""
            revision, digest = _next_revision(conn, prev, row["title"], body, excerpt)
            # 发布门: 一条从被读到到公开的缓冲。已有的行不重算
            # (一次重读不能把已公开的东西又关回去)。
            release_stamp = (_item_release_at(prev, now))
            conn.execute(
                "INSERT INTO feed_items (source_id, external_id, source_kind, identity_key,"
                " title, url, author, body, excerpt, content_hash, revision, published_at,"
                " media_json, quoted_json, seen_at, released_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, external_id) DO UPDATE SET"
                " identity_key = excluded.identity_key, title = excluded.title,"
                " url = excluded.url, author = excluded.author, body = excluded.body,"
                " excerpt = excluded.excerpt, content_hash = excluded.content_hash,"
                " revision = excluded.revision,"
                " published_at = excluded.published_at,"
                # `seen_at` 不在更新之列: 它是**第一次**读到这一条的时刻 (AIHOT 的
                # `discovered_at`), 重读一次不改变"它什么时候被发现的". 时间线判定靠它
                # (materials.is_historical), 每次 upsert 都刷新的话, 一条老记录会越读越"新",
                # 于是永远判不成历史.
                " media_json = excluded.media_json, quoted_json = excluded.quoted_json",
                (source_id, external_id, row["source_kind"], row["identity_key"],
                 row["title"], row["url"], row.get("author"), body, excerpt, digest, revision,
                 _utc_stamp(row["published_at"]),
                 json.dumps(row.get("media") or [], ensure_ascii=False),
                 json.dumps(row.get("quoted") or {}, ensure_ascii=False) or None, now,
                 release_stamp),
            )
            if prev is None or digest != (prev["content_hash"] or ""):
                _archive_revision(conn, source_id, external_id, revision, digest,
                                  row["title"], body, excerpt, now)
        return len(rows)
    finally:
        conn.close()


def feed_items_for(identity_key: str, limit: int = 200) -> list[dict]:
    """一条事件下面的原始记录，最早的在前：一期引用了什么，就从这里追溯。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_items WHERE identity_key = ?"
            " ORDER BY published_at ASC LIMIT ?", (identity_key, limit)).fetchall()]
    finally:
        conn.close()


def feed_item_get(source_id: str, external_id: str) -> dict | None:
    """一行条目, 或者 None。台账 / 星标有效性 / 分享图都要它 ——
    这几处问的是“这一行现在什么样”, 而不是“窗口里有什么”。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM feed_items WHERE source_id = ? AND external_id = ?",
            (source_id, external_id)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def feed_items_pending_llm(stale_before: str, limit: int = 40, version: str = "",
                           versions: Sequence[str] = (),
                           rewritten: Sequence[tuple[str, str]] = ()) -> list[dict]:
    """还欠模型一遍、而且上一趟问它已经在 `stale_before` 之前的那些行, 新的在前。

    两条路各算各的, 不是一句 AND 串起来:
    1. 还没走到头 (`llm_stage` 不到 `full`) **而且**上一趟问它已经在 `stale_before` 之前 ——
       失败过的过一阵再补, 没问过的一直在队里;
    2. 已经写到 `full`, 但 `prompt_version` 不是 `version` 开头 —— **提示词改过**。

    第 3 条与第 4 条都会把已经写完的行重新排进预算, 而它们是必须的: 提示词版本
    是内容哈希, 改一个字就是一个新版本, 而旧版写下的字是按旧口径写的 —— 不加这一条, 改了提
    示词只会影响此后新抓到的条目, 库里那几千行会一直用旧口径冒充新口径, 且完全静默
    (AIHOT 的 promptVersion 配 regroup_pending 是同一件事)。

    第 4 条问的是"这份结论还是不是说的现在这一版正文": `llm_revision` 跟不上 `revision`
    就重写。它也不等 `retryHours` —— 内容改了, 一份过期的结论再挂二十四小时
    只是让旧口径多活一天。

    还没问过的 (`llm_stage` 是 NULL) 排在前面 —— 它们一条一次调用; 已经到 `understand` 的不
    一定花往返 (不过精选线的在手里就结账), 所以先花该花的钱。

    两个时刻都归一成 UTC 再比 (与 `_utc_stamp` 同一条理由): 这里是文本比较, 一个写着 +08:00
    的时间戳会让"24 小时后再问它"变成"8 小时后"。
    """
    # 两个分支, 不是一个 OR 串: 「还没写完」要等够 retryHours 才再问, 而「写完了但提示词换过」
    # 不等 —— 提示词换过是一条已经过期的结论, 再等二十四小时只是让旧口径多活一天。
    #
    # 第 2 条问的是「这一串 token 里有没有现行的那一版」, 不是「开头是不是它」: 版本是一串
    # 逗号分开的 token (评分/写作/翻译各一段), 按前缀比会让每一行永远追不上版本, 而且完全静默。
    #
    # `versions` 是写作档的现行 token (理解或便宜, 命中**一个**就算这条写对了 —— 分高的走理解,
    # 分低的走便宜); `rewritten` 是「写过才有这一档 token」的那些档 (评分、翻译): 前缀在里面
    # 就说明它用过这一档, token 不是现役的就要重写。
    # `blocked` 是终态 (预筛判为无关): 与 `full` 一样不再排进预算, 但它一个字都没写, 所以页面
    # 上也不会出现 (没有中文标题摘要 = 不发布).
    fail = ["((llm_stage IS NULL OR llm_stage NOT IN ('full', 'blocked'))"
            " AND (llm_at IS NULL OR llm_at < ?))"]
    args: list = [_utc_stamp(stale_before)]
    current = [token for token in versions if token] or ([version] if version else [])
    if current or rewritten:
        tests: list[str] = []
        for token in current:
            tests.append("prompt_version LIKE ?")
            args.append("%%%s%%" % token)
        for prefix, token in rewritten:
            tests.append("(prompt_version LIKE ? AND prompt_version NOT LIKE ?)")
            args.append("%%%s%%" % prefix)
            args.append("%%%s%%" % token)
        joined = tests[0] if len(tests) == 1 else "(%s)" % " OR ".join(tests)
        fail.append("(llm_stage = 'full' AND (prompt_version IS NULL OR NOT %s))" % joined)
    # 第 4 条: 正文改过版。结论是接着某一版正文写的 (`llm_revision`), 而内容变了就是
    # 一份过期的结论 (AIHOT 的 `stale`: 旧 revision 的结果只留档, 不冒充新版)。
    fail.append("(llm_stage IS NOT NULL AND (llm_revision IS NULL OR llm_revision <> revision))")
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_items WHERE (" + " OR ".join(fail) + ")"
            " ORDER BY (llm_stage IS NOT NULL), published_at DESC LIMIT ?", args).fetchall()]
    finally:
        conn.close()


def feed_items_write_llm(rows: list[dict]) -> int:
    """把模型写好的字写回那一行。只写传进来的那几格 —— 两档写的是不同的列, 所以理解档的成功
    不会被翻译档的失败连累, 反过来也一样。

    一次一批而不是一行一次: 这一站一轮要走几十条, 一条一开连接是几十次开库; 而这里每一行都
    自带主键, 不需要事务把几行绑在一起 —— 半截的答案是这一层的正常状态 (`llm_stage` 说得出
    它走到了哪), 不是需要回滚的东西。
    """
    if not rows:
        return 0
    columns = ("title_zh", "summary_llm", "reason_llm", "body_zh", "body_complete",
               "category_llm", "tags_json", "fact_json", "subjects_json",
               "score_llm", "llm_scores_json", "relevance",
               "llm_stage", "prompt_version", "llm_at", "identity_guard")
    conn = connect()
    try:
        for row in rows:
            sets: list[str] = []
            args: list = []
            for column in columns:
                if column not in row or row[column] is None:
                    continue
                value = row[column]
                sets.append("%s = ?" % column)
                args.append(_utc_stamp(value) if column == "llm_at" else value)
            if not sets:
                continue
            # 这份结论是接着**现在**这一版正文写的。下一次内容改版 (`revision + 1`)
            # 时这一列就比 revision 小, 等于把这份结论标成过期 —— 它不被删,
            # 页面上还能看到, 但下一轮一定会被重写 (feed_items_pending_llm 第 4 条).
            sets.append("llm_revision = revision")
            args.extend([row["source_id"], row["external_id"]])
            conn.execute("UPDATE feed_items SET %s WHERE source_id = ? AND external_id = ?"
                         % ", ".join(sets), args)
        return len(rows)
    finally:
        conn.close()


def feed_edition_put(row: dict) -> dict:
    """存下一期，或把已经存在的那一期原样还回来。

    返回的是库里那一行，不是传进来的：同一个窗口跑第二遍时，答案必须是读者已经读过的那
    一期，而不是这次算出来的新版。重跑幂等这件事由表上的约束保证，不由调用方记得保证。
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_editions (id, kind, key, window_from, window_to, seen, entries,"
            " headline, metrics_json, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (row["id"], row["kind"], row["key"], row["window_from"], row["window_to"],
             int(row.get("seen") or 0), int(row.get("entries") or 0),
             row.get("headline") or "", row.get("metrics_json") or "[]",
             row["payload_json"], row.get("created_at") or now_iso()),
        )
        saved = conn.execute(
            "SELECT *, %s AS issue_number FROM feed_editions WHERE id = ?"
            % _ISSUE_NUMBER, (row["id"],)).fetchone()
        return dict(saved)
    finally:
        conn.close()


def feed_editions(limit: int = 20, kind: str | None = None) -> list[dict]:
    """出过的各期，新的在前。列表不带 payload：一期可以很大，而列表只画标题和时间。

    按 kind 过滤，因为三种刊各自是一份档案：日报的左栏里出现一段周报，读者会以为周报也是
    一天一期。`kind=None` 是不分刊的那种读法，给测试和排查用。
    """
    conn = connect()
    try:
        where, args = ("WHERE kind = ?", [kind, limit]) if kind else ("", [limit])
        return [dict(row) for row in conn.execute(
            "SELECT id, kind, key, window_from, window_to, seen, entries, %s AS issue_number,"
            " headline, created_at FROM feed_editions %s"
            " ORDER BY window_to DESC LIMIT ?" % (_ISSUE_NUMBER, where), args).fetchall()]
    finally:
        conn.close()


def feed_edition(edition_id: str) -> dict | None:
    """一期，含正文。"""
    conn = connect()
    try:
        row = conn.execute("SELECT *, %s AS issue_number FROM feed_editions WHERE id = ?"
                           % _ISSUE_NUMBER, (edition_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_edition_neighbors(kind: str, key: str) -> dict:
    """同一门刊里贴着这一期的前一期与后一期, 给的是 id (AIHOT 的 `neighbors`).

    按 `key` 比而不是按时间戳: key 是读者认得的那串 (`2026-10-03` / `2026-W40`), 同一个窗口
    补出的第二期不会跑到别的期前面去. 给 id 是因为页面拿它换期, key 只用来印字.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT (SELECT id FROM feed_editions WHERE kind = ? AND key < ?"
            " ORDER BY key DESC LIMIT 1) AS prev,"
            " (SELECT id FROM feed_editions WHERE kind = ? AND key > ?"
            " ORDER BY key ASC LIMIT 1) AS next",
            (kind, key, kind, key)).fetchone()
        return {"prev": row["prev"], "next": row["next"]}
    finally:
        conn.close()


def feed_latest_edition(kind: str | None = None) -> dict | None:
    """最新的一期，或者 None。页面第一次打开时要有东西可画，这是它读的那个。"""
    conn = connect()
    try:
        where, args = ("WHERE kind = ?", [kind]) if kind else ("", [])
        row = conn.execute(
            "SELECT *, %s AS issue_number FROM feed_editions %s ORDER BY window_to DESC LIMIT 1"
            % (_ISSUE_NUMBER, where), args).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _source_row(row) -> dict:
    """一行源, 其中 `config_json` 解成 `config`。

    四个抓取器只认识 `config` 这一个键, 而存储用哪一列是这一层的事: 留在原地就得让每个抓取
    器都自己 json 一遍, 少写一处就是一个源永远拿到空配置 —— 它会以「没有 feedUrl」的样子
    失败, 而真正的原因看不出跟存储有关。

    config 解不开就按空配置走: 一个坏行只让那一个源失败 (抓取器会报缺哪个键), 而不是把这一
    轮的源列表整个掐掉。
    """
    out = dict(row)
    try:
        out["config"] = json.loads(out.pop("config_json", None) or "{}")
    except ValueError:
        out["config"] = {}
    return out


def feed_sources_all(only_enabled: bool = False) -> list[dict]:
    """每一个源，按 id 排。出厂清单导入之后，这里是「有哪些源」的唯一出处。"""
    conn = connect()
    try:
        where = "WHERE enabled = 1" if only_enabled else ""
        return [_source_row(row) for row in conn.execute(
            "SELECT * FROM feed_sources %s ORDER BY id ASC" % where).fetchall()]
    finally:
        conn.close()


def feed_source_clocks() -> list[dict]:
    """每一个按时去抓的源，连同它最后一次成功的时刻（AIHOT 的 `sourceClocks`）。

    `last_ok_at` 是最后的**成功**，不是最后一次尝试 —— 拿失败的尝试当时钟，一个正在失败的源会被
    当成"刚刚读过"，而它这一格其实什么都没读到。`created_at` 一并带出来：热度涨幅那一比要知道
    每家是什么时候接入的（刚接入的一家会把涨幅算成几百个点）。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT id, kind, enabled, last_ok_at, interval_minutes, created_at"
            " FROM feed_sources ORDER BY id ASC").fetchall()]
    finally:
        conn.close()


def feed_sources_put(rows: list[dict]) -> int:
    """把出厂清单写进表 —— 只写「这个源是什么」那几列，运行态一列不碰。

    这份清单是人手编辑的，而 cursor / fail_count / health 是程序写的：导入时把它们覆盖掉，
    等于每改一次间隔就把这一路读到的位置和失败记录清零。所以那几列不在更新之列。
    `enabled` 是例外，它本来就是清单上的话，写着 false 就该 false。

    `next_fetch_at` 是另一个例外，但只在该改的时候：当这一行的「它是什么」（name / kind /
    config / interval_minutes / enabled）变了，下一次抓取的时刻清空 —— 修好的源不该等上一次
    失败的退避走完，否则界面上只能看到旧的失败。清单没动的那些行不受影响，退避照旧。
    """
    if not rows:
        return 0
    now = now_iso()
    conn = connect()
    try:
        for row in rows:
            icon = str(row.get("icon") or "")
            conn.execute(
                "INSERT INTO feed_sources (id, name, kind, config_json, tier, first_party,"
                " participation_mode, visibility, withdrawn_at, site_fulltext, syndicate_fulltext,"
                " interval_minutes, enabled, language, icon_url, icon_checked_at,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " name = excluded.name, kind = excluded.kind,"
                " config_json = excluded.config_json, tier = excluded.tier,"
                " first_party = excluded.first_party,"
                " participation_mode = excluded.participation_mode,"
                " visibility = excluded.visibility,"
                " withdrawn_at = excluded.withdrawn_at,"
                " site_fulltext = excluded.site_fulltext,"
                " syndicate_fulltext = excluded.syndicate_fulltext,"
                # 图标: 清单里写了就用清单的, 没写就留着采集层找到的那一枚 ——
                # 导入会把列重写一遍, 直接赋值等于每起一次进程就把找到的图标抹掉。
                " icon_url = COALESCE(excluded.icon_url, feed_sources.icon_url),"
                # 清单给了图标 = 这一格不用再找了, 记一个"刚看过"; 没给就保留原来的时刻。
                " icon_checked_at = CASE WHEN excluded.icon_url IS NOT NULL"
                " THEN excluded.icon_checked_at ELSE feed_sources.icon_checked_at END,"
                " interval_minutes = excluded.interval_minutes,"
                " language = excluded.language,"
                " enabled = excluded.enabled, updated_at = excluded.updated_at,"
                # 改那一行的「它是什么」改了，就让它立刻到点：否则上一次失败的退避还在计时，
                # 界面上只能看到旧的失败，而修好的配置要等上一个小时才自证。cursor / fail_count /
                # health 不在此列：那些是「它过得怎么样」，不是配置的一部分。
                " next_fetch_at = CASE WHEN name = excluded.name"
                " AND kind = excluded.kind AND config_json = excluded.config_json"
                " AND interval_minutes = excluded.interval_minutes"
                " AND enabled = excluded.enabled AND language = excluded.language"
                " AND visibility = excluded.visibility"
                " AND site_fulltext = excluded.site_fulltext"
                " AND syndicate_fulltext = excluded.syndicate_fulltext"
                " THEN next_fetch_at ELSE NULL END",
                (row["id"], row["name"], row["kind"],
                 json.dumps(row.get("config") or {}, ensure_ascii=False),
                 row.get("tier", "T2"), int(bool(row.get("first_party"))),
                 row.get("participation_mode", "editorial"),
                 str(row.get("visibility") or "public"), row.get("withdrawn_at"),
                 int(bool(row.get("site_fulltext"))),
                 int(bool(row.get("syndicate_fulltext"))),
                 int(row.get("interval_minutes", 60)), int(row.get("enabled", True)),
                 str(row.get("language") or ""), icon or None, now if icon else None,
                 now, now),
            )
        return len(rows)
    finally:
        conn.close()


def feed_sources_due(now: str, limit: int = 40) -> list[dict]:
    """到点的源，从没抓过的优先（AIHOT 的 NULLS FIRST）。

    只挑开着的。`next_fetch_at` 为空的（刚导入、或上次是版本升级前留下的）算到点。
    """
    conn = connect()
    try:
        return [_source_row(row) for row in conn.execute(
            "SELECT * FROM feed_sources WHERE enabled = 1"
            " AND (next_fetch_at IS NULL OR next_fetch_at <= ?)"
            " ORDER BY next_fetch_at IS NOT NULL, next_fetch_at ASC LIMIT ?",
            (now, limit)).fetchall()]
    finally:
        conn.close()


def feed_source_claim(source_id: str, until: str) -> None:
    """占位：抓之前先把下一次推后。

    于是同一分钟内的第二个 tick 不会把同一个源再排一遍，而抓崩了也不会把这一格永远占住
    —— 真值由 feed_source_ok / feed_source_fail 覆盖回来。
    """
    conn = connect()
    try:
        conn.execute("UPDATE feed_sources SET next_fetch_at = ?, updated_at = ? WHERE id = ?",
                     (until, now_iso(), source_id))
    finally:
        conn.close()


def feed_source_ok(source_id: str, cursor_json: str | None, next_at: str) -> None:
    """一次成功的抓取：清零失败计数，推进 cursor，排下一次。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_sources SET health = 'ok', fail_count = 0, last_error = NULL,"
            " last_fetch_at = ?, last_ok_at = ?, next_fetch_at = ?, cursor_json = COALESCE(?, cursor_json),"
            " updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), next_at, cursor_json, now_iso(), source_id))
    finally:
        conn.close()


def feed_source_fail(source_id: str, error: str, next_at: str, paid: bool = False) -> dict:
    """记一次失败，返回更新后的这一行。

    `paid` 的失败不涨 fail_count：预算花超是我们自己的事，不是源坏了。涨了就会把它的退避
    一段段拉长，最后变成一个「因为钱不够而看起来坏了」的源。1-4 次记 degraded，5 次起
    记 failing。注意 SET 里的表达式读的都是旧值，所以这里 `fail_count + 1` 与下面那个
    `fail_count + 1` 说的是同一个数 —— 新计数。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_sources SET last_fetch_at = ?, last_error = ?,"
            " fail_count = CASE WHEN ? THEN fail_count ELSE fail_count + 1 END,"
            " health = CASE WHEN ? THEN health"
            "  WHEN fail_count + 1 >= 5 THEN 'failing' ELSE 'degraded' END,"
            " next_fetch_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), (error or "")[:1000], int(paid), int(paid), next_at, now_iso(), source_id))
        row = conn.execute("SELECT * FROM feed_sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def feed_fetch_run_start(source_id: str) -> int:
    """开一次抓取的账，返回它的 id。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO feed_fetch_runs (source_id, started_at, status) VALUES (?, ?, 'running')",
            (source_id, now_iso()))
        return int(cur.lastrowid)
    finally:
        conn.close()


def feed_fetch_run_end(run_id: int, status: str, found: int, created: int,
                       error: str | None = None) -> None:
    """合上这次抓取的账。found 是「这次看到几条」，created 是「其中几条是新的」。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_fetch_runs SET finished_at = ?, status = ?, found = ?, created = ?,"
            " error = ? WHERE id = ?",
            (now_iso(), status, found, created, (error or None), run_id))
    finally:
        conn.close()


def feed_fetch_runs(limit: int = 50, source_id: str | None = None) -> list[dict]:
    """最近的抓取记录，新的在前。`found = 0` 连着几轮就是「这个源不吐了」。"""
    conn = connect()
    try:
        where, args = ("WHERE source_id = ?", [source_id]) if source_id else ("", [])
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_fetch_runs %s ORDER BY started_at DESC, id DESC LIMIT ?"
            % where, args + [limit]).fetchall()]
    finally:
        conn.close()


def _utc_stamp(value: str) -> str:
    """ISO 8601 一律归一成 UTC, 秒精度。

    `published_at` 上的比较是在 SQL 里做的, 而 SQL 比的是**字典序**: `2026-10-03T00:00:00+00:00`
    与 `2026-10-03T00:00:00+08:00` 是墙上差八小时的同一对时刻, 按字符串比大小却颠倒 —— 于是
    一个写 `+00:00` 的来源会整批落在窗口之外, 而窗口看起来只是「今天没什么东西」(实测 79 条
    里丢了 18 条)。写进来的时候归一, 比较的两边才在同一个坐标系里; `ORDER BY published_at`
    也因此才是真的按时间排。

    没有时区的写法按 UTC 读, 与 `materials.moment` 同一条规矩 —— 两个地方各有一套解法, 迟早
    会有一边读成早上八点。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return value or ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def feed_items_since(since: str) -> list[dict]:
    """窗口里的原始记录，按发表时间排。

    四个视图读的是这张表，不是源：抓一次、大家读同一份，页面才不会为了画一屏去敲四十个
    源的网络。`identity_key` 是入库那一刻算的账，读出来还要按现在的词表重算一遍 ——
    列上那句注释说的就是这件事。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_items WHERE published_at >= ? ORDER BY published_at ASC",
            (_utc_stamp(since),)).fetchall()]
    finally:
        conn.close()


def feed_items_known(source_id: str, external_ids: list[str]) -> set[str]:
    """这些 id 里哪些已经在库里。

    一处两用：算「这轮新进了几条」，以及决定要不要为它去抓细节页 —— 已经读过的不再读第二遍。
    """
    if not external_ids:
        return set()
    conn = connect()
    try:
        marks = ",".join("?" * len(external_ids))
        rows = conn.execute(
            "SELECT external_id FROM feed_items WHERE source_id = ? AND external_id IN (%s)"
            % marks, [source_id, *external_ids]).fetchall()
        return {row["external_id"] for row in rows}
    finally:
        conn.close()


def feed_items_needing_body(min_chars: int, limit: int = 40,
                            kinds: Sequence[str] = ("rss", "web_list", "json_list"),
                            before: str | None = None) -> list[dict]:
    """还没有可用正文、且这一轮该去补的那些条目，最近的在前。

    长度按**字**算：SQLite 的 `LENGTH` 对 TEXT 数字而不是字节，中文才不会被算成三倍 —— 按字节
    算的话，「只有二十来个汉字」看起来就过线了，而那正是只有一句摘要的那一批。

    `before` 是「上次试过之后隔多久才再试」的那条线，由调用方按 `detail.retryHours` 算好。
    已经死了的链接不该每轮都敲一次：每轮敲，这一轮的预算就被同一批坏链接吃光，排在后面的好
    条目永远轮不到。
    """
    conn = connect()
    try:
        marks = ",".join("?" * len(kinds))
        sql = ("SELECT * FROM feed_items WHERE source_kind IN (%s)"
               " AND LENGTH(COALESCE(body, '')) < ?" % marks)
        args: list = [*kinds, min_chars]
        if before is not None:
            sql += " AND (detail_at IS NULL OR detail_at <= ?)"
            args.append(before)
        sql += " ORDER BY seen_at DESC, published_at DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def feed_items_set_detail(rows: list[dict], tried_at: str | None = None) -> int:
    """把补来的正文（必要的话连标题）写回那一行，并记下这一次去读了原文。

    `body` / `title` 传 `None` 表示这一格不动 —— 所以「读了但没读到」也走这里：只记时刻。
    失败也要记，否则那条坏链接每一轮都会被重新排进预算。

    只写这几列：补正文不该顺手改掉 `published_at` / `seen_at`，那两个时刻说的是「源什么时候
    说的」与「我们什么时候看见的」，与我们后来又去读了一遍原文没有关系。

    补来的正文与源在清单里给的那份是同一份内容的两面，所以它也过同一个版本判定
    (AIHOT 在 `extract` 里做同一件事)：补到一处更长的正文就是新的一版，而模型之前写下的字
    因此过期，下一轮重写。
    """
    if not rows:
        return 0
    stamp = tried_at or now_iso()
    conn = connect()
    try:
        for row in rows:
            source_id = row["source_id"]
            external_id = row["external_id"]
            prev = _item_row(conn, source_id, external_id)
            sets: list[str] = []
            args: list = []
            if row.get("body") is not None:
                sets.append("body = ?")
                args.append(row["body"])
            if row.get("title") is not None:
                sets.append("title = ?")
                args.append(row["title"])
            if row.get("media") is not None:
                sets.append("media_json = ?")
                args.append(json.dumps(row["media"], ensure_ascii=False))
            if prev is not None:
                # 没传的就是不动的: 版本判定算的是"写完之后这一行长什么样".
                title = prev["title"] if row.get("title") is None else row["title"]
                body = prev["body"] if row.get("body") is None else row["body"]
                revision, digest = _next_revision(conn, prev, title, body or "",
                                                  prev["excerpt"] or "")
                sets.extend(["revision = ?", "content_hash = ?"])
                args.extend([revision, digest])
            sets.append("detail_at = ?")
            args.append(stamp)
            args.extend([source_id, external_id])
            conn.execute("UPDATE feed_items SET %s WHERE source_id = ? AND external_id = ?"
                         % ", ".join(sets), args)
            if prev is not None:
                title = prev["title"] if row.get("title") is None else row["title"]
                body = prev["body"] if row.get("body") is None else row["body"]
                if digest != (prev["content_hash"] or ""):
                    _archive_revision(conn, source_id, external_id, revision, digest,
                                      title, body or "", prev["excerpt"] or "", stamp)
        return len(rows)
    finally:
        conn.close()


# ── 分组：事件与事实 (feed/stories.py) ────────────────────────────────────────────────
#
# 三张表三个问题, 分开读而不拼一条大 SQL: 「哪些条目还没归过组」是队列, 「召回池」是判定
# 的输入, 「窗口里已经归好的事件」是页面要的东西。拼在一起写会变成一条每次都要跟着改的
# 语句, 而它们之间没有一条共同的排序。

def feed_items_for_grouping(limit: int = 60) -> list[dict]:
    """有骨架、有中文标题、还没归过组的那些行, 新的在前。

    只要求一样: 模型写过中文标题 (`title_zh`)。判定读的是"标题 + 摘要" (AIHOT 的 `reportText`),
    骨架只是这条新事实的名字 —— 空骨架 (模型漏写) 也照样该被归组, 拿它当门槛会让那些条目永远
    排在队里, 把预算占满。

    讨论帖 (`participation_mode = hot_signal`) 也走这一趟, 但出口不同 (见 `feed/stories._signal`):
    它挂到别人的事件上, 不立事件。已经挂过 (或已经问过没挂上) 的不再问第二遍。

    行已经归过组就不再问第二遍: 归组是**定夺**, 重问一遍是重做一遍决定, 而那要换提示词才该发生
    (AIHOT 的 regroup_pending)。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT i.*, s.participation_mode, s.name AS source_name,"
            " s.first_party AS source_first_party FROM feed_items i"
            " LEFT JOIN feed_item_facts m"
            "   ON m.source_id = i.source_id AND m.external_id = i.external_id"
            " LEFT JOIN feed_story_signals g"
            "   ON g.source_id = i.source_id AND g.external_id = i.external_id"
            " LEFT JOIN feed_sources s ON s.id = i.source_id"
            " WHERE i.title_zh IS NOT NULL AND i.title_zh <> ''"
            "   AND (i.analysis_scope IS NULL OR i.analysis_scope = '')"
            "   AND m.fact_id IS NULL AND g.source_id IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM feed_grouping_overrides o"
            "     WHERE o.source_id = i.source_id AND o.external_id = i.external_id)"
            " ORDER BY i.published_at DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        conn.close()


def feed_items_regroup(version: str, since: str, limit: int = 40) -> list[dict]:
    """按**旧版**分组规则定过的那些行, 老的在前 (AIHOT 的 `regroup_pending`)。

    提示词改一个字就是一个新版本, 而库里那些按旧版定的行不会自己重跑 —— 它们会一直用旧口径
    冒充新口径, 且完全静默 (`writing` 那一侧靠 `prompt_version` 的 token 治, 分组侧原先没有)。

    只取召回窗口里的那些: 重判要拿**现在的池子**当候选, 窗口外的行在池子里已经没有自己那件事
    了, 重判会把一条老报道变成一个**新**事件 —— 那是把一个决定换成另一个错。窗口外那些行保留
    旧规则下的决定, 是有意的取舍。

    `prompt_version` 为空的行 (这次改动之前定的, 或人工/测试写进去的) 不算旧版, 不动: 上线那
    一刻把全库重判一遍, 是拿一整轮的模型钱换一次没有理由的重来。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT i.*, s.participation_mode, s.name AS source_name,"
            " s.first_party AS source_first_party"
            " FROM feed_items i"
            " LEFT JOIN feed_sources s ON s.id = i.source_id"
            " LEFT JOIN feed_item_facts m"
            "   ON m.source_id = i.source_id AND m.external_id = i.external_id"
            " LEFT JOIN feed_story_signals g"
            "   ON g.source_id = i.source_id AND g.external_id = i.external_id"
            " WHERE i.title_zh IS NOT NULL AND i.title_zh <> ''"
            "   AND i.published_at >= ?"
            "   AND NOT EXISTS (SELECT 1 FROM feed_grouping_overrides o"
            "     WHERE o.source_id = i.source_id AND o.external_id = i.external_id)"
            "   AND ((m.prompt_version <> '' AND m.prompt_version <> ?)"
            "     OR (g.prompt_version <> '' AND g.prompt_version <> ?))"
            " ORDER BY i.published_at, i.source_id, i.external_id LIMIT ?",
            (_utc_stamp(since), version, version, limit)).fetchall()]
    finally:
        conn.close()


def feed_grouping_reset(keys: list[tuple[str, str]]) -> int:
    """把自动定的归属撤掉, 好让它们重新走一遍定夺 (AIHOT 的 `resetAutomatic`)。

    只撤自动记下的那两笔 —— 人工决定 (`feed_grouping_overrides`) 在查询那一侧已经被排除, 这里
    也就不可能撤到它。撤掉之后, 这一条在下一趟定夺里既不是候选也不算证据, 与 AIHOT 把重判中的
    报道标成 `regroup_pending` 是同一件事。
    """
    if not keys:
        return 0
    conn = connect()
    try:
        for source_id, external_id in keys:
            conn.execute("DELETE FROM feed_item_facts WHERE source_id = ? AND external_id = ?",
                         (source_id, external_id))
            conn.execute("DELETE FROM feed_story_signals WHERE source_id = ? AND external_id = ?",
                         (source_id, external_id))
        return len(keys)
    finally:
        conn.close()


def feed_items_unattached_signals(since: str, quoted_id: str = "", limit: int = 200) -> list[dict]:
    """窗口里「问过、当时没挂上」的讨论帖, 新的在前 (AIHOT 的 `unattachedSignal`)。

    `feed_story_signals` 那一行 `story_id` 为空 = 问过没挂上, 判定站从此不再排它 —— 于是**先到**
    的那条帖子在原帖出现之后永远掉在外面。回头捞的那一趟 (`stories._reattach`) 读的就是这一份。

    窗口按**观察时刻** (`observed_at`, 判定时归一到 UTC) 算, 与 `regroup` 用 `published_at` 同理:
    比的是字典序, 两边必须在同一个坐标系里。人工钉过的行不算 —— 人已经判过了, 机器不该把它改
    回去 (AIHOT 的 `unattachedSignal` 同样排除 `grouping_overrides`)。

    `quoted_id` 给出来就只取引用了这条的 (SQL 里先粗筛一遍, 精确的对 `/status/<id>` 在
    `stories._quotes` 里做): 等原帖的那条线要扫 48 小时, 不筛一下就成了每立一件事全表扫一遍。
    """
    query = ("SELECT i.*, s.participation_mode, s.name AS source_name,"
             " s.first_party AS source_first_party"
             " FROM feed_story_signals g"
             " JOIN feed_items i ON i.source_id = g.source_id AND i.external_id = g.external_id"
             " LEFT JOIN feed_sources s ON s.id = i.source_id"
             " WHERE g.story_id IS NULL AND g.observed_at >= ?"
             "   AND NOT EXISTS (SELECT 1 FROM feed_grouping_overrides o"
             "     WHERE o.source_id = i.source_id AND o.external_id = i.external_id)")
    args: list = [_utc_stamp(since)]
    if quoted_id:
        query += " AND i.quoted_json LIKE ?"
        args.append("%%/status/%s%%" % quoted_id)
    query += " ORDER BY g.observed_at DESC LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


#: 一条成员报道算不算**证据** (AIHOT 的 `recall.trusted`): 人工钉过的一直算; 还在等重判的那些
#: (定它的那版规则已经不是现在这一版) 不算 —— 它自己的归属还没重新决定, 拿它去给别人当证据,
#: 等于把旧口径借着别人再抄一遍。`prompt_version` 为空的行是这套账之前定的, 判不出来, 按老行算。
_TRUSTED_EVIDENCE = (
    " AND (EXISTS (SELECT 1 FROM feed_grouping_overrides o"
    "      WHERE o.source_id = m.source_id AND o.external_id = m.external_id)"
    "  OR m.prompt_version = '' OR m.prompt_version = ?)")


def feed_fact_pool(since: str, limit: int = 400,
                   evidence_version: str = "") -> tuple[list[dict], list[dict]]:
    """召回池: (事实, 成员报道)。

    事实按 `at` 新到旧取一段, 成员报道再按 (事实, 发布时间) 排 —— 每个事实的第一条就是它的
    代表报道 (AIHOT 的 reportView)。两条查询而不是一条 JOIN: 一条事实有几个成员就有几行,
    拼成一条之后"取每个事实的第一条"要靠窗口函数, 而这一层读的是几百行。

    `evidence_version` 给的是**现在这一版**分组规则: 给了它, 等重判的那些就不算证据
    (`_TRUSTED_EVIDENCE`)。不给就是不筛 —— 老的调用方与测试按原样读全量。
    """
    conn = connect()
    try:
        facts = [dict(row) for row in conn.execute(
            "SELECT f.*, s.title AS story_title FROM feed_facts f"
            " JOIN feed_stories s ON s.id = f.story_id"
            " WHERE f.at >= ? ORDER BY f.at DESC LIMIT ?",
            (_utc_stamp(since), limit)).fetchall()]
        params: list = [_utc_stamp(since)]
        where = " WHERE f.at >= ?"
        if evidence_version:
            where += _TRUSTED_EVIDENCE
            params.append(evidence_version)
        members = [dict(row) for row in conn.execute(
            "SELECT m.fact_id, m.role, r.source_id, r.external_id, r.url, r.title,"
            " r.title_zh, r.summary_llm, r.published_at, src.name AS source, src.first_party"
            " FROM feed_item_facts m"
            " JOIN feed_items r ON r.source_id = m.source_id AND r.external_id = m.external_id"
            " JOIN feed_facts f ON f.id = m.fact_id"
            " LEFT JOIN feed_sources src ON src.id = r.source_id"
            + where +
            " ORDER BY m.fact_id ASC, r.published_at ASC",
            tuple(params)).fetchall()]
        return facts, members
    finally:
        conn.close()


def feed_story_members(since: str) -> list[dict]:
    """窗口里已经归好的事件, 一行一个成员报道 —— 页面据此重新分桶。

    `s.last_at >= since` 而不是每个成员各自比: 事件的窗口是**这件事**的窗口, 一件三个月前的
    事今天有了新进展, 它就该出现在今天的窗口里, 而它最早的报道不该被切掉。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT DISTINCT s.id AS story_id, s.title AS story_title, s.digest, s.latest,"
            " s.status, s.first_at, s.last_at, m.source_id, m.external_id, m.relation"
            " FROM feed_stories s"
            " JOIN feed_facts f ON f.story_id = s.id"
            " JOIN feed_item_facts m ON m.fact_id = f.id"
            " WHERE s.last_at >= ?"
            " ORDER BY s.last_at DESC, s.id DESC", (_utc_stamp(since),)).fetchall()]
    finally:
        conn.close()


def feed_story_leader(story_id: int) -> int:
    """一条故事的**存活** id (AIHOT 的别名表): 合并过就一路跟到那个还在的头上。

    我们的故事键是 `s:<id>` 这种会被人收藏/贴出去的字符串, 所以合并之后旧 id 必须还能落到新事上 ——
    否则一条被合并的事在页面上会变成 404, 而它只是换了名字。
    """
    conn = connect()
    try:
        seen: set[int] = set()
        current = int(story_id)
        while current not in seen:
            seen.add(current)
            row = conn.execute("SELECT merged_into FROM feed_stories WHERE id = ?",
                               (current,)).fetchone()
            if row is None or row["merged_into"] is None:
                return current
            current = int(row["merged_into"])
        return current
    finally:
        conn.close()


def feed_story_merge(from_id: int, into_id: int) -> dict | None:
    """把一件事并进另一件 (AIHOT 的 `mergeStoryInto`): 事实与讨论证据搬过去, 旧 id 留成别名。

    搬的是**事实归属**而不是报道: 一条报道属于哪个事实不变, 变的是那个事实现在算在哪件事下。
    `from` 那件事保留一行、`merged_into` 指过去 —— 删掉它, 那些已经贴出去的 `s:<id>` 就没地方落。
    """
    if from_id == into_id:
        raise ValueError("cannot merge a story into itself")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = [dict(row) for row in conn.execute(
            "SELECT id, merged_into, first_at, last_at FROM feed_stories"
            " WHERE id IN (?, ?)", (from_id, into_id)).fetchall()]
        found = {row["id"]: row for row in rows}
        if len(found) < 2 or found[from_id]["merged_into"] is not None \
                or found[into_id]["merged_into"] is not None:
            conn.execute("ROLLBACK")
            return None
        moved = conn.execute("UPDATE feed_facts SET story_id = ? WHERE story_id = ?",
                            (into_id, from_id)).rowcount
        conn.execute("UPDATE OR IGNORE feed_story_signals SET story_id = ? WHERE story_id = ?",
                     (into_id, from_id))
        conn.execute("DELETE FROM feed_story_signals WHERE story_id = ?", (from_id,))
        conn.execute("UPDATE OR IGNORE feed_story_ties SET story_id = ? WHERE story_id = ?",
                     (into_id, from_id))
        conn.execute("DELETE FROM feed_story_ties WHERE story_id = ?", (from_id,))
        conn.execute("DELETE FROM feed_story_links WHERE story_id IN (?, ?) OR other_id IN (?, ?)",
                     (from_id, into_id, from_id, into_id))
        conn.execute("UPDATE feed_stories SET merged_into = ? WHERE id = ?", (into_id, from_id))
        source = found[from_id]
        conn.execute(
            "UPDATE feed_stories SET first_at = CASE WHEN first_at = '' OR first_at > ? THEN ?"
            " ELSE first_at END, last_at = CASE WHEN last_at < ? THEN ? ELSE last_at END"
            " WHERE id = ?",
            (source["first_at"], source["first_at"], source["last_at"], source["last_at"],
             into_id))
        conn.execute("COMMIT")
        return {"moved": moved}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def feed_story_ties_set(rows: list[dict]) -> int:
    """记下"这一条报道被模型牢牢绑到了哪些事上" (AIHOT 的 `grouping_decisions.candidates`)。

    判定一次已经知道全部候选的关系了, 只是从前只用了被选中的那一条。绑到两件事上的报道说明
    那两件事可能是一件 —— `consolidate` 就是拿这批边去找的。
    """
    if not rows:
        return 0
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO feed_story_ties (source_id, external_id, story_id, relation, confidence, at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source_id, external_id, story_id) DO UPDATE SET"
            " relation = excluded.relation, confidence = excluded.confidence, at = excluded.at",
            [(row["source_id"], row["external_id"], int(row["story_id"]),
              row.get("relation") or "", float(row.get("confidence") or 0),
              row.get("at") or "") for row in rows])
        return len(rows)
    finally:
        conn.close()


def feed_story_tie_pairs(since: str, limit: int = 200) -> list[dict]:
    """被**同一条报道**绑在一起的两件事 (AIHOT 的 `consolidate(storyIds)`)。

    一条边只要一条报道就够"值得比一比"; "该不该互为相关"要两条报道, 那是另一个问题
    (`feed_story_link_pairs`)。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT t1.story_id AS x, t2.story_id AS y, t1.source_id, t1.external_id,"
            " t1.relation AS first_relation, t1.confidence AS first_confidence,"
            " t2.relation AS second_relation, t2.confidence AS second_confidence"
            " FROM feed_story_ties t1 JOIN feed_story_ties t2"
            "   ON t2.source_id = t1.source_id AND t2.external_id = t1.external_id"
            "  AND t2.story_id > t1.story_id"
            " JOIN feed_stories a ON a.id = t1.story_id AND a.merged_into IS NULL"
            " JOIN feed_stories b ON b.id = t2.story_id AND b.merged_into IS NULL"
            " WHERE t1.at >= ? ORDER BY t1.story_id, t2.story_id LIMIT ?",
            (_utc_stamp(since), limit)).fetchall()]
    finally:
        conn.close()


def feed_story_root_report(story_id: int) -> dict | None:
    """一件事的根事实 + 它的代表报道 (AIHOT 的 `storyRoot`)。

    根事实是这件事**开始**的地方, 合并判定比的就是两个根 —— 拿最新的那条比, 一场后续进展会被
    当成一件新事, 而它们本来就是同一件的两段。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT f.id AS fact_id, f.story_id, f.title AS fact_title, f.subject, f.action,"
            " f.object, f.occurred_at, f.at AS fact_at, i.source_id, i.external_id, i.title,"
            " i.title_zh, i.summary_llm, i.published_at, src.name AS source, src.first_party,"
            " (SELECT count(*) FROM feed_item_facts r WHERE r.fact_id = f.id"
            "         AND r.role IN ('primary', 'report')) AS members,"
            " EXISTS (SELECT 1 FROM feed_item_facts r WHERE r.fact_id = f.id"
            "         AND r.relation = 'roundup') AS roundup"
            " FROM feed_facts f"
            " JOIN feed_item_facts m ON m.fact_id = f.id"
            " JOIN feed_items i ON i.source_id = m.source_id AND i.external_id = m.external_id"
            " LEFT JOIN feed_sources src ON src.id = i.source_id"
            " WHERE f.story_id = ? AND f.root = 1 AND m.role IN ('primary', 'report')"
            " ORDER BY i.published_at ASC, i.source_id ASC LIMIT 1", (story_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_story_link_pairs(since: str, min_reports: int = 2) -> list[dict]:
    """该互为「相关事件」的两件事: **至少两条**报道把它们绑在一起 (AIHOT 的 `linkRelatedStories`)。

    一条边太容易是"模型在一串候选里顺口说了一句", 两件不相干的事就会因此互相挂上 —— 而挂错的
    代价是读者以为它们是一件。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT t1.story_id AS x, t2.story_id AS y, count(DISTINCT t1.source_id || '|'"
            " || t1.external_id) AS reports"
            " FROM feed_story_ties t1 JOIN feed_story_ties t2"
            "   ON t2.source_id = t1.source_id AND t2.external_id = t1.external_id"
            "  AND t2.story_id > t1.story_id"
            " JOIN feed_stories a ON a.id = t1.story_id AND a.merged_into IS NULL"
            " JOIN feed_stories b ON b.id = t2.story_id AND b.merged_into IS NULL"
            " WHERE t1.at >= ? AND t1.relation IN ('same_occurrence', 'same_story')"
            "   AND t2.relation IN ('same_occurrence', 'same_story')"
            " GROUP BY t1.story_id, t2.story_id HAVING reports >= ?",
            (_utc_stamp(since), int(min_reports))).fetchall()]
    finally:
        conn.close()


def feed_story_links_add(pairs: list[tuple[int, int]]) -> int:
    """两件事互相记成「相关」. 只加不减: 合并掉的那一头在读取时自然落下去。"""
    if not pairs:
        return 0
    stamp = now_iso()
    rows = [(a, b) for a, b in pairs] + [(b, a) for a, b in pairs]
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO feed_story_links (story_id, other_id, relation, created_at)"
            " VALUES (?, ?, 'related', ?) ON CONFLICT (story_id, other_id) DO NOTHING",
            [(a, b, stamp) for a, b in rows])
        return len(rows)
    finally:
        conn.close()


def feed_story_links(since: str) -> list[dict]:
    """窗口里那些「相关事件」的边, 两头都还在的才给."""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT l.story_id, l.other_id, o.title AS other_title, o.last_at AS other_last_at"
            " FROM feed_story_links l"
            " JOIN feed_stories s ON s.id = l.story_id AND s.merged_into IS NULL"
            " JOIN feed_stories o ON o.id = l.other_id AND o.merged_into IS NULL"
            " WHERE s.last_at >= ? ORDER BY l.story_id, o.last_at DESC",
            (_utc_stamp(since),)).fetchall()]
    finally:
        conn.close()


def feed_story_links_for(story_id: int, limit: int = 8) -> list[dict]:
    """一件事的「关联事件」(AIHOT 的 `relatedStories`): 对面还活着、而且还有人在报.

    对面那一头还要有报道: 一条指向某件已经没人报过的事的边, 点进去是一页空的. AIHOT 的
    条件是"对面还有一条公开报道", 这里是同一个意思的可用版本 (有没有报道由成员表数得出来).
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT l.other_id, l.relation, o.title, o.last_at FROM feed_story_links l"
            " JOIN feed_stories o ON o.id = l.other_id AND o.merged_into IS NULL"
            " WHERE l.story_id = ? AND l.other_id <> l.story_id"
            " AND (SELECT count(*) FROM feed_facts f JOIN feed_item_facts m ON m.fact_id = f.id"
            "      WHERE f.story_id = o.id) > 0"
            " ORDER BY o.last_at DESC, o.id DESC LIMIT ?",
            (story_id, int(limit))).fetchall()]
    finally:
        conn.close()


def feed_story_invalidate(story_id: int, title: str) -> None:
    """输入变了: 派生文字当场作废 (AIHOT 的 `invalidateStoryInputs`)。

    清掉的是**结论**: 标题、综述、最新进展、指纹。不清归属, 也不碰报道 —— 下一轮综述站会按新的
    输入重写; 而留着旧结论配新成员, 是页面上说了一件没有人再算过的事。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_stories SET title = ?, digest = '', latest = '', digest_inputs = '',"
            " digest_ids = '' WHERE id = ?", (title or "", story_id))
    finally:
        conn.close()


def feed_overrides_all() -> list[dict]:
    """所有人工改过的分组决定 (AIHOT 的 `grouping_overrides`)。自动分组读到就跳过这条。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_grouping_overrides ORDER BY created_at DESC").fetchall()]
    finally:
        conn.close()


def feed_override_set(row: dict) -> None:
    """记下一条人工决定。自动分组、重试与后续修订都不推翻它 —— 只有显式「重新分组」才清。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_grouping_overrides (source_id, external_id, reason, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (source_id, external_id) DO UPDATE SET"
            " reason = excluded.reason, actor = excluded.actor, created_at = excluded.created_at",
            (row["source_id"], row["external_id"], row.get("reason") or "",
             row.get("actor") or "", row.get("created_at") or now_iso()))
    finally:
        conn.close()


def feed_override_get(source_id: str, external_id: str) -> dict | None:
    """这一条有没有人工决定。

    写入前再读一次 (AIHOT 的 `manualDecision` 在事务里重读): 判定跑一轮要几十秒, 人可能
    就在这中间点了「放在这里」—— 那一票不能被随后算完的模型答案盖掉。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM feed_grouping_overrides WHERE source_id = ? AND external_id = ?",
            (source_id, external_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_override_clear(source_id: str, external_id: str) -> None:
    """显式的「重新分组」: 把那条人工决定拿掉, 让自动分组再判一次。"""
    conn = connect()
    try:
        conn.execute("DELETE FROM feed_grouping_overrides WHERE source_id = ? AND external_id = ?",
                     (source_id, external_id))
    finally:
        conn.close()


def feed_story_new(row: dict) -> int:
    """开一件事。返回它的 id (AIHOT 的 createStory)。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO feed_stories (title, first_at, last_at, created_at)"
            " VALUES (?, ?, ?, ?)",
            (row.get("title") or "", row.get("at") or "", row.get("at") or "",
             row.get("created_at") or now_iso()))
        return int(cur.lastrowid)
    finally:
        conn.close()


def feed_story_touch(story_id: int, at: str) -> None:
    """把一件事的 `last_at` 推到最新那一次发生上, 并补 `first_at`。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_stories SET last_at = CASE WHEN last_at < ? THEN ? ELSE last_at END,"
            " first_at = CASE WHEN first_at = '' OR first_at > ? THEN ? ELSE first_at END"
            " WHERE id = ?", (_utc_stamp(at), _utc_stamp(at), _utc_stamp(at), _utc_stamp(at),
                             story_id))
    finally:
        conn.close()


#: 一件事下面有几条可用的报道 ("有报道"这件事的过滤器, 不是输入哈希).
_REPORTS_SUBQUERY = (
    " (SELECT count(*) FROM feed_facts f JOIN feed_item_facts m ON m.fact_id = f.id"
    "  WHERE f.story_id = s.id)")


def feed_stories_for_digest(since: str, limit: int = 0) -> list[dict]:
    """窗口内、有报道的那些事 —— 连带上次写综述时的输入指纹与 id 集合。

    该不该重写由**输入指纹**说了算, 不是条数 (AIHOT 的 `digestInputsHash`): 条数一样而某条报道
    换了正文、条数一样而换了一条报道, 条数都看不出来, 哈希看得出来 —— 而这两种情况下旧综述说的
    已经不是这件事现在的样子。指纹要按当前报道重算, SQL 算不出来, 所以这里只筛"窗口内 + 有
    报道", 比较放在 `feed/digest.py` 里。

    `limit` 是**候选**上限 (0 = 全给): 真正该花几次模型钱由调用方按预算数, 不是这里数。
    """
    conn = connect()
    try:
        sql = ("SELECT s.id, s.title, s.last_at, s.digest, s.digest_inputs, s.digest_ids,"
               + _REPORTS_SUBQUERY + " AS reports FROM feed_stories s"
               " WHERE s.last_at >= ? AND" + _REPORTS_SUBQUERY + " > 0"
               " ORDER BY s.last_at DESC")
        args: list = [_utc_stamp(since)]
        if limit > 0:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def feed_story_reports(story_id: int, limit: int = 0) -> list[dict]:
    """一件事下面的报道，按时间排 (AIHOT 的 `digestReports`)。

    `role` 只认 primary / report: mention 是"顺带提了一句"的复合分析, 拿它当材料会让综述里
    长出一件从来没发生过的事。

    `limit` 只在读**材料**时用得上 (取最新那些 —— 更早的转折已经写进上一版综述里了); 算输入
    指纹要的是**全部**报道, 所以默认 0 = 全给 (AIHOT 也是先拿全量、再 `slice(-40)`)。
    返回的每一行带 `external_id`: `源|外号` 就是它的身份, 指纹与增量都要这个。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT i.source_id, i.external_id, i.published_at, i.title, i.title_zh, i.summary_llm"
            " FROM feed_item_facts m"
            " JOIN feed_facts f ON f.id = m.fact_id"
            " JOIN feed_items i ON i.source_id = m.source_id AND i.external_id = m.external_id"
            " WHERE f.story_id = ? AND m.role IN ('primary', 'report')"
            " ORDER BY i.published_at ASC, i.source_id ASC", (story_id,)).fetchall()
    finally:
        conn.close()
    items = [dict(row) for row in rows]
    return items[-limit:] if limit > 0 else items


def feed_story_digests(since: str) -> list[dict]:
    """窗口里写过综述的那些事。读取侧据此把模型写的那段话盖到规则版上面。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT id AS story_id, title, digest, latest, status FROM feed_stories"
            " WHERE last_at >= ? AND digest <> '' ORDER BY last_at DESC",
            (_utc_stamp(since),)).fetchall()]
    finally:
        conn.close()


def feed_story_digest_set(story_id: int, row: dict) -> None:
    """把模型写的事件综述写回去 (AIHOT 的 composeStoryDigest): 文字 + 这一次的输入指纹。

    指纹与 id 集合跟文字一起落笔 —— 只有它们和这段文字是同一批输入时, 下一次才敢说"输入没变"。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_stories SET title = ?, digest = ?, latest = ?, status = ?,"
            " digest_inputs = ?, digest_ids = ? WHERE id = ?",
            (row.get("title") or "", row.get("digest") or "", row.get("latest") or "",
             row.get("status") or "active", row.get("digest_inputs") or "",
             row.get("digest_ids") or "", story_id))
    finally:
        conn.close()


def feed_story_signal_members(since: str) -> list[dict]:
    """窗口里已经挂上的讨论证据, 一行一个成员帖子 —— 与报道一起交给重新分桶。

    只为真挂上的那些答得出来: `story_id` 为空的那一行意思是"问过了, 没挂上"
    (AIHOT 的 `signal-unmatched`), 它不属于任何一件事。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT s.id AS story_id, s.title AS story_title, s.digest,"
            " s.latest, s.status, s.first_at, s.last_at, g.source_id, g.external_id,"
            " 'signal' AS relation"
            " FROM feed_story_signals g"
            " JOIN feed_stories s ON s.id = g.story_id"
            " WHERE g.story_id IS NOT NULL AND s.last_at >= ?"
            " ORDER BY s.last_at DESC, s.id DESC", (_utc_stamp(since),)).fetchall()]
    finally:
        conn.close()


def feed_story_signal_set(rows: list[dict]) -> int:
    """把一条讨论帖挂到某一件事上。`story_id` 为空就是那一笔"没挂上" (AIHOT 的 `recordSignal`)。"""
    if not rows:
        return 0
    stamp = now_iso()
    conn = connect()
    try:
        for row in rows:
            story = row.get("story_id")
            conn.execute(
                "INSERT INTO feed_story_signals (source_id, external_id, story_id, kind,"
                " observed_at, prompt_version) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, external_id) DO UPDATE SET"
                " story_id = excluded.story_id, kind = excluded.kind,"
                " observed_at = excluded.observed_at,"
                " prompt_version = excluded.prompt_version",
                (row["source_id"], row["external_id"], int(story) if story else None,
                 row.get("kind") or "signal", row.get("observed_at") or stamp,
                 row.get("prompt_version") or ""))
        return len(rows)
    finally:
        conn.close()


def feed_fact_new(row: dict) -> int:
    """在某一件事下面记一次发生。返回它的 id (AIHOT 的 createFact)。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO feed_facts (story_id, title, subject, action, object, occurred_at,"
            " at, root, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(row["story_id"]), row.get("title") or "", row.get("subject") or "",
             row.get("action") or "", row.get("object") or "", row.get("occurred_at"),
             _utc_stamp(row.get("at") or now_iso()), 1 if row.get("root") else 0,
             row.get("created_at") or now_iso()))
        return int(cur.lastrowid)
    finally:
        conn.close()


def feed_item_facts_set(rows: list[dict]) -> int:
    """把一条条报道钉到它的事实上。主键是报道, 所以重跑覆盖的是同一条 (AIHOT 的 fact_articles)。"""
    if not rows:
        return 0
    stamp = now_iso()
    conn = connect()
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO feed_item_facts (source_id, external_id, fact_id, role, relation,"
                " confidence, note, decided_at, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, external_id) DO UPDATE SET"
                " fact_id = excluded.fact_id, role = excluded.role,"
                " relation = excluded.relation, confidence = excluded.confidence,"
                " note = excluded.note, decided_at = excluded.decided_at,"
                " prompt_version = excluded.prompt_version",
                (row["source_id"], row["external_id"], int(row["fact_id"]),
                 row.get("role") or "report", row.get("relation") or "",
                 float(row.get("confidence") or 0), row.get("note") or "",
                 row.get("decided_at") or stamp, row.get("prompt_version") or ""))
        return len(rows)
    finally:
        conn.close()


def feed_fact_root(story_id: int) -> dict | None:
    """一件事的根事实 (AIHOT 的 rootFactOf)。后续只挂它。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM feed_facts WHERE story_id = ? AND root = 1"
            " ORDER BY at ASC, id ASC LIMIT 1", (story_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_group_clear() -> None:
    """把分组结果清空 —— 换提示词/词表之后要重来一遍时用它。条目本身一条不动。"""
    conn = connect()
    try:
        conn.execute("DELETE FROM feed_item_facts")
        conn.execute("DELETE FROM feed_facts")
        conn.execute("DELETE FROM feed_stories")
        conn.execute("DELETE FROM feed_story_ties")
        conn.execute("DELETE FROM feed_story_links")
        conn.execute("DELETE FROM feed_grouping_overrides")
    finally:
        conn.close()


def feed_stories_all() -> list[dict]:
    """所有事件，新的在前。抽查与测试读它 —— 页面不走这一条（那条按窗口读成员）。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_stories ORDER BY last_at DESC, id DESC").fetchall()]
    finally:
        conn.close()


def feed_story_evidence_times(story_id: int, since: str, until: str) -> list[dict]:
    """一件事在 `[since, until]` 里每条证据的 `(source_id, observed_at)` (AIHOT `heatSeries` 读的那批).

    报道与讨论帖一起给: 热度数的是"这一刻几家在说这件事", 少一边这条线就比卡片上的数矮一截。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT i.source_id, i.published_at AS observed_at"
            " FROM feed_item_facts m"
            " JOIN feed_facts f ON f.id = m.fact_id"
            " JOIN feed_items i ON i.source_id = m.source_id AND i.external_id = m.external_id"
            " WHERE f.story_id = ? AND m.role IN ('primary', 'report')"
            " AND i.published_at >= ? AND i.published_at <= ?"
            " UNION ALL"
            " SELECT g.source_id, g.observed_at AS observed_at FROM feed_story_signals g"
            " WHERE g.story_id = ? AND g.observed_at >= ? AND g.observed_at <= ?",
            (story_id, since, until, story_id, since, until)).fetchall()]
    finally:
        conn.close()


def feed_story_evidence_span(story_id: int) -> dict | None:
    """一件事的第一条与最后一条证据 (AIHOT `backfillStoryHeat` 的头两行). 没有证据答 None。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT min(observed_at) AS first_at, max(observed_at) AS last_at FROM ("
            " SELECT i.published_at AS observed_at FROM feed_item_facts m"
            " JOIN feed_facts f ON f.id = m.fact_id"
            " JOIN feed_items i ON i.source_id = m.source_id AND i.external_id = m.external_id"
            " WHERE f.story_id = ? AND m.role IN ('primary', 'report')"
            " UNION ALL"
            " SELECT g.observed_at AS observed_at FROM feed_story_signals g WHERE g.story_id = ?"
            ")", (story_id, story_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_item_facts_all() -> list[dict]:
    """所有「报道 → 事实」的归属关系。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_item_facts ORDER BY decided_at DESC").fetchall()]
    finally:
        conn.close()

# ------------------------------------------------------------------ heat snapshots
# 每小时的热度与每次算出来的榜单都落库 (AIHOT 的 `story_heat_hourly` / `hot_rankings`): 页面上
# 那条线和那个名次读的是**同一份写下来的结果**, 不是各请求各算一遍 —— 两个地方各算一次, 迟
# 早会算出两个数。


def feed_heat_snapshot_set(hour: str, rows: list[dict]) -> int:
    """写某一小时的热度快照 (AIHOT 的 `saveHeat`)。

    键是 `(story_id, hour)`, 重写时五个派生列一起换 —— 落后源追上来重算时, `heat` 和
    `complete` 必须同时是对的, 只改一个就会留下一条自相矛盾的记录。
    """
    if not rows:
        return 0
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO feed_story_heat_hourly"
            " (story_id, hour, heat, participants, cohort, complete)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (story_id, hour) DO UPDATE SET heat = excluded.heat,"
            " participants = excluded.participants, cohort = excluded.cohort,"
            " complete = excluded.complete",
            [(row["story_id"], hour, float(row["heat"]), int(row["participants"]),
              int(row.get("cohort") or 0), 1 if row.get("complete", True) else 0)
             for row in rows])
        return len(rows)
    finally:
        conn.close()


def feed_heat_hourly_stale(since: str, before: str) -> list[dict]:
    """`[since, before)` 里没补完的小时, 每个小时带它那批事的键 (AIHOT `snapshotHeat` 的尾段)。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT hour, story_id FROM feed_story_heat_hourly"
            " WHERE complete = 0 AND hour >= ? AND hour < ?"
            " ORDER BY hour ASC, story_id ASC", (since, before)).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["hour"], []).append(row["story_id"])
        return [{"hour": hour, "story_ids": ids} for hour, ids in grouped.items()]
    finally:
        conn.close()


def feed_heat_hourly(story_ids: list[str], since: str, until: str) -> list[dict]:
    """一批事在 `[since, until]` 里**完整**那些小时的热度, 旧的在前 (AIHOT 的 `sparklines`)。

    只认 `complete = 1` 的小时: 一个当时有源在落后的格子画出来就是一条假的低谷 (曲线读的
    是"那一刻大家都报完了没有", 不是"我们那一刻收到了多少")。
    """
    if not story_ids:
        return []
    marks = ",".join("?" for _ in story_ids)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT story_id, hour, heat, participants FROM feed_story_heat_hourly"
            " WHERE complete = 1 AND story_id IN (%s) AND hour >= ? AND hour <= ?"
            " ORDER BY hour ASC, story_id ASC" % marks,
            (*story_ids, since, until)).fetchall()]
    finally:
        conn.close()


def feed_heat_prune(before: str) -> int:
    """删掉 `before` 以前的小时快照 —— 曲线只画 7 天, 更早的行没有人读。"""
    conn = connect()
    try:
        return conn.execute(
            "DELETE FROM feed_story_heat_hourly WHERE hour < ?", (before,)).rowcount or 0
    finally:
        conn.close()


def feed_hot_ranking_new(row: dict) -> int:
    """落一份榜单快照 (AIHOT `computeHotRanking` 的那句 INSERT)。`entries_json` 是整张榜。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO feed_hot_rankings"
            " (computed_at, rule_version, entries_json, evidence_json, published)"
            " VALUES (?, ?, ?, ?, ?)",
            (row["computed_at"], row["rule_version"], row.get("entries_json") or "[]",
             row.get("evidence_json") or "{}", 1 if row.get("published", True) else 0))
        return int(cur.lastrowid)
    finally:
        conn.close()


def feed_hot_ranking_latest() -> dict | None:
    """最新一份**已发布**的榜单 (AIHOT 的 `latestHotRanking`)。页面只读这一条。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM feed_hot_rankings WHERE published = 1"
            " ORDER BY computed_at DESC, id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_hot_ranking_prune(before: str) -> int:
    """删掉 `before` 以前的榜单 —— 历史只留 30 天 (AIHOT 那句 `interval '30 days'`)。"""
    conn = connect()
    try:
        return conn.execute(
            "DELETE FROM feed_hot_rankings WHERE computed_at < ?", (before,)).rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 可见性 (feed/scope.py)

def feed_item_withdraw(source_id: str, external_id: str, reason: str = "") -> bool:
    """一条不再公开。行不删 —— 已经转发出去的链接要能解释为什么没了。

    返回“这一次真的改了”: 重复下架不会把时间戳刷新 —— 否则审计里那一笔
    会说“刚刚下架的”, 而它其实是上个月下架的。
    """
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE feed_items SET withdrawn_at = ?"
            " WHERE source_id = ? AND external_id = ? AND withdrawn_at IS NULL",
            (now_iso(), source_id, external_id))
        return bool(cursor.rowcount)
    finally:
        conn.close()


def feed_item_restore(source_id: str, external_id: str) -> bool:
    """下架的反向操作: 把那一格清回去。"""
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE feed_items SET withdrawn_at = NULL"
            " WHERE source_id = ? AND external_id = ? AND withdrawn_at IS NOT NULL",
            (source_id, external_id))
        return bool(cursor.rowcount)
    finally:
        conn.close()


def feed_item_set_seo(source_id: str, external_id: str, *, indexed: bool,
                      at: str | None = None) -> None:
    """编辑手动收录 / 排除 (AIHOT 的 `seo_indexed_at` / `seo_excluded_at`)。

    两格互斥: 收录一条就清掉排除, 反之亦然。一行两个标记同时在,
    页面就得猜哪个算数。
    """
    stamp = at or now_iso()
    conn = connect()
    try:
        if indexed:
            conn.execute("UPDATE feed_items SET seo_indexed_at = ?, seo_excluded_at = NULL"
                         " WHERE source_id = ? AND external_id = ?",
                         (stamp, source_id, external_id))
        else:
            conn.execute("UPDATE feed_items SET seo_excluded_at = ?, seo_indexed_at = NULL"
                         " WHERE source_id = ? AND external_id = ?",
                         (stamp, source_id, external_id))
    finally:
        conn.close()


def feed_audit_add(row: dict) -> int:
    """记一笔审计 (feed/audit.py)。"""
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO feed_audit (at, actor, action, subject, detail_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (row.get("at") or now_iso(), row.get("actor") or "", row["action"],
             row.get("subject") or "",
             json.dumps(row.get("detail") or {}, ensure_ascii=False)))
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def feed_audit_recent(limit: int = 50, *, subject: str = "", action: str = "") -> list[dict]:
    """最近的几笔审计。可以按被改的东西或动作筛。"""
    where, params = [], []
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if action:
        where.append("action = ?")
        params.append(action)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM feed_audit%s ORDER BY at DESC, id DESC LIMIT ?" % clause,
            [*params, limit]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except ValueError:
                item["detail"] = {}
            out.append(item)
        return out
    finally:
        conn.close()


# --- 定时活儿 (feed/jobs.py) ------------------------------------------------------------
#
# 一张状态表加一张流水表, 和 `feed_sources` / `feed_fetch_runs` 是同一个分法:
# `feed_jobs` 回答"这一槽该不该跑" (状态), `feed_job_runs` 回答"上一次跑成什么样" (流水)。
# 状态与流水分开是因为它们的读法不同: 判到点这件事每分钟问一次, 而流水是给人查的。


def feed_jobs_all() -> list[dict]:
    """每个登记过的活儿, 连同它上一次跑在哪个槽。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM feed_jobs ORDER BY name ASC").fetchall()]
    finally:
        conn.close()


def feed_jobs_put(rows: list[dict]) -> int:
    """登记这些活儿 —— 只写"它叫什么、多久一次、开没开", 运行态一列不碰。

    和源清单同一个道理: 进程每次起来都对一遍, 是为了让"表里少了一个活儿"看得见;
    而 `last_slot` 是程序写的, 每次重写等于每起一次进程就把所有活儿都当没跑过。
    """
    if not rows:
        return 0
    conn = connect()
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO feed_jobs (name, cadence_minutes, enabled) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET cadence_minutes = excluded.cadence_minutes,"
                " enabled = excluded.enabled",
                (row["name"], int(row["cadence_minutes"]), int(row.get("enabled", True))))
        return len(rows)
    finally:
        conn.close()


def feed_job_run_start(name: str, slot: str) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO feed_job_runs (name, slot, started_at, status)"
            " VALUES (?, ?, ?, 'running')", (name, slot, now_iso()))
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def feed_job_run_end(run_id: int, status: str, detail: dict | None = None,
                     error: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_job_runs SET finished_at = ?, status = ?, detail_json = ?, error = ?"
            " WHERE id = ?",
            (now_iso(), status, json.dumps(detail or {}, ensure_ascii=False),
             error[:400] or None, run_id))
    finally:
        conn.close()


def feed_job_mark(name: str, slot: str, status: str, error: str = "") -> None:
    """这个活儿这一槽有着落了。`last_slot` 是判"下一槽该不该跑"的唯一依据。

    "跑过了"与"跳过了"都写它 —— 两者都表示这一槽不必再来一次, 差别只在流水那条记录上。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_jobs SET last_slot = ?, last_run_at = ?, last_status = ?,"
            " last_error = ? WHERE name = ?",
            (slot, now_iso(), status, error[:400] or None, name))
    finally:
        conn.close()


def feed_job_runs(limit: int = 50, name: str | None = None) -> list[dict]:
    """最近的几趟。按活儿的名字可以只看一个。"""
    conn = connect()
    try:
        if name:
            rows = conn.execute(
                "SELECT * FROM feed_job_runs WHERE name = ? ORDER BY slot DESC, id DESC LIMIT ?",
                (name, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feed_job_runs ORDER BY slot DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except ValueError:
                item["detail"] = {}
            out.append(item)
        return out
    finally:
        conn.close()


def feed_job_runs_prune(before: str) -> int:
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_job_runs WHERE slot < ?", (before,)).rowcount or 0
    finally:
        conn.close()


def feed_fetch_runs_prune(before: str) -> int:
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_fetch_runs WHERE started_at < ?",
                            (before,)).rowcount or 0
    finally:
        conn.close()


def feed_audit_prune(before: str) -> int:
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_audit WHERE at < ?", (before,)).rowcount or 0
    finally:
        conn.close()


# --- 来源图标 (feed/sources/icons.py) ----------------------------------------------------


def feed_sources_set_icon(source_id: str, icon_url: str, checked_at: str) -> None:
    """记下这一家找图标的结论。空串也是结论 —— "找过了, 没有"。

    `checked_at` 一起写, 所以空手而归也要等一个重试周期, 不是每一轮都去敲一次对方。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_sources SET icon_url = ?, icon_checked_at = ?, updated_at = ?"
            " WHERE id = ?", (icon_url or None, checked_at, now_iso(), source_id))
    finally:
        conn.close()


def feed_sources_need_icon(before: str, limit: int = 40) -> list[dict]:
    """该去找图标的源: 还没有图标, 而且够久没试过。

    清单里自己写了 `icon:` 的不会被挑中 (导入时记了 `icon_checked_at`), 所以这里挑出来的
    都是真的还没有脸的。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM feed_sources WHERE enabled = 1"
            " AND (icon_url IS NULL OR icon_url = '')"
            " AND (icon_checked_at IS NULL OR icon_checked_at < ?)"
            " ORDER BY id ASC LIMIT ?", (before, limit)).fetchall()
        return [_source_row(row) for row in rows]
    finally:
        conn.close()


def feed_selected_state_all() -> dict:
    """上一次对账时在集合里的那些条目 (AIHOT 的 selected_state)."""
    conn = connect()
    try:
        return {row["item_id"]: int(row["in_set"]) for row in conn.execute(
            "SELECT item_id, in_set FROM feed_selected_state").fetchall()}
    finally:
        conn.close()


def feed_selected_state_put(item_id: str, in_set: int, last_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_selected_state (item_id, in_set, last_id) VALUES (?, ?, ?)"
            " ON CONFLICT (item_id) DO UPDATE SET in_set = excluded.in_set,"
            " last_id = excluded.last_id", (item_id, int(in_set), int(last_id)))
    finally:
        conn.close()


def feed_selected_ledger_add(item_id: str, op: str, at: str, visible_at: str) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO feed_selected_ledger (item_id, op, changed_at, visible_at)"
            " VALUES (?, ?, ?, ?)", (item_id, op, at, visible_at))
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def feed_selected_ledger(item_id: str = "", limit: int = 50) -> list[dict]:
    """台账的最近几笔; 给一个 item_id 就只看那一条的历史."""
    conn = connect()
    try:
        if item_id:
            rows = conn.execute(
                "SELECT * FROM feed_selected_ledger WHERE item_id = ? ORDER BY id DESC LIMIT ?",
                (item_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feed_selected_ledger ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def feed_selected_ledger_prune(before: str) -> int:
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_selected_ledger WHERE changed_at < ?",
                            (before,)).rowcount or 0
    finally:
        conn.close()


def feed_items_coverage() -> dict:
    """写作站的覆盖率 (2.3b): 每一档各有多少条, 以及进度到哪一步。

    不同档是不同的价钱与不同的字 (`llm_stage`), 所以它不是一个百分比 ——
    "还没问过"与"问了但只理解了"是两件事, 合并成一个数就看不出卡在哪一步。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT COALESCE(llm_stage, 'none') AS stage, COUNT(*) AS n FROM feed_items"
            " GROUP BY 1").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0]
        return {"total": int(total),
                "stages": {row["stage"]: int(row["n"]) for row in rows}}
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# feed_settings (5.9 / 5.8 / 5.12): 机器本地的那几个键值。


def feed_setting_get(key: str) -> str:
    """一个键的值; 没有就回空串 —— 空串是"没配", 与"配了个空"在这几个键上是同一件事。"""
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM feed_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""
    finally:
        conn.close()


def feed_setting_put(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at", (key, value, now_iso()))
    finally:
        conn.close()


def feed_setting_delete(key: str) -> int:
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_settings WHERE key = ?", (key,)).rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# feed_deliveries (5.8): 一条要发出去的消息的账。


def feed_delivery_add(channel: str, target: str, subject: str, body_json: str) -> int:
    conn = connect()
    try:
        at = now_iso()
        cursor = conn.execute(
            "INSERT INTO feed_deliveries (channel, target, subject, body_json, status,"
            " attempts, next_try_at, last_error, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, '', '', ?, ?)",
            (channel, target, subject, body_json, at, at))
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def feed_delivery_claim(limit: int = 5, at: str = "") -> list[dict]:
    """该发的几条: 还没发过的, 以及退避时间到了的失败件。"""
    conn = connect()
    try:
        moment = at or now_iso()
        rows = conn.execute(
            "SELECT * FROM feed_deliveries WHERE status = 'pending'"
            " AND (next_try_at = '' OR next_try_at <= ?) ORDER BY id LIMIT ?",
            (moment, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def feed_delivery_mark(delivery_id: int, status: str, error: str = "",
                       next_try_at: str = "") -> None:
    conn = connect()
    try:
        delivered = now_iso() if status == "sent" else None
        conn.execute(
            "UPDATE feed_deliveries SET status = ?, attempts = attempts + 1,"
            " last_error = ?, next_try_at = ?, updated_at = ?,"
            " delivered_at = COALESCE(?, delivered_at) WHERE id = ?",
            (status, error[:400], next_try_at, now_iso(), delivered, delivery_id))
    finally:
        conn.close()


def feed_delivery_seen(channel: str, target: str, subject: str) -> bool:
    """这一条是不是已经排过队了。同一期跑第二遍不该再发一条 ——
    幂等在表上，不在调用方的记性里。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM feed_deliveries WHERE channel = ? AND target = ? AND subject = ?",
            (channel, target, subject)).fetchone()
        return row is not None
    finally:
        conn.close()


def feed_deliveries_recent(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM feed_deliveries ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def feed_deliveries_prune(before: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "DELETE FROM feed_deliveries WHERE status = 'sent' AND delivered_at < ?",
            (before,)).rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# feed_fetch_queue (1.2): 补正文的持久队列。


def feed_queue_enqueue(rows: list[dict]) -> int:
    """把"要补正文"的那些行放进队列。已经在队列里的重新排一次队, 但**已经做完的不动**。

    做完那一条不能复活: 它的正文已经写在 `feed_items` 上了, 再排一次就是每轮都白跑一趟。
    """
    if not rows:
        return 0
    at = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN")
        for row in rows:
            conn.execute(
                "INSERT INTO feed_fetch_queue (source_id, external_id, url, status, attempts,"
                " next_try_at, leased_until, last_error, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', 0, '', '', '', ?, ?)"
                " ON CONFLICT (source_id, external_id) DO UPDATE SET"
                " url = excluded.url,"
                " status = CASE WHEN feed_fetch_queue.status = 'done' THEN 'done' ELSE 'pending' END,"
                " next_try_at = CASE WHEN feed_fetch_queue.status = 'done' THEN feed_fetch_queue.next_try_at ELSE '' END,"
                " updated_at = excluded.updated_at",
                (row["source_id"], row["external_id"], row.get("url") or "", at, at))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return len(rows)


def feed_queue_reclaim(at: str = "") -> int:
    """租约过期的行回到 pending。这是"崩了能接着跑"的那一行代码。"""
    conn = connect()
    try:
        moment = at or now_iso()
        return conn.execute(
            "UPDATE feed_fetch_queue SET status = 'pending', leased_until = '', updated_at = ?"
            " WHERE status = 'leased' AND leased_until <> '' AND leased_until <= ?",
            (now_iso(), moment)).rowcount or 0
    finally:
        conn.close()


def feed_queue_claim(limit: int, lease_until: str, at: str = "") -> list[dict]:
    """认领这一轮要跑的几条, 顺手给它们上租约。"""
    if limit <= 0:
        return []
    conn = connect()
    try:
        moment = at or now_iso()
        rows = conn.execute(
            "SELECT * FROM feed_fetch_queue WHERE status = 'pending'"
            " AND (next_try_at = '' OR next_try_at <= ?) ORDER BY next_try_at, id LIMIT ?",
            (moment, limit)).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            marks = ", ".join("?" * len(ids))
            conn.execute(
                "UPDATE feed_fetch_queue SET status = 'leased', leased_until = ?, updated_at = ?"
                " WHERE id IN (%s)" % marks, [lease_until, now_iso()] + ids)
        return [dict(row) for row in rows]
    finally:
        conn.close()


def feed_queue_finish(queue_id: int, status: str, error: str = "",
                      next_try_at: str = "") -> None:
    """一条跑完: done 或 failed(带下一次尝试的时刻)。失败要把 attempts 加一。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE feed_fetch_queue SET status = ?, attempts = attempts + 1,"
            " last_error = ?, next_try_at = ?, leased_until = '', updated_at = ? WHERE id = ?",
            (status, error[:400], next_try_at, now_iso(), queue_id))
    finally:
        conn.close()


def feed_queue_stats(at: str = "") -> dict:
    """队列的现状: 每一档几条, 以及到点了还没跑的几条。运维读口与测试都读它。"""
    conn = connect()
    try:
        moment = at or now_iso()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM feed_fetch_queue GROUP BY 1").fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        due = conn.execute(
            "SELECT COUNT(*) FROM feed_fetch_queue WHERE status = 'pending'"
            " AND (next_try_at = '' OR next_try_at <= ?)", (moment,)).fetchone()[0]
        return {"pending": counts.get("pending", 0), "leased": counts.get("leased", 0),
                "done": counts.get("done", 0), "failed": counts.get("failed", 0),
                "due": int(due), "total": sum(counts.values())}
    finally:
        conn.close()


def feed_queue_prune(before: str) -> int:
    """做完了的旧行清掉; 失败的留着 —— 它还要重试。"""
    conn = connect()
    try:
        return conn.execute(
            "DELETE FROM feed_fetch_queue WHERE status = 'done' AND updated_at < ?",
            (before,)).rowcount or 0
    finally:
        conn.close()



def feed_item_mentions_set(rows: list[dict]) -> int:
    """把一条报道「提及」的那些事实钉下来 (4.9b)。重跑覆盖同一对键。"""
    if not rows:
        return 0
    stamp = now_iso()
    conn = connect()
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO feed_item_mentions (source_id, external_id, fact_id, decided_at,"
                " prompt_version) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, external_id, fact_id) DO UPDATE SET"
                " decided_at = excluded.decided_at, prompt_version = excluded.prompt_version",
                (row["source_id"], row["external_id"], int(row["fact_id"]),
                 row.get("decided_at") or stamp, row.get("prompt_version") or ""))
        return len(rows)
    finally:
        conn.close()


def feed_item_mentions_of(source_id: str, external_id: str) -> list[dict]:
    """这一条提到了哪几件事: 事件 id 与标题, 给人看。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.id AS story_id, s.title AS story_title, f.id AS fact_id"
            " FROM feed_item_mentions m"
            " JOIN feed_facts f ON f.id = m.fact_id"
            " JOIN feed_stories s ON s.id = f.story_id"
            " WHERE m.source_id = ? AND m.external_id = ?"
            " ORDER BY s.id", (source_id, external_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def feed_items_set_scope(source_id: str, external_id: str, scope: str) -> None:
    """记下这一条的分析是 `primary` 还是 `composite` (4.9b)。"""
    conn = connect()
    try:
        conn.execute("UPDATE feed_items SET analysis_scope = ? WHERE source_id = ?"
                     " AND external_id = ?", (scope, source_id, external_id))
    finally:
        conn.close()


def feed_item_mentions_clear(source_id: str, external_id: str) -> int:
    """重判之前先把旧账清掉: 上一次提及的未必还在 (4.9b 与重分组同一条理由)。"""
    conn = connect()
    try:
        return conn.execute("DELETE FROM feed_item_mentions WHERE source_id = ?"
                            " AND external_id = ?", (source_id, external_id)).rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 引用帖的译文 (3.6)


def _quote_tweet_id(url: str) -> str:
    """链接里的推文号 (AIHOT 的 `/status/(\\d+)`)。同一个号才是同一条推, 网址不是。"""
    match = re.search(r"/status/(\d+)", url or "")
    return match.group(1) if match else ""


def feed_quote_translation_get(tweet_id: str) -> dict | None:
    """被引帖的译文攒在这儿, 一条只译一次 (AIHOT 的 `quote_translations`)。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM feed_quote_translations WHERE tweet_id = ?",
                           (tweet_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def feed_quote_translation_put(tweet_id: str, text_hash: str, text_zh: str, origin: str) -> None:
    """写一份译文。主键是被引帖的号, 所以十条帖子引同一句话只有这一行。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_quote_translations (tweet_id, text_hash, text_zh, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (tweet_id) DO UPDATE SET text_hash = excluded.text_hash,"
            " text_zh = excluded.text_zh, origin = excluded.origin, created_at = excluded.created_at",
            (tweet_id, text_hash, text_zh, origin, now_iso()))
    finally:
        conn.close()


def feed_quoted_items(since: str, limit: int = 400) -> list[dict]:
    """窗口里引用了别人的条目 (那条引用真有字), 新的在前 (AIHOT 的 `translateQuotes` 那一查)。"""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT source_id, external_id, quoted_json, published_at FROM feed_items"
            " WHERE quoted_json IS NOT NULL AND quoted_json <> '' AND quoted_json <> '{}'"
            "   AND published_at >= ? ORDER BY published_at DESC LIMIT ?",
            (_utc_stamp(since), int(limit))).fetchall()]
    finally:
        conn.close()


def feed_quote_own_text(tweet_id: str) -> str:
    """被引的那条帖子**自己**收过、译过的话 (AIHOT 的 `own_zh`)。

    同一条推文既可能被我们收成一条条目、也被别人引用; 收过就用它已经译好的那一份, 不再问模型 ——
    同一条推文的两份译文不一样, 读的人会以为其中一份在改口。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT i.body_zh FROM feed_items i JOIN feed_sources s ON s.id = i.source_id"
            " WHERE i.external_id = ? AND s.kind = 'x_search' AND i.body_zh IS NOT NULL"
            "   AND i.body_zh <> '' LIMIT 1", (str(tweet_id),)).fetchone()
        return str(row["body_zh"]).strip() if row else ""
    finally:
        conn.close()


def feed_item_quoted_apply(tweet_id: str, text_zh: str) -> int:
    """把这一份译文写回每一条引用了它的条目 —— 页面读的正是条目上这一格。

    写回而不是读取时现查: 列表一次几十条, 每条都去 join 一次译文表, 是拿一次连接换一格已经
    算好的字。被引的帖子改了 (号一样、话不一样) 由 `text_hash` 管, 与这里无关。
    """
    if not tweet_id or not text_zh:
        return 0
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT source_id, external_id, quoted_json FROM feed_items"
            " WHERE quoted_json LIKE '%/status/%'").fetchall()
        written = 0
        for row in rows:
            try:
                quoted = json.loads(row["quoted_json"] or "{}")
            except ValueError:
                continue
            if not isinstance(quoted, dict):
                continue
            if _quote_tweet_id(str(quoted.get("url") or "")) != str(tweet_id):
                continue
            if str(quoted.get("translation") or "").strip() == text_zh:
                continue
            quoted["translation"] = text_zh
            conn.execute("UPDATE feed_items SET quoted_json = ?"
                         " WHERE source_id = ? AND external_id = ?",
                         (json.dumps(quoted, ensure_ascii=False),
                          row["source_id"], row["external_id"]))
            written += 1
        return written
    finally:
        conn.close()
