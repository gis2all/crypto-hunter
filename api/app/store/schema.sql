-- Crypto Hunter schema. Applied idempotently by store/db.py.
-- Rule: runs/snapshots/settings_versions/run_summary are immutable; only lexicon and tokens are editable.

CREATE TABLE IF NOT EXISTS chains (
  id          TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 0,
  config_json TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- One row per committed settings change. Immutable: reports always point at the version they ran with.
CREATE TABLE IF NOT EXISTS settings_versions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  chain_id    TEXT NOT NULL,
  version     INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  note        TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (chain_id, version)
);

-- Token master record (first seen + static facts). Editable.
CREATE TABLE IF NOT EXISTS tokens (
  ca            TEXT PRIMARY KEY,
  chain_id      TEXT NOT NULL,
  symbol        TEXT,
  decimals      INTEGER,
  launchpad     TEXT,
  created_at    TEXT,
  first_seen_run TEXT,
  --: 距高回撤 reads the high from here: the highest market cap any source has reported
  --: for this CA in any round, written for every scanned token, not only the candidates.
  peak_market_cap_usd REAL,
  peak_market_cap_at  TEXT,
  updated_at    TEXT NOT NULL
);

-- One screening round. Immutable. run_key makes the cron tick idempotent.
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  run_key       TEXT NOT NULL UNIQUE,
  chain_id      TEXT NOT NULL,
  trigger       TEXT NOT NULL,
  scheduled_at  TEXT,
  status        TEXT NOT NULL,
  scanned       INTEGER NOT NULL DEFAULT 0,
  dropped       INTEGER NOT NULL DEFAULT 0,
  candidates    INTEGER NOT NULL DEFAULT 0,
  selected      INTEGER NOT NULL DEFAULT 0,
  settings_version_id INTEGER REFERENCES settings_versions(id),
  started_at    TEXT,
  finished_at   TEXT,
  error         TEXT,
  progress      INTEGER NOT NULL DEFAULT 0,   -- 0-100, what the page draws as a bar
  progress_label TEXT                          -- the phase that percent belongs to
);

-- Per token per run: the decision plus the reason, so "who got filtered out and why" is answerable.
CREATE TABLE IF NOT EXISTS run_items (
  run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ca           TEXT NOT NULL,
  decision     TEXT NOT NULL,          -- selected | candidate | dropped
  drop_reason  TEXT,                   -- see core.models.DropReason
  score        REAL,
  rank         INTEGER,
  breakdown_json TEXT,
  PRIMARY KEY (run_id, ca)
);

-- Metric snapshot per token per run. Immutable: recomputation replays these, it never re-fetches.
CREATE TABLE IF NOT EXISTS snapshots (
  run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ca           TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  safety_json  TEXT,
  social_json  TEXT,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, ca)
);

-- Round-level facts that belong to no single token: the funnel's per-rule counts,
-- the change list, the口径 the round ran under and where its numbers came from.
CREATE TABLE IF NOT EXISTS run_summary (
  run_id       TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

-- The lexicon: LLM drafts it, a human confirms it. This is the asset that compounds.
CREATE TABLE IF NOT EXISTS lexicon (
  ca           TEXT PRIMARY KEY,
  chain_id     TEXT NOT NULL DEFAULT 'arc',
  symbol       TEXT,
  tag          TEXT,
  narrative    REAL,
  product      REAL,
  social       TEXT,
  concl        TEXT,
  narr_text    TEXT,
  prod_text    TEXT,
  mech_text    TEXT,
  pros         TEXT,
  cons         TEXT,
  watch        TEXT,
  scenario     TEXT,
  review_state TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed
  evidence_json TEXT,
  updated_at   TEXT NOT NULL,
  -- The refresh bookkeeping: what the entry was written against, and when it was
  -- last rewritten. `baseline_*` is the measuring stick the refresh conditions use -
  -- without it "市值波动 40%" has nothing to compare with.
  baseline_mcap    REAL,
  baseline_holders INTEGER,
  baseline_at      TEXT,
  -- The safety verdict the words were written against (源码/权限/LP, as words: it
  -- lands in a reason column and a diff of it is the sentence the card shows).
  -- A verdict that lands after the entry was written makes the entry stale.
  baseline_safety  TEXT,
  refresh_reason   TEXT,   -- why the last rewrite happened
  research_state   TEXT NOT NULL DEFAULT 'none',  -- none | queued | running | done | failed
  research_note    TEXT                           -- why a research pass failed or was cut short
);

-- One report card per selected token per run. Immutable once sent.
CREATE TABLE IF NOT EXISTS reports (
  run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ca           TEXT NOT NULL,
  rank         INTEGER NOT NULL,
  verdict      TEXT,
  status       TEXT NOT NULL DEFAULT 'draft',    -- draft | written | sent
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, ca)
);

-- Every LLM call, for cost and auditability.
CREATE TABLE IF NOT EXISTS llm_calls (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT,
  purpose      TEXT NOT NULL,
  model        TEXT,
  prompt_version TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms   INTEGER,
  cost_usd     REAL,
  ok           INTEGER NOT NULL DEFAULT 1,
  error        TEXT,
  created_at   TEXT NOT NULL
);

-- Where an entry evidence came from: one row per page a research pass actually read,
-- so a card can link its claim back to the page instead of asking anyone to trust prose.
CREATE TABLE IF NOT EXISTS lexicon_sources (
  ca         TEXT NOT NULL,
  chain_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,   -- website | x | github | docs | other
  url        TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  note       TEXT,
  PRIMARY KEY (ca, url)
);

CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id, decision);
CREATE INDEX IF NOT EXISTS idx_runs_chain_time ON runs(chain_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS idx_lexicon_state ON lexicon(review_state);
CREATE INDEX IF NOT EXISTS idx_lexicon_chain ON lexicon(chain_id, symbol);
