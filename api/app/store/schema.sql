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

-- The lexicon: this round's off-chain judgement and prose, for the ten tokens the
-- round shortlisted. Nothing here outlives its round, on purpose - a row is written by
-- this round's research pass against this round's facts, so there is no baseline to
-- compare against, no expiry to check, and no text to reuse. `run_id` is what enforces
-- that: last round's rows are deleted before this round's are written.
CREATE TABLE IF NOT EXISTS lexicon (
  ca           TEXT PRIMARY KEY,
  chain_id     TEXT NOT NULL DEFAULT 'arc',
  run_id       TEXT,                -- the round this entry belongs to, and only that one
  symbol       TEXT,
  tag          TEXT,
  -- The five off-chain scores, each 0-5, keyed exactly as the prompt asks for them.
  -- There is no "undetermined" here: 0 means the search came back empty, which is a
  -- finding. Only an on-chain fact can be undetermined.
  f_completeness REAL,   -- 产品完成度
  f_substance    REAL,   -- 项目实质与亮点
  f_team         REAL,   -- 团队与 dev 可信度
  f_narrative    REAL,   -- 叙事与官方关系
  f_community    REAL,   -- 社区活跃度
  -- The card's prose, one field per judgement and named the way the card prints them:
  -- concl is the verdict at the top, the five below it are the 链下调研 fold. A judgement
  -- with no field here is a score nobody can read the reason for, and a field with no
  -- judgement is prose outside the rubric - the two lists move together.
  social         TEXT,   -- 社区活跃度
  concl          TEXT,   -- 结论：定位 / 实质 / 亮点 / 风险
  narr_text      TEXT,   -- 叙事与官方关系
  prod_text      TEXT,   -- 产品完成度
  substance_text TEXT,   -- 项目实质与亮点
  team_text      TEXT,   -- 团队与 dev 可信度
  watch        TEXT,
  scenario     TEXT,
  evidence_json TEXT,
  updated_at   TEXT NOT NULL
);

-- One report card per selected token per run. Immutable once written.
CREATE TABLE IF NOT EXISTS reports (
  run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ca           TEXT NOT NULL,
  rank         INTEGER NOT NULL,
  verdict      TEXT,
  status       TEXT NOT NULL DEFAULT 'written',  -- written once the entry's prose is in
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
CREATE INDEX IF NOT EXISTS idx_lexicon_chain ON lexicon(chain_id, symbol);
