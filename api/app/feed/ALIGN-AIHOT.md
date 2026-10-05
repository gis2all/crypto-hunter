# ALIGN-AIHOT —— 与 AIHOT 的差异台账

**这份文件不是什么.** 它不是设计文档 —— 设计在 `DESIGN.md`, 为什么这么做也在那里.
它只回答一个问题: **AIHOT 有、我们还没有的那几样, 各是什么, 差在哪, 归到哪一期.**

怎么用:

1. 动手之前, 先在这里查一眼要动的那一件事有没有编号 (没有就先补一行, 免得做完了没人记得它做过).
2. 做完一项, 把那一行从 §3 的台账挪到 §1 的底账, 并补一句"差在哪"变成"是什么".
3. `DESIGN.md` 里"见 `ALIGN-AIHOT.md` 4.8"这样的引用, 指的就是 §3 那张表里编号那一行.

> **2026-10-05 重建说明.** 这份文件被一次编辑事故清空成 0 字节, `.git` 里没有
> (它还没进过版本库), 编辑器的本地历史也没有. 下面是按当轮的记录重建的版本:
> §1 是和 AIHOT 对齐了的那一批, §2 是明确不做的, §3 是还差的 (现在是空的). **行数以本表实际行为准**, 不再追一个对不上的旧计数.
> §2 那九条是用户明确说不做的, 留着是为了下一个读到"AIHOT 有而我们没有"的人不用重新判断一次.

## 1 底账: 已经和 AIHOT 对齐的

| 阶段 | # | 是什么 |
| --- | --- | --- |
| 采集与正文 | 1.5 | 正文的档位 (`body_mode`): `full` / `summary` / `link-only` 三个词只在一处定义 (`publication/rules.ts` 的 `bodyModeOf`) |
| 采集与正文 | 1.9 | 源体检: `GET /feed/sources` —— 每一家最近一次抓取、条数、错误 |
| 采集与正文 | 1.10 | 抓取出口的 SSRF 守卫 (`feed/lib/http_policy.py` 的 `guard` / `check`), 取源与取正文两条路都过 |
| 预筛与花钱 | 2.3 | `report` 这个模型能力位 (`llm.models.report` / `CH_LLM_MODEL_REPORT`), 以及设定页改动写审计 |
| 预筛与花钱 | 2.8 | 审计台账: `feed_audit` 表 + `GET /api/v1/system/audit` |
| 发布与页面 | 5.1 | 发布门: 一行公开之前要过的延迟 (`released_at`), 读侧的老实现在 `scope.py` |
| 发布与页面 | 5.2 | 列表的分页与搜索 (`channel` / `query` / `page` / `cursor` / `page_size`) |
| 发布与页面 | 5.3 | 游标 (`feed/lib/cursor.py`): 读"上一页最后那一条之后", 不用 OFFSET |
| 发布与页面 | 5.10 | `sitemap.xml` / `llms.txt` / `robots.txt` (`api/app/api/root.py`, 挂在 SPA 之前) |
| 发布与页面 | 5.13 | `GET /api/v1/feed/agent` —— 给 agent 读的一份说明 |
| 发布与页面 | 5.14 | `GET /api/v1/feed/v1` —— 版本与能力清单 |
| 发布与页面 | 5.15 | RSS / Atom: `GET /feeds/{channel}.xml` |
| 发布与页面 | 5.16 | `GET /og/{item_id}` —— 分享卡那张图 |
| 发布与页面 | 5.17 | 单条的 Markdown 导出: `GET /api/v1/feed/item.md` (`views.item_markdown`) |
| 发布与页面 | 5.18 | 已公开判定的有效性: 读者钟早于该行 `seen_at` 时也当它已经公开 (读历史) |
| 发布与页面 | 5.21 | 站点绝对地址只有一个出处 (`feed/site.py` 的 `SITE_URL`) |
| 发布与页面 | 5.23 | 渠道键: 卡片走哪一路 (`rules.channel_of`), 列表按它筛、计数在筛之前 |
| 发布与页面 | 5.24 | 站点身份: `outlets.site_meta` (站名 / 联系方式 / 采集器 UA 的单一出处; 只剩 MCP `feed_site` 在读) |
| 发布与页面 | 5.26 | 展开 / 收起的进出场动画 (`features/feed/Presence.tsx`, CSS keyframes, 不引动画库) |
| 发布与页面 | 5.27 | 详情页的「事件后续」(`ItemPage.tsx`, 数据是后端的 `followups`) |
| 发布与页面 | 5.28 | 热点榜参与者头像 (`features/feed/Faces.tsx`, 数据是后端的 `faces`) |
| 发布与页面 | 5.29 | 列表的按天时间轨 + 滚到底取下一页 (`ItemsPage.tsx` + `mergeDays`) |
| 出口与路由 | routes/static | 根路由与 `StaticFiles` 的顺序: root 那一组必须先挂上 |
| 出口与路由 | lib/* | 通用小件: `ids` / `cache` / `cursor` / `calendar` / `text` / `http_policy` / `shutdown` |
| 写作 | 提示词缺值报错 | `writing.read_prompt` 展开之后还剩占位符就报错, 不静默留白 |
| 出口与路由 | 搜索并发闸 | 搜索最多 4 个在跑、8 个排队, 超了回 503 + `Retry-After` |
| 写作 | 北京日历单一出处 | 日期 / 星期 / 时刻只从 `feed/lib/calendar.py` 来, 页面不自己算 |

| 采集与正文 | 1.2 | 持久抓取队列 (`feed_fetch_queue`): 补正文入队 + 租约 + 退避, 崩了下一轮接着跑 (`sources.details`) |
| 采集与正文 | 1.11 | 源图标: `sources.icons` 那条活儿把 `icon` 收回来 |
| 采集与正文 | 1.12 | 多角色 cron 与 `job_runs`: `feed/jobs.py` 的槽表 ＋ 每一趟的账 (`GET /feed/ops`) |
| 预筛与花钱 | 2.3b | 写作站覆盖率 (`feed_items_coverage`) 与 `llm_stage` 分档进度: 卡在哪一步看得见 |
| 理解与写作 | 3.1a | 正文里第一张图当卡片图 (`htmlbody.images` 落 `media_json`; 源自带 media 时优先用源给的) |
| 理解与写作 | 3.9 | 行内标签占位符协议: 记号一个不少, 对不上就留原文等下一轮 (`htmlbody.shields` / `restore`) |
| 理解与写作 | 3.10 | `display_tags`: 角标串里哪些是可点的实体 (AIHOT 的 `displayTags`) |
| 事件与分组 | 4.9b | composite 分析: 盘点只**提及**、不立事件 (`feed_item_mentions` ＋ `analysis_scope`) |
| 发布与页面 | 5.4 | 卡片字段细目 (`DESIGN.md` 第 35 节) |
| 发布与页面 | 5.5 | 资格集中: `eligibility.GATES` 一份有序名单 ＋ `GET /feed/eligibility` |
| 发布与页面 | 5.6 | 发布台账: 定时那趟写, 读口 `GET /feed/ledger` |
| 发布与页面 | 5.9 | 媒体多规格与签名代理 (`feed/media.py`: `image-336/720/1200/1600` ＋ `proxied_image`) |
| 发布与页面 | 5.11 | operations 运维读口 (`GET /feed/ops`): jobs / job_runs / fetch_runs / audit / coverage / queue / deliveries |
| 发布与页面 | 5.12 | 口令与那道门 (`feed/auth.py`; `/feed/admin/*` 守着 `/run` 与 `/override`); 设置页那个管理台面板 2026-10-05 移除, 页面不再有入口 |
| 发布与页面 | 5.20b | SPA catch-all 分两档: 带点回 404, 不带点回 `index.html` |
| 出口与路由 | routes/mcp | MCP 出口: `POST /api/mcp` (JSON-RPC 2.0, 只读, `feed/mcp.py`) |
| 周边 | 5.7 | 报刊的模型撰写 (`prompts/edition.md` ＋ `reports.write_issue`; 一期一行落库) |
| 周边 | 5.8 | 飞书推送 (`feed/delivery.py` ＋ `notify.deliver`; 地址是 key, 幂等在表上) |
| 周边 | 5.22 | 金标评测 (`feed/eval.py` ＋ `eval_golden.json`; `GET /feed/eval`) |

| 理解与写作 | 3.6 | 别人的原话单独译一遍: 按被引帖的号去重, 结果写回条目 (`feed_quote_translations` ＋ `writing.translate_quotes`) |
| 事件与分组 | 4.8a | 等重判的那些不算证据 (`db._TRUSTED_EVIDENCE`, AIHOT 的 `recall.trusted`); 人工钉过的一直算 |
| 事件与分组 | 4.8b | 人工入口 ＋ 审计: `feed_grouping_overrides` ＋ `GET/POST/DELETE /feed/override`, 每一步进 `feed_audit` (actor) |
| 事件与分组 | 4.10 | 折叠规则: 同一件事别家的报道按**每家只留最新一条**折叠 (`views.other_reports`), 这条自己的来源不算 |
| 发布与页面 | 5.25 | 阅读位置: 折着的几天与屏幕上的那一条记在会话里, 换页回来还在原处 (`features/feed/restore.ts`) |
| 事件与分组 | 4.12 | 事件页的官方报道: `views.official_reports` —— 当事方自己发的那些 (`first_party`), 与别家的说法分开印 |
| 发布与页面 | 5.30 | 正文目录: `htmlbody.with_outline` 给 h2–h4 编 `sec-N` 并注进正文, 详情页照它跳 |
| 周边 | 5.31 | 报刊的要点与快讯: `reports.highlights` / `flashes` 随快照存, 翻期读 `feed_edition_neighbors` |

## 2 明确不做

用户明确说过不做的十条, 留在这里当一个显式的"不", 不是"还没做":

| # | 不做什么 | 为什么 |
| --- | --- | --- |
| 1 | `leaderboard/*` | 我们没有榜单这个东西 |
| 2 | `monitor/*` | 监控是另一个产品 |
| 3 | `industry/features.ts` | 行业页的栏目表, 我们的页签是页面自己的版式 |
| 4 | `dajiala` + `sources/mp.ts` | 微信那一套取源, 不接 |
| 5 | Jina 车道 | 取正文不走它 |
| 6 | `ingest/items.ts` | 我们入库那条路是自己的一套 |
| 7 | `docker-compose` | 单进程后端 + 前端静态包, 目前不欠这一套栈 |
| 8 | 1.1 `EXCLUDE_MP` | 它存在的前提是接微信公号; 那一整套取源明确不接 (见第 4 条), 于是没有可排除的对象 |
| 9 | 4.6a 主题模型化 | 我们的主题是「这条牵了谁」的实体 (4.11, 用户定于 2026-10-03); 再来一套按模型的赛道聚类, 等于把同一批条目换个名字再列一遍 |
| 10 | 5.19 ＋ 5.20a 静态页 | 用户 2026-10-05 要求移除「关于」: 左栏那一行 (关于 / 隐私 / 联系) 与三页一起去掉, `site.PAGES`、`/feed/site*` 与 sitemap / llms.txt 里的地址一并删掉 |

## 3 §4.0 剩余台账

2026-10-05 复查又把 §3 清空过一次 (那一批是 3.6 / 4.8a / 4.8b / 4.10 / 5.25), 现在这里留的是
同一趟复查里新发现、**用户还没定**的两组 —— 它们既没做, 也不在 §2 的"明确不做"里:

| # | 差什么 | AIHOT 那边是什么 |
| --- | --- | --- |
| 4.13 | 人工更正的分组动作只有「钉住 / 撤销」 | `events/corrections.ts` 还有「把一条移进指定的事件」与「合并两个事件」(走行锁, 自动分组不许撤销) |
| 5.32 | 运维面与后台的两页 | `operations/{alerts,watch,heartbeat,backup,indexnow,reports}.ts` 的告警/心跳/备份/IndexNow/周报, `admin/{models,selectbench}.ts` 的模型与评测台, `operations/feedback.ts` 的收件与滥用控制 —— 我们只有 `/feed/ops` 这一个读口 |

留在这一节的行, 说明它还没做完 —— 空的一节比一行"差不多也行"诚实。
