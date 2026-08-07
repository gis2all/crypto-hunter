-- Crypto Hunter schema. Applied idempotently by store/db.py.
-- Rule: runs/snapshots/settings_versions/run_summary are immutable; only lexicon and tokens are editable.
-- 新机会那六张表按同一条规则分两侧：原始材料（opportunity_posts / opportunity_pages）只增不改，
-- 判断（opportunity_sources / opportunities / opportunity_steps）可编辑，扫描回执（opportunity_scans）只增。

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
  --: Which of the chain's sets of settings this version belongs to. Versions are numbered
  --: per chain, so two sets on one chain share the counter and never a row.
  config_id   TEXT NOT NULL DEFAULT 'default',
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

-- One screening round. Immutable. run_key makes the scheduled tick idempotent.
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  run_key       TEXT NOT NULL UNIQUE,
  chain_id      TEXT NOT NULL,
  --: Which of the chain's sets of settings this round ran under. The report library is
  --: read through it: two sets on one chain keep two libraries and never compare across.
  config_id     TEXT NOT NULL DEFAULT 'default',
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
  ca           TEXT NOT NULL,
  chain_id     TEXT NOT NULL DEFAULT 'arc',
  --: Which of the chain's sets of settings judged it. The same CA can be screened by two
  --: sets on one chain, and each keeps its own entry - keyed by CA alone, the second set's
  --: round would overwrite a judgement the first set's card was already written from.
  config_id    TEXT NOT NULL DEFAULT 'default',
  run_id       TEXT,                -- the round this entry belongs to, and only that one
  symbol       TEXT,
  tag          TEXT,
  -- The five off-chain scores, each 0-5, keyed exactly as the prompt asks for them.
  -- There is no "undetermined" here: 0 means the search came back empty, which is a
  -- finding. Only an on-chain fact can be undetermined.
  f_completeness REAL,   -- 产品完成度
  f_substance    REAL,   -- 项目实质与亮点
  f_team         REAL,   -- 团队与 dev 可信度
  f_narrative    REAL,   -- 叙事与主线
  f_community    REAL,   -- 社区活跃度
  -- The card's prose, one field per judgement and named the way the card prints them:
  -- concl is the verdict at the top, the five below it are the 链下调研 fold. A judgement
  -- with no field here is a score nobody can read the reason for, and a field with no
  -- judgement is prose outside the rubric - the two lists move together.
  social         TEXT,   -- 社区活跃度
  concl          TEXT,   -- 结论：定位 / 实质 / 亮点 / 风险
  narr_text      TEXT,   -- 叙事与主线
  prod_text      TEXT,   -- 产品完成度
  substance_text TEXT,   -- 项目实质与亮点
  team_text      TEXT,   -- 团队与 dev 可信度
  watch        TEXT,
  scenario     TEXT,
  evidence_json TEXT,
  updated_at   TEXT NOT NULL,
  -- One entry per CA per set of settings: the second set's judgement is its own row.
  PRIMARY KEY (chain_id, config_id, ca)
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
  config_id  TEXT NOT NULL DEFAULT 'default',
  kind       TEXT NOT NULL,   -- website | x | github | docs | other
  url        TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  note       TEXT,
  PRIMARY KEY (chain_id, config_id, ca, url)
);

CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id, decision);
CREATE INDEX IF NOT EXISTS idx_runs_chain_time ON runs(chain_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS idx_lexicon_chain ON lexicon(chain_id, symbol);

-- Cards the reader has seen for a token: either one they collected (`kept` = 1) or one a CA
-- search wrote (`kept` = 0, origin = 'search'). A card is a copy, not a pointer: a report row
-- lives and dies with its round, and a whole report library can be dropped, while a card the
-- reader holds on to has to stay readable - so the assembled card is copied in whole and
-- where it came from is recorded beside it. Keyed by (chain_id, ca), and by the chain alone:
-- a searched token has no round and no set of settings to belong to, and a shelf that emptied
-- when the reader switched settings would not be theirs. Nothing here cascades - deleting a
-- round, or a whole report library, leaves it alone on purpose.
CREATE TABLE IF NOT EXISTS favorites (
  chain_id      TEXT NOT NULL,
  ca            TEXT NOT NULL,
  symbol        TEXT,
  origin        TEXT NOT NULL DEFAULT 'round',   -- round | search
  origin_run_id TEXT,                            -- only a round has one
  origin_label  TEXT,                            -- what the card's foot prints as 生成时间
  score         REAL,
  rank          INTEGER,
  config_id     TEXT,                            -- the set of settings the round ran under
  kept          INTEGER NOT NULL DEFAULT 1,      -- 1 = the reader collected it; 0 = a search record
  payload_json  TEXT NOT NULL,                   -- the card, verbatim
  saved_at      TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (chain_id, ca)
);

-- One CA search, as the page that started it watches it.
--
-- Deliberately not a runs row. A search belongs to no set of settings, no round and no
-- report library, and a chain's round list that showed one would read as that chain having
-- run again. It is also shorter-lived for a different reason: a round is a fact about a
-- chain that is kept forever, while this is the progress of two minutes of work plus the
-- answer it produced - and the answer itself lands in favorites (origin = 'search'), which
-- is what the recent list reads. A finished job is a receipt, not a record.
CREATE TABLE IF NOT EXISTS search_jobs (
  id          TEXT PRIMARY KEY,
  ca          TEXT NOT NULL,
  chain_id    TEXT,                             -- filled once the search read which chain
  status      TEXT NOT NULL,                    -- running | done | failed
  stage       TEXT,                             -- the stage running now: a key of lookup.STAGES
  stages_json TEXT NOT NULL,                    -- [{key, label, state, seconds}] in order
  error       TEXT,                             -- why a failed search stopped
  row_json    TEXT,                             -- the stored card row, once it answered
  started_at  TEXT NOT NULL,
  finished_at TEXT
);

-- ============================================================================================
-- 新机会：外部活动与新项目的发现（api/app/opportunity/，见该模块 README）
--
-- 这个模块与代币筛选流水线无关：它不读 run_items / snapshots / 候选池，不写 runs，不参与打分。
-- 它发现两类东西——可参与活动（有步骤）与新项目 / 新协议 / 新实验（说得清是什么）——并把
-- 每一类做成一条带来源、可核实的条目。下面六张表按两侧分：原始材料只增不改，判断可编辑。
-- ============================================================================================

-- The watchlist: what a scan looks at. Editable - an operator adds an account or a platform
-- without touching a file. The seed in opportunity/seeds/watchlist.yaml fills this once, on
-- the first start, and never again: after that the table is the truth, deletions included.
CREATE TABLE IF NOT EXISTS opportunity_sources (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  target     TEXT NOT NULL UNIQUE,          -- an X handle (official) or a raw X query (query)
  kind       TEXT NOT NULL,                 -- official | query
  ecosystem  TEXT NOT NULL DEFAULT 'multi',
  cluster    TEXT,                          -- launchpad / defi / infra / ... a label, not a rule
  enabled    INTEGER NOT NULL DEFAULT 1,
  --: A query row's own three numbers: how far back it looks, how long one slice is, and how many
  --: posts one request asks for. Slicing is the answer to "forty posts per request": a day cut
  --: into four-hour pieces is six requests, not a ceiling (see opportunity/xread.py).
  window_hours INTEGER NOT NULL DEFAULT 24,
  slice_hours  INTEGER NOT NULL DEFAULT 4,
  max_results  INTEGER NOT NULL DEFAULT 40,
  note       TEXT,                          -- why it is on the list, and what verified it
  added_at   TEXT NOT NULL,
  --: When a scan last read it. One scan reads a bounded number of sources (see
  --: opportunity/scan.py MAX_SOURCES), so the next one starts with the least recently read
  --: rather than the same head of the list every time.
  last_read_at TEXT
);

-- Raw material, the X half. Immutable: one row per post, forever. The judgement beside it
-- (`classified`) is written once, by the classification pass, and never rewritten by a later
-- scan - what a post was read as is part of what was read.
CREATE TABLE IF NOT EXISTS opportunity_posts (
  tweet_id        TEXT PRIMARY KEY,
  handle          TEXT NOT NULL,
  author_name     TEXT,
  text            TEXT NOT NULL,
  lang            TEXT,
  metrics_json    TEXT,
  --: The free gate's own reading (the rule set): score, level and the rules that matched. Kept on
  --: the material because "why was this one let through" has to be answered by the rules as they
  --: were, not by whatever they say later.
  score           REAL,
  rules_json      TEXT,
  --: 溯源（线索 → 官方家）的读数：等级 A/B/C、trust、依据，以及这次读到的官方页。
  --: 官方页随这条线索存，因为它只为这条线索读一次，而抽取要拿它当材料（2026-10-19）。
  urls_json       TEXT,                     -- 帖子自带的链接（展开过的），溯源要用
  project_name    TEXT,                     -- 分类那一步给出的项目名，溯源用它搜官方号
  trace_level     TEXT,                     -- A | B | C | NULL（没溯源）
  trace_json      TEXT,
  traced_at       TEXT,
  posted_at       TEXT,
  url             TEXT,
  source_target   TEXT,                     -- which watchlist row found it
  first_seen_scan TEXT,
  classified      TEXT,                     -- activity | project | opinion | news | other | skipped
  classified_at   TEXT,
  extracted_at    TEXT                      -- when the expensive pass last read it (NULL = not yet)
);

-- Raw material, the page half: a platform listing, an announcement board, an official site.
-- Same rule as the posts: stored verbatim, kept, never rewritten.

-- The entries themselves. Editable: a re-scan rewrites what it can re-derive, and the row
-- keeps the earliest `discovered_at` it ever had - that timestamp is this project's own
-- observation, not something the material claimed, which is why it is the list's sort key.
CREATE TABLE IF NOT EXISTS opportunities (
  slug            TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,            -- activity | project
  category        TEXT,                     -- 打新 / 挖矿 / ... or 新协议 / 新实验 / ...
  title           TEXT NOT NULL,
  organizer       TEXT,                     -- the official handle, when the material names one
  official_url    TEXT,
  summary         TEXT,                     -- the project half: what it is, in one sentence
  trust           TEXT NOT NULL DEFAULT 'unjudged',  -- official | community | unjudged
  trust_basis     TEXT,                     -- which reading made it official (or why it is not)
  --: 从哪条 watchlist 行读到的：生态与来源类型都是那条行的字段，条目自己不知道它们，
  --: 而页面要按它们筛选，所以在写入时随行存下来（来源行以后被删掉，条目的归属也还在）。
  ecosystem       TEXT,
  source_type     TEXT,                     -- official | platform | exchange | catalog | github | keyword
  risk            TEXT,
  barrier         TEXT,                     -- comma-joined enum, the activity half only
  maturity        TEXT,                     -- experimental | testnet | mainnet | unjudged
  started_at      TEXT,                     -- only ever copied from the material
  deadline        TEXT,                     -- only ever copied from the material, never inferred
  status          TEXT NOT NULL DEFAULT 'candidate',  -- candidate | promoted | retired（管线写的）
  --: 读者自己的决定（归档 / 删除），与 status 是两件事：一张卡可以同时是「未晋升」（管线说
  --: 没找到官方来源）与「已归档」（你说优先级不高）。**管线永不改写它** —— 重抽一个项目时
  --: `opportunity_put` 的 upsert 会重写其它每一列，但不会把归档撤销。删除是软删，行留着。
  user_state      TEXT NOT NULL DEFAULT 'active',   -- active | archived | deleted
  user_state_at   TEXT,
  reason          TEXT,                     -- why it is not promoted; see opportunity/promote.py
  --: 热度：来源帖自身的互动量换算的 0-100（见 opportunity/heat.py）。没有来源帖的材料是
  --: NULL —— 页面材料没有互动读数，而"没问到"与"没人看"不是一件事。它不是排序键。
  heat            REAL,
  heat_basis      TEXT,                     -- 分数旁边的原始数字（浏览 / 赞 / 转 / 评）
  discovered_at   TEXT NOT NULL,
  source_kind     TEXT,                     -- post | page
  source_ref      TEXT,                     -- the tweet id or the url it was read from
  extract_version TEXT,                     -- which prompt wrote it; a rule change is traceable
  updated_at      TEXT NOT NULL
);

-- The participation steps. Only an activity has rows, and they are rewritten whole on every
-- extraction: a half-updated step list is a set of instructions nobody can follow.
CREATE TABLE IF NOT EXISTS opportunity_steps (
  slug       TEXT NOT NULL,
  ordinal    INTEGER NOT NULL,
  title      TEXT,
  body       TEXT NOT NULL,
  source_url TEXT,                          -- where this step was read
  source_at  TEXT,
  PRIMARY KEY (slug, ordinal)
);

-- One scan, as the page that started it watches it. Append-only, and deliberately not a runs
-- row for the same reason search_jobs is its own table: a scan belongs to no chain, no set of
-- settings and no report library, and showing one in a chain's round list would read as that
-- chain having run again.
CREATE TABLE IF NOT EXISTS opportunity_scans (
  id            TEXT PRIMARY KEY,
  status        TEXT NOT NULL,              -- running | done | failed
  stage         TEXT,                       -- a key of opportunity.job.STAGES
  stages_json   TEXT NOT NULL,              -- [{key, label, state, seconds}] in order
  counters_json TEXT,                       -- how much each stage found and spent
  error         TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_opportunity_status ON opportunities(status, kind, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunity_posts_handle ON opportunity_posts(handle, posted_at DESC);
