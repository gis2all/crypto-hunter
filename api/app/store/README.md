# 数据与状态

数据模型以 `schema.sql` 为准；`db.py` 负责连接、迁移（`_ADDED_COLUMNS`，配 `SCHEMA_VERSION`）与本机库文件。

## 数据模型

表分两类：改设定的动作追加行，一轮跑出来的结果行落库即冻结。加列必须同时改 `schema.sql` 与迁移，老库靠迁移补齐并回填。

| 规则 | 说明 / 反例 |
| --- | --- |
| 数据模型以 `api/app/store/schema.sql` 为准（23 张表：一轮的 13 张 ＋ 新机会的 6 张 ＋ 链注册表 1 张 ＋ 监控 3 张）。加列走 `db.init_db` 的 `_ADDED_COLUMNS`（配 `SCHEMA_VERSION`）；`chain_registry` 由 `chainverify` 自动维护。 | **不应直接修改 `.db`** —— 旧库不会自动补出那一列。 |
| 三种表不应改写：`settings_versions`（改设定 ＝ 追加一行）、`snapshots`（每轮每币一写）、`reports`（落库即冻结）。`favorites` **不在此列** —— 那一行说的是「这个币现在该看哪张卡」，不是某一轮说了什么（见 `web/src/features/README.md` · 收藏页）。 | 就地 UPDATE 已发布报告，历史轮次与当时的卡片就对不上。 |
| 新机会 v1 的六张表按同一条规则分两侧：`opportunity_posts` 是**原始材料**（只增不改；官网刷新写进来的行也是它），`opportunities` / `opportunity_steps` / `opportunity_sources` 是**判断**（可编辑，条目带 `novelty`，query 行带 `family`），`opportunity_scans` 是回执（只增），`opportunity_sites` 是官网刷新的轮转表（按域名一行，存快照、失败次数与 `last_refresh_at`）。`opportunity_pages` 在 2026-10-18 随非 X 来源一起删掉。`track` 两列自 2026-10-24 起不再写入，保留只为旧行可读。 | 一份原始帖子被后一轮改写，就没有东西能回答「当初为什么把它判成活动」；反之判断不可改，规则升级后旧条目就成了无出处的断言。 |
| 监控的三张表：`monitor_cards` 是判断（按 `(monitor, slug)` 唯一，重写保留 `first_seen`），`monitor_scans` 是回执，`monitor_posts` 是待读队列（按 `tweet_id` 去重，`pending` → `used`，只保留 30 天）。 | 帖子不落队列的话，每轮会把同一个 24 小时窗口重读一遍，卡片数永远长不上去（2026-10-30, user）。 |
