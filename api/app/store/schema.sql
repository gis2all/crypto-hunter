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

-- One screening round. Immutable. run_key makes the cron tick idempotent.
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

-- 加密信息流（宏观）：采集层读到的每一条原始记录。一行 = 一个来源说过的一次。
-- 与雷达同库不同域：这张表不引用 runs，也不被 runs 引用；一轮雷达读不到它，也不写它。
--
-- 主键是（来源，该来源自己的 id），不是自增 id：同一个来源重发同一条时，「我们又看了一遍」
-- 和「又发生了一次」必须能分开，否则「本轮读了 400 条」这个数字量的是我们看了几次。
CREATE TABLE IF NOT EXISTS feed_items (
  source_id    TEXT NOT NULL,          -- 独立主体。热度的去重按它算，不按条数
  external_id  TEXT NOT NULL,          -- 该来源自己的 id
  source_kind  TEXT NOT NULL,          -- rss | x | exchange | regulator
  identity_key TEXT NOT NULL,          -- 它属于哪件事。词表学到新名字后这一列会被改写
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  author       TEXT,
  body         TEXT,                   -- 净化过的块级 HTML (htmlbody.py), 不是纯文本
  excerpt      TEXT,                   -- 源在清单里给的摘要 (纯文字; AIHOT 的 excerpt); 列表只给一句话时它就是那句话
  content_hash TEXT NOT NULL DEFAULT '',   -- 内容的身份: 这一串变了 revision 就 +1 (AIHOT contentHash)
  revision     INTEGER NOT NULL DEFAULT 1, -- 第几版正文: 补到正文 / 源改了字 都会 +1
  llm_revision INTEGER,                -- 库里这些模型写的字是接着哪一版写的; 小于 revision 就是过期结论
  published_at TEXT NOT NULL,          -- 来源自己的时间
  media_json   TEXT NOT NULL DEFAULT '[]',  -- 来源自带的图; 只有 X 这类会给
  quoted_json  TEXT,                   -- 这条引用的那条推 {text, handle, url}; 只有 X 这类会给
  fact_json    TEXT,                   -- 这件事的骨架 {subject, action, object}; 判定分组要读它
  subjects_json TEXT,                  -- 这条在说谁 (实体词典的 id); 模型写过才有
  seen_at      TEXT NOT NULL,          -- 我们**第一次**读到它的时间 (AIHOT 的 discovered_at); 重读不改它
  detail_at    TEXT,                   -- 上次去读它原文的时刻; 失败也记, 免得坏链接每轮都敲
  title_zh     TEXT,                   -- 模型写的中文标题; 空 = 规则版, 页面印源自己的标题
  summary_llm  TEXT,                   -- 模型写的摘要; 空 = 规则版 (正文第一段)
  reason_llm   TEXT,                   -- 模型写的推荐理由; 空 = 规则版 (按类别套的那一句)
  identity_guard TEXT,                 -- 身份守卫退过的东西 (JSON); 空 = 标题与摘要都通过了
  body_zh      TEXT,                   -- 译文正文 (块级 HTML, 结构与 body 相同); 空 = 没译过
  body_complete INTEGER NOT NULL DEFAULT 0,  -- 译文是不是整篇; 0 = 缺块, 页面要标出来
  score_llm    REAL,                   -- 模型两次评分的均值 (印在页面); 空 = 只有规则分
  llm_scores_json TEXT,                -- 那两次各自的分数 [int, int]; 精选门槛按它算
  category_llm TEXT,                   -- 结构档抽的类别 key; 空 = 用规则版读出来的那一个
  tags_json    TEXT,                   -- 结构档抽的 {tags, entities}; 空 = 用规则版
  relevance    TEXT,                   -- 预筛三态: pass | block | unknown; 空 = 还没预筛过
  llm_stage    TEXT,                   -- NULL 还没问 | summarize 便宜档 | understand 理解档 | full 都写了 | blocked 预筛判无关
  prompt_version TEXT,                 -- 用的哪一版提示词 (内容哈希); 改一个字就是一个新版本
  llm_at       TEXT,                   -- 上次问模型的时刻; 失败也记, 免得坏条目每轮都排进预算
  -- 发布门与可见性 (AIHOT 的 `visible_after` / `withdrawn`): 读到之后先压三分钟。
  -- 空 released_at = 这一行比这个列还早, 不拆它的门 (不能因为一次升级把整库藏起来)。
  released_at      TEXT,
  withdrawn_at     TEXT,
  -- SEO 两格 (AIHOT 的 `seo_indexed_at` / `seo_excluded_at`): 编辑手动收录或排除。
  seo_indexed_at   TEXT,
  seo_excluded_at  TEXT,
  PRIMARY KEY (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_items_identity
  ON feed_items(identity_key, published_at DESC);

-- 每一版正文都留档 (AIHOT 的 article_revisions)。
-- 两个理由, 一个是追溯 ("这一版之前是什么样"), 另一个是判定:
-- 一份曾经见过的渲染 (列表在两种写法之间来回换、页面轮换推广位)
-- 不算新材料, 不该再去花一遍模型的钱.
CREATE TABLE IF NOT EXISTS feed_item_revisions (
  source_id    TEXT NOT NULL,
  external_id  TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  title        TEXT NOT NULL,
  body_text    TEXT,
  excerpt      TEXT,
  seen_at      TEXT NOT NULL,
  PRIMARY KEY (source_id, external_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_feed_item_revisions_hash
  ON feed_item_revisions(source_id, external_id, content_hash);

-- 一期。一期只写一次：写出去之后再改，「读者已经读过的那一版」就不存在了。
-- 主键就是这件事的实现：id 由 (kind, key) 推出来，而 key 是一段时期的名字（2026-10-03 /
-- 2026-W40 / 2026-10），所以"这一天再跑一遍"算出的是同一个 id，撞上主键冲突什么都不改。
-- 一期是一个**时期**，不是一次运行 —— 这正是重跑不能悄悄换掉已经发出去的那一期。
-- 行是标量，payload_json 只有正文（每件事与它下面的原始记录），一件事只有一个地方记。
-- 「第几期」不在这里：它是对同一份档案里比自己早的那些数出来的（store/db.py 的
-- `_ISSUE_NUMBER`），存下来就要在每次回填之后重新对一遍，而派生出来的永远是当期那个答案。
CREATE TABLE IF NOT EXISTS feed_editions (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,          -- day | week | month
  key          TEXT NOT NULL,          -- 2026-10-03 | 2026-W40 | 2026-10
  window_from  TEXT NOT NULL,
  window_to    TEXT NOT NULL,
  seen         INTEGER NOT NULL DEFAULT 0,   -- 这个窗口读到多少条原始记录
  entries      INTEGER NOT NULL DEFAULT 0,   -- 其中有多少条上了刊
  headline     TEXT NOT NULL DEFAULT '',     -- 一期一个标题，档案栏那一列画的是它
  metrics_json TEXT NOT NULL DEFAULT '[]',   -- 报头那几个数 [{key, label, value}]
  payload_json TEXT NOT NULL,          -- 正文：每件事与它下面的原始记录
  created_at   TEXT NOT NULL,
  UNIQUE (kind, key)
);

-- 一个源。是数据不是代码：`kind` 挑抓取器，其余全在 `config_json` 里，键按 kind 走白名单
-- （`feed/sources/kind/config_keys.py`）—— 不认的键让这一轮失败，不静默落回通用解析，
-- 因为静默忽略的键就是一个从此不再回答的源。
-- 「这个源是什么」与「它怎么样了」分两列组：config / interval 那几列来自
-- `config/feed/sources.yaml`，cursor / fail_count / health / next_fetch_at 只写库、不回头写
-- 文件 —— 一份既被程序改写、又被人手编辑的清单，两边都会不可信。
CREATE TABLE IF NOT EXISTS feed_sources (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  kind                TEXT NOT NULL,          -- rss | json_list | web_list | x_search
  config_json         TEXT NOT NULL DEFAULT '{}',
  tier                TEXT NOT NULL DEFAULT 'T2',
  first_party         INTEGER NOT NULL DEFAULT 0,
  language            TEXT NOT NULL DEFAULT '',   -- zh | en | ''(没说, 按正文数汉字判)
  participation_mode  TEXT NOT NULL DEFAULT 'editorial',   -- editorial | hot_signal | isolated
  -- 内容许可与可见性 (AIHOT 的 SourceFacts)，三格都是人手写的清单，不是运行态：
  -- 一个付费墙源能抓到正文，不等于可以印它。
  visibility          TEXT NOT NULL DEFAULT 'public',      -- public | withdrawn
  withdrawn_at        TEXT,                                -- 下架的时刻；空 = 还在
  site_fulltext       INTEGER NOT NULL DEFAULT 0,          -- 允许在站内印全文
  syndicate_fulltext  INTEGER NOT NULL DEFAULT 0,          -- 允许把全文再分发给 RSS/API
  interval_minutes    INTEGER NOT NULL DEFAULT 60,
  enabled             INTEGER NOT NULL DEFAULT 1,
  health              TEXT NOT NULL DEFAULT 'unknown',     -- ok | degraded | failing | paused | unknown
  fail_count          INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  last_fetch_at       TEXT,
  last_ok_at          TEXT,
  -- 租约：抓之前先推到 now + 10 分钟，抓完写回真值。于是「正在抓」不会在下一分钟被
  -- 再排一次，而抓崩了也不会把这一格永远占住。
  next_fetch_at       TEXT,
  -- 读到哪儿了。失败的抓取绝不推进它 —— 推进了就等于把没读到的那批当成读过了。
  cursor_json         TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

-- 每次抓取一行。它的唯一作用是回答「这个源这次拿到几条」：HTML 源的选择器失效不报错、
-- 不抛异常，只是从此一条不吐，而 found = 0 是那种失败唯一会留下的痕迹。
CREATE TABLE IF NOT EXISTS feed_fetch_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id    TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed
  found        INTEGER NOT NULL DEFAULT 0,
  created      INTEGER NOT NULL DEFAULT 0,
  error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_feed_fetch_runs_source
  ON feed_fetch_runs(source_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_feed_sources_due
  ON feed_sources(enabled, next_fetch_at);

-- 审计日志 (AIHOT 的 `audit`): 一个东西被改了, 得能回答"谁改的, 什么时候".
-- `subject` 是被改的那东西的稳定名字 (feed:source:rss:theblock 这种), 不是一句描述:
-- 一句描述没办法当键查。`action` 取 `audit.ACTIONS` 里的值。
CREATE TABLE IF NOT EXISTS feed_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL DEFAULT '',
  action      TEXT NOT NULL,
  subject     TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_feed_audit_subject
  ON feed_audit(subject, at DESC);

-- 定时活儿 (AIHOT 的 `job_runs` 与它那排 cron): 一个名字、一个周期, 以及「上一次跑在哪个槽」。
-- 「槽」是按周期对齐后向下取整的那个时刻, 不是「上次跑完的时刻」—— 用后者的话, 一次跑久了
-- 后面每一槽都跟着往后漂。判「这一槽该不该跑」只看这一列; 积了几槽的账补跑一次 (见 feed/jobs.py)。
CREATE TABLE IF NOT EXISTS feed_jobs (
  name            TEXT PRIMARY KEY,
  cadence_minutes INTEGER NOT NULL,
  last_slot       TEXT,
  last_run_at     TEXT,
  last_status     TEXT,
  last_error      TEXT,
  enabled         INTEGER NOT NULL DEFAULT 1
);

-- 每次跑一行, 和 `feed_fetch_runs` 同一个道理: 一个一天跑一次的活儿坏掉, 只有这张表记得。
CREATE TABLE IF NOT EXISTS feed_job_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  slot        TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | skipped
  detail_json TEXT NOT NULL DEFAULT '{}',
  error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_feed_job_runs_name
  ON feed_job_runs(name, slot DESC);

-- 分组：事实与事件。AIHOT 的 stories / facts / fact_articles 三张表在这里。
--
-- `feed_stories` 是「一件事」，`feed_facts` 是这件事下面的一次**发生**（一个新进展），
-- `feed_item_facts` 是一条报道落在哪个事实上（role = primary 是这次发生本身，report 是又一次
-- 报道）。分层是必须的：热点榜数的是「几家在说这一次发生」，而时间线要的是「这件事先后发生过
-- 什么」—— 一张表存不下两个问题，硬存就会出现「同一家被数了三次」那种数。
--
-- `root` 只标在该事件的**根**事实上（最早那一次发生）。后续只挂根（AIHOT 的 storyRoot）：一条
-- 已经有后续的事实不再收新的后续，否则时间线会长成一条链，两年前的事能挂到今天。
CREATE TABLE IF NOT EXISTS feed_stories (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  title          TEXT NOT NULL DEFAULT '',      -- 事件标题；模型写过综述就是综述那个标题
  digest         TEXT NOT NULL DEFAULT '',      -- 事件综述；空 = 没人写过，页面印规则版
  latest         TEXT NOT NULL DEFAULT '',      -- 最新进展一句话
  status         TEXT NOT NULL DEFAULT 'active',-- active | watching | settled
  digest_inputs TEXT NOT NULL DEFAULT '',       -- 上次写综述时的输入指纹（报道集合的哈希）
  digest_ids    TEXT NOT NULL DEFAULT '',       -- 那一次输入覆盖的报道 id（JSON 数组），算增量用
  first_at       TEXT NOT NULL DEFAULT '',
  last_at        TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_story_ties (
  source_id   TEXT NOT NULL,
  external_id TEXT NOT NULL,
  story_id    INTEGER NOT NULL,
  relation    TEXT NOT NULL DEFAULT '',
  confidence  REAL NOT NULL DEFAULT 0,
  at          TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_id, external_id, story_id)
);

CREATE TABLE IF NOT EXISTS feed_story_links (
  story_id   INTEGER NOT NULL,
  other_id   INTEGER NOT NULL,
  relation   TEXT NOT NULL DEFAULT 'related',
  created_at TEXT NOT NULL,
  PRIMARY KEY (story_id, other_id)
);

CREATE TABLE IF NOT EXISTS feed_grouping_overrides (
  source_id   TEXT NOT NULL,
  external_id TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  actor       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS feed_facts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id    INTEGER NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  subject     TEXT NOT NULL DEFAULT '',
  action      TEXT NOT NULL DEFAULT '',
  object      TEXT NOT NULL DEFAULT '',
  occurred_at TEXT,                             -- 原文明确给出的发生日期；未知是 NULL
  at          TEXT NOT NULL DEFAULT '',         -- 这一次发生（首发）的时刻
  root        INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feed_facts_story
  ON feed_facts(story_id, at);

-- 讨论证据 (AIHOT 的 `story_signals`): 只当"热度证据"的源 (X 上的监测账号这类) 不立事件, 它
-- 挂到别人已经立好的那件事上; 挂不上也记一笔, 免得每一轮都拿它去问一次模型。
-- `story_id` 为空 = 问过了没挂上 (AIHOT 的 `signal-unmatched`), 那一行不进任何一件事的成员。
-- 热点榜问的是"几家在说**这一次发生**", 所以转发一句也算一家在说 —— 这一列就是那份证据。
CREATE TABLE IF NOT EXISTS feed_story_signals (
  source_id   TEXT NOT NULL,
  external_id TEXT NOT NULL,
  story_id    INTEGER,
  kind        TEXT NOT NULL DEFAULT 'signal',   -- signal | signal-native
  observed_at TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT '',      -- 定夺这条时用的分组提示词版本（空 = 本次改动之前定的）
  PRIMARY KEY (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_story_signals_story
  ON feed_story_signals(story_id);

CREATE TABLE IF NOT EXISTS feed_item_facts (
  source_id   TEXT NOT NULL,
  external_id TEXT NOT NULL,
  fact_id     INTEGER NOT NULL,
  role        TEXT NOT NULL DEFAULT 'report',   -- primary | report
  relation    TEXT NOT NULL DEFAULT '',         -- same_occurrence | same_story | ...
  confidence  REAL NOT NULL DEFAULT 0,
  note        TEXT NOT NULL DEFAULT '',
  decided_at  TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT '',      -- 定夺这条时用的分组提示词版本（空 = 本次改动之前定的）
  PRIMARY KEY (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_item_facts_fact
  ON feed_item_facts(fact_id);

-- 热度小时快照 (AIHOT 的 `story_heat_hourly`): 每小时给每一件事记一行热度, 页面那条线读它。
-- `complete` = 这一刻没有源在落后 —— 落后时先记成不完整, 等源追上再用 `grace=false` 重算一次
-- (证据带着源自己的时间, 所以过去的那一格是确定的)。`story_id` 是事件的键 (`s:<id>` 或规则桶
-- 的哈希键), 不是外键: 规则桶还没落进 `feed_stories`。
CREATE TABLE IF NOT EXISTS feed_story_heat_hourly (
  story_id     TEXT NOT NULL,
  hour         TEXT NOT NULL,
  heat         REAL NOT NULL,
  participants INTEGER NOT NULL,
  cohort       INTEGER NOT NULL DEFAULT 0,
  complete     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (story_id, hour)
);

CREATE INDEX IF NOT EXISTS idx_feed_story_heat_hourly_hour
  ON feed_story_heat_hourly(hour, complete);

-- 榜单快照 (AIHOT 的 `hot_rankings`): 每次算榜落一行, 页面读最新那一行。存下来是为了让
-- "卡片上印的名次"和"详情页说的榜内第 N 名"是同一份, 而不是各自算一次。
CREATE TABLE IF NOT EXISTS feed_hot_rankings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  computed_at   TEXT NOT NULL,
  rule_version  TEXT NOT NULL,
  entries_json  TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  published     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_feed_hot_rankings_time
  ON feed_hot_rankings(computed_at DESC);

-- 付费请求的收据 (AIHOT 的 `receipts`): 一次逻辑请求一行。`logical_key` 是「决定了输出的
-- 那一切」的哈希 (用途 / 模型 / 提示词 / 材料 / 温度 / 上限 / 显式重跑标签), 所以同一份材料
-- 再问一次不再付第二次钱 —— 重跑一整轮、取消后重来、进程重启, 都靠这一行把答案捡回来。
-- `response` 存的就是那份答案 (原样保存, 业务在写库前先落它)。
CREATE TABLE IF NOT EXISTS llm_receipts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  logical_key  TEXT NOT NULL UNIQUE,
  service      TEXT NOT NULL,
  model        TEXT,
  purpose      TEXT NOT NULL,
  subject      TEXT,
  status       TEXT NOT NULL,   -- pending | received | completed | failed | unknown
  response     TEXT,
  request_json TEXT,            -- 诊断用: 只有哈希与长度, 不放正文与密钥
  attempts     INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  received_at  TEXT,
  completed_at TEXT
);

-- 真的发出去的每一次请求一行 (AIHOT 的 `receipt_attempts`)。**预算数的是 attempt**: 重试
-- 躲不过预算, 而捡回来的那次根本没发、也不重复计费。`origin` 分 live 与 replay: 只有 live
-- 进预算。
CREATE TABLE IF NOT EXISTS llm_receipt_attempts (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id        INTEGER NOT NULL REFERENCES llm_receipts (id) ON DELETE CASCADE,
  attempt           INTEGER NOT NULL,
  service           TEXT NOT NULL,
  model             TEXT,
  origin            TEXT NOT NULL DEFAULT 'live',  -- live | replay | imported
  status            TEXT NOT NULL,                 -- pending | received | failed | unknown
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  latency_ms        INTEGER,
  error             TEXT,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  UNIQUE (receipt_id, attempt)
);


CREATE INDEX IF NOT EXISTS idx_llm_receipts_status ON llm_receipts(status);
CREATE INDEX IF NOT EXISTS idx_llm_receipt_attempts_service ON llm_receipt_attempts(service, origin, started_at);


-- 精选台账 (AIHOT 的 selected_ledger / selected_state): 哪一条什么时候进过首页、又什么时候下来了。
-- 我们这边精选是每次请求重算的派生结果, 没有发布事务可挂 —— 账挂在定时的一趟上 (jobs 'selected-ledger'),
-- 拿现在的集合与上一次的账对一遍, 进的记 upsert、出的记 remove。
CREATE TABLE IF NOT EXISTS feed_selected_ledger (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    TEXT NOT NULL,
  op         TEXT NOT NULL,            -- upsert | remove
  changed_at TEXT NOT NULL,
  visible_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feed_selected_ledger_item
  ON feed_selected_ledger(item_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_feed_selected_ledger_visible
  ON feed_selected_ledger(visible_at);

-- 上一次对账时这一条在不在集合里, 以及它最后一笔的 seq —— 差异只需要一个布尔.
CREATE TABLE IF NOT EXISTS feed_selected_state (
  item_id TEXT PRIMARY KEY,
  in_set  INTEGER NOT NULL,
  last_id INTEGER NOT NULL DEFAULT 0
);
-- 机器本地的键值 (5.9 签名密钥 / 5.12 管理台口令 / 5.8 飞书地址): 程序自己的状态,
-- 既不是清单也不是运行流水 —— 放 system.yaml 会被人手改坏, 放环境变量又活不过一个
-- 从页面点出来的动作。所以给它一张最小的表: 一个键、一个值、一个改动时刻。
CREATE TABLE IF NOT EXISTS feed_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);


-- 投递台账 (5.8, AIHOT 的 deliveries): 一条要发出去的消息, 发到哪、发过几次、成没成。
-- 目标是 webhook 的指纹而不是明文: 这张表会被运维读口翻出来, 而一串 webhook 地址
-- 就是一把 key。失败带下一次尝试的时刻, 所以退避是记在行上的, 不是靠重跑整轮。
CREATE TABLE IF NOT EXISTS feed_deliveries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  channel      TEXT NOT NULL,
  target       TEXT NOT NULL DEFAULT '',
  subject      TEXT NOT NULL DEFAULT '',
  body_json    TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_try_at  TEXT NOT NULL DEFAULT '',
  last_error   TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_feed_deliveries_status ON feed_deliveries(status, next_try_at);


-- 抓取队列 (1.2): 补正文的活儿一行一个, 带租约与退避。租约是这张表存在的理由 ——
-- 进程崩在半路时, 手上那几条还写着 leased, 而它们的租约一过期就被下一个 tick 认领,
-- 于是"崩了就丢"变成"崩了下一分钟接着跑"。同一对 (源, 外部 id) 只允许一行。
CREATE TABLE IF NOT EXISTS feed_fetch_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL,
  external_id   TEXT NOT NULL,
  url           TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_try_at   TEXT NOT NULL DEFAULT '',
  leased_until  TEXT NOT NULL DEFAULT '',
  last_error    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_fetch_queue_ready ON feed_fetch_queue(status, next_try_at);
-- 复合分析「提及」了哪些事实 (4.9b, AIHOT 的 `fact_articles.role = 'mention'`)。
-- `feed_item_facts` 的主键是报道, 所以一条报道只能**属于**一个事实; 而一篇盘点同时谈好几件
-- 事, 它不是那些事的证据, 只是提到了它们。这一张表就是那另一半: 一条报道对多个事实的
-- 「提及」关系。读材料的那几条路 (`feed_story_reports` 等) 只读 `feed_item_facts`, 所以
-- 提及天然不进综述、不进热度 —— 这正是这张表存在的理由。
CREATE TABLE IF NOT EXISTS feed_item_mentions (
  source_id   TEXT NOT NULL,
  external_id TEXT NOT NULL,
  fact_id     INTEGER NOT NULL,
  decided_at  TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_id, external_id, fact_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_item_mentions_fact ON feed_item_mentions(fact_id);

-- 被引用的那几句话单独译一份 (3.6, AIHOT 的 `quote_translations`)。主键是**被引帖的号**:
-- 十条帖子引同一句话只花一次钱。`text_hash` 是那句原文的指纹 —— 同一条帖子被改过就重译,
-- 而不是把旧译文配新原文。`origin` 记这份译文的来路 (`reused` = 被引的那条帖子自己收过、
-- 译过, 直接拿它的; `model` = 这一次问出来的)。
CREATE TABLE IF NOT EXISTS feed_quote_translations (
  tweet_id   TEXT PRIMARY KEY,
  text_hash  TEXT NOT NULL DEFAULT '',
  text_zh    TEXT NOT NULL DEFAULT '',
  origin     TEXT NOT NULL DEFAULT 'model',   -- model | reused
  created_at TEXT NOT NULL
);
