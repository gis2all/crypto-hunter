# 数据与状态

数据模型以 `schema.sql` 为准；`db.py` 负责连接、迁移（`_ADDED_COLUMNS`，配 `SCHEMA_VERSION`）与本机库文件。

## 数据模型

表分两类：改设定的动作追加行，一轮跑出来的结果行落库即冻结。加列必须同时改 `schema.sql` 与迁移，老库靠迁移补齐并回填。

| 规则 | 说明 / 反例 |
| --- | --- |
| 数据模型以 `api/app/store/schema.sql` 为准（18 张表：一轮的 13 张 ＋ 新机会 v1 的 5 张）。加列走 `db.init_db` 的 `_ADDED_COLUMNS`（配 `SCHEMA_VERSION`）。 | **不应直接修改 `.db`** —— 旧库不会自动补出那一列。 |
| 三种表不应改写：`settings_versions`（改设定 ＝ 追加一行）、`snapshots`（每轮每币一写）、`reports`（落库即冻结）。`favorites` **不在此列** —— 那一行说的是「这个币现在该看哪张卡」，不是某一轮说了什么（见 `web/src/features/README.md` · 收藏页）。 | 就地 UPDATE 已发布报告，历史轮次与当时的卡片就对不上。 |
| 新机会 v1 的五张表按同一条规则分两侧：`opportunity_posts` 是**原始材料**（只增不改），`opportunities` / `opportunity_steps` / `opportunity_sources` 是**判断**（可编辑，条目带 `novelty`，query 行带 `family`），`opportunity_scans` 是回执（只增）。`opportunity_pages` 在 2026-10-18 随非 X 来源一起删掉。`track` 两列自 2026-10-24 起不再写入，保留只为旧行可读。 | 一份原始帖子被后一轮改写，就没有东西能回答「当初为什么把它判成活动」；反之判断不可改，规则升级后旧条目就成了无出处的断言。 |
