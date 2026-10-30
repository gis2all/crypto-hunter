# 监控

一个监控 = 一个长期问题（「哪些项目现在接受 Binance Agent ID」），不是单个项目。每个监控
自己抓、自己一套卡、自己一页、自己的节奏；两个监控之间不共享任何东西，同一个项目在两个
监控里各出一张卡是正常结果（user, 2026-10-29）。

| 件 | 位置 | 说明 |
| --- | --- | --- |
| 项目目录 | `monitors/<id>/` | 一个监控一个目录：`monitor.py`（`Monitor` 定义）、`prompts/`、`settings.yaml`。加项目 = 加目录，注册表自动发现。 |
| 注册表 | `registry.py` | `Monitor` 类型 + 扫描 `monitors/*/monitor.py` 的发现逻辑；`package()` / `prompt_dir()` 给引擎定位项目文件。 |
| 引擎 | `core/` | `job.py` 扫描、`chain.py` 链上、`schedule.py` 时钟、`settings.py` 设定。公共代码，不随项目走；新项目不复制。 |
| 查询 | `<id>/settings.yaml` 里的 `queries` | 每个监控的 X 高级搜索，一条一次搜索，多条各跑一遍；没写就回退 `monitor.py` 的默认值。查询和开关、节奏、采集与过闸在同一个文件里，设置页不编辑它。 |
| 提示词 | `<id>/prompts/` | 抽取提示词由 `Monitor.prompt` 指定；同一个目录里还有补字段的 `fill.md` 和摘要重写的 `summary.md`。读的时候才打开，改完下一轮生效。 |
| 设定 | `core/settings.py` + `<id>/settings.yaml` | 启用、计划任务、间隔、起始时 / 分、查询窗口、切片长度、过闸下限分、X 查询。一个监控一个文件，第一次读取就把各项按默认值写出来，手改和页面保存写的是同一份；缺的键回退 `monitor.py` 默认，删掉会按默认重建。设定页没有左栏，一列就是每个监控一块（开关 / 运行节奏 / 采集与过闸），顶部一个「保存并生效」写所有块。 |
| 一次扫描 | `core/job.py` | 搜 X（复用 `opportunity/xread.py` 的切片与节流）→ 免费闸（`opportunity/rules.py`）→ 分批模型调用 → 溯源（复用 `opportunity/trace.py`，落 C 才补二次搜索）→ 链上 → 落卡 → 官网刷新。可中止。 |
| 时钟 | `core/schedule.py` | `anchor + k × interval`，默认 01:15 / 8 小时，与主扫描错开；跑过就记这一格用掉。 |
| 接口 | `api/v1/monitors.py` | 列表、卡片、设定、`POST .../scan`、轮询、中止。 |
| 表 | `monitor_cards` / `monitor_scans` / `monitor_posts` | 卡按 `(monitor, slug)` 唯一，重写保留 `first_seen`；扫描是回执；`monitor_posts` 是待读帖子队列（按 `tweet_id` 去重）。 |

## 一条扫描的七个阶段

`collect` 搜 X → `gate` 免费闸 → `extract` 模型读成项目 → `trace` 找官方入口 → `chain` 读链上 →
`store` 落卡 → `refresh` 刷新官网。
计数写在扫描行上（帖子 / 过闸 / 入队 / 项目 / 批次 / 写入 / 刷新 / 未变 / 清理），页面只轮询那一行。

**帖子先入队**（`monitor_posts`）：抓到的帖子过闸后按 `tweet_id` 写进队列，`status = pending`；
抽取只消费 `pending`，读完置 `used`。所以第二轮读的是“9:15 之后的新帖”，而不是把 24 小时再读一遍
——这是“多跑几轮真的会更多卡”的前提（2026-10-30, user）。某一批模型调用失败时帖子留在队列里，
下一轮重试。队列只保留 30 天（每轮落库后按 `first_seen` 清理，计数 `pruned`），它是工作集不是档案。

**抽取是分批的**：过闸帖子按 `max_posts`（100）切批，一轮最多 5 批（500 条），四个批次并发跑模型；
每批最多 20 条项目，合并去重（同一 handle 只留一条，空字段补齐、facts 并集）后**整轮最多 20 张新卡**
——宁缺毋滥。扫描计数里多记 `batches` 和 `posts_sent`。

**官网刷新是每轮的最后一段**（`refresh`）：按每张卡自己的 `official_url` 重读官网，只更新铸造进度、
价格、截止与 `fields.site_checked_at`；标题、类型、摘要、信任等级都不动——这次抓取失败也不会把卡片
写坏。本轮刚写入的卡跳过（刚落库时已经读过站）；页面内容与 `fields.site_hash` 相同就只更新时间，
**不调模型**，只有页面真的变了才解析进度并补价格 / 截止。进度以**模型读整页为主、正则兜底**；
进度只向前（比上一轮小就丢弃），「每钱包上限 / limit」附近的 `x / y` 不算铸造进度。它不消耗 X 调用，计数记
`refreshed`（变了）与 `unchanged`（没变）。老卡因此不必等“再有人发帖”才会更新进度。

**链上**：合约地址来自模型字段、摘要 / 事实或官网正文；页面上常同时印着 mint pass、forge、
emitter 和代币，所以整页地址都收进来，逐个 `eth_call` 试，第一个答上「已铸造」的当铸造合约。
**代币是另一个地址**：铸造合约先用 `token()` / `tokenAddress()` / `assetToken()` / `saleToken()`
/ `tokenContract()` / `paymentToken()` 指向代币（JAGENT 的 `totalMinted()` 合约就是这样指到
`0x8e64…ae15`）。地址要同时答上 `symbol()`、`decimals()`、`totalSupply()`，且 `supportsInterface
(0x80ac58cd)` 不为真，才算代币 —— 官网印的地址同样要过这一关，过不了就当没有。GMGN / Debot
只挂验证过的代币；NFT 和铸造合约没有对应页面（2026-10-30, user）。

## 卡片类型

类型按「你买到什么」分（`job._type`），chip 排在链名后面；没命中的不出 chip，卡面照常显示已有的数。

| key | 中文 | 判定 | 卡面 |
| --- | --- | --- | --- |
| `nft` | NFT | 单位是 card / egg / farmer / NFT；或 ERC-721；或正文出现 NFT / profile card / collectible | 铸造价 + 进度 |
| `share` | 代币份额 | 单位是 份 / shares / 份额 / token；或验证过的 ERC-20（含铸造合约 `token()` 指向的） | 铸造价 + 进度 + 安全 + GMGN/Debot |
| `inscription` | 铭文 / 符文 | 标题 / facts 出现 runes / inscription / ordinals / brc-20 / 铭文 / 符文 | 铸造价 + 进度 |
| `launchpad` | 发射台 | 标题 / facts / 摘要，或官网 `<title>` / `og:title` 出现 launchpad / 发射台 / 发射平台 / 发币平台 / 发行平台 / 打新平台 | 只有说明 |
| `task` | 任务 / 积分 | `mint_kind` / `asset_type = task`，或摘要出现 quest / 签到 / 打卡 / 积分 / 任务 | 只有说明 |
| `unknown` | — | 都不命中 | 有什么显示什么 |

顺序：铭文（标题 / facts）→ 单位份额 → 发射台 → 任务（`mint_kind` / `asset_type`）→ 单位物件（NFT）
→ 链上 ERC-721 → 验证过的 ERC-20 → 模型给的 `asset_type` → 任务动词 → NFT 字样 / profile card
→ 只有摘要提到铭文 → 有分母的按份铸造（单位写 shares 记份额，否则记 NFT）→ unknown。

判定约束：

- **单位优先于链上和平台**：单位写着 份 / shares 的就是份额（BIBIAI 按份卖，摘要把它叫发射平台也
  不改判），写着 cards / farmers 的就是 NFT（Phosphor、Tiny Farm），哪怕旁边有 ERC-20、哪怕铸造
  合约的 `token()` 指向另一个币。
- **官网标题优先于摘要**：GANG 的页面标题就是 `Agent-only launchpad on BNB Chain`，抽取摘要没写；
  标题存进 `fields.page_title`（老卡也回填过一次）。
- **摘要不进第一梯队**：摘要是模型写的，会照抄 KOL 的叫法（BNBS 被原帖叫「符文项目」，官网写的是
  `10 shares per wallet`）。铭文只看标题 / facts，摘要在最后才作为兜底；判定用的是**抽取阶段**的摘要
  （`summary_extract`，现在会随卡片落库，误判时可回看），不是溯源后重写的那份。
- **官网原文只用于** `profile card` / `collectible` 这一档；前 2000 字存进 `fields.page_excerpt`，回填时
  直接复用，不必再抓一次站。
- **没有官网不建卡**：只有社区号、没有站的项目直接跳过，扫描计数记 `skipped_no_site`（Polka Agents）。
- **摘要重写不覆盖空话**：模型写回「官网原文未提供 / 无法确认项目内容 / 未说明参与」时丢弃，保留原摘要；
  官网链接在 https 可用时统一升级。

报告页左栏是四组：项目 / 类型 / 状态 / 排序。类型计数按全量；状态计数跟着已选类型收敛（选了 NFT，
下面只剩 NFT 的未开始 / 铸造中 / 已结束），发射台与任务单列「不适用」。排序是 发现时间 / 截止时间 +
从新到旧 / 从旧到新，没有截止的卡在两个方向下都排最后。

**标题**取官网 `<title>` / `og:title`（去掉 `|`、`—` 这类站点后缀），且必须和模型给的名字有共同词，
否则退回模型标题——营销标语（"Weave ideas into markets."）和 Cloudflare 挑战页（"Attention
Required!"）都不是项目名。模型每轮换叫法（JAGENT → JAGENT Mint Pass），站点名不会。

**状态**：发射台 / 任务不参与铸造状态，筛选里单列「不适用」；其余按进度 → `card.status` → 未判定。
补字段时 `未判定` / `unknown` / 空白都算缺失，模型写下的占位符不会挡住补价格。

**安全**是卡片上的一行 chip，只出现在**验证过代币**的卡上（NFT / 任务合约没有池子，也没有这套开关）。数据来自 GoPlus；GoPlus 没给 LP 表时读池子本身（几次 `eth_call`）：

| chip | 判据 | 状态 |
| --- | --- | --- |
| 貔貅 | `is_honeypot` 模拟买卖；税率到 10% 转黄、20% 转红（低于 1% 不占位） | 貔貅 / 非貔貅 · 税 N% / 无池 / 未判定 |
| LP | `lp_holders[]` 里锁定或烧掉的份额，与流动性金额无关；GoPlus 没给表时问池子自己 | LP 已烧 / LP 已锁 N% / LP 锁 N% / LP 未锁 / 未组池 / 已撤池 / 无池 / LP 未判定 |
| 权限 | `dangerous_perms`（增发、暂停、黑名单、隐藏 owner、代理等） | 命中项 +N / 无危险权限 / 公开铸造 / 未判定 |

`is_mintable` 要和 owner 一起读：合约有真实 owner 才叫「可增发」（红）；JAGENT、BabyJagent
没有 `owner()`、SWARM 的 owner 是 `0x0`，它们的增发来自公开铸造，谁也关不掉、谁也不能单方面
多印，记作灰的「公开铸造」而不是风险（`job._mint_capability`）。

**撤池**只有跨轮才判得出来：`_rug()` 拿上一轮卡片的 `pool` / `pool_liquidity_usd` 和这一轮比，
池子没了记 `gone`，流动性掉 80% 以上记 `drained`；这一轮根本没回答（既没池子列表也没流动性）
不算撤池。第一轮没有对比对象，什么都不写。

GoPlus 不给 `lp_holders` 时（BabyJagent），`lp_from_pair()` 直接读池子：`getReserves()` 两边都是 0
或 LP 总量为 0 记「未组池」；否则把黑洞地址和已知锁仓合约（Unicrypt / PinkLock / Team Finance）
的 `balanceOf` 加起来算锁仓份额。核对不到份额时写「LP 未判定」，**不**写成「未锁」——没看见不等于
看见了没有。

**时间**：`start`（开卖 / 开放参与）和 `deadline`（截止）分开存，互不顶替；页面写 UTC / GMT 的由
`_when()` 加 8 小时换成 Asia/Shanghai（SWARM 的 "mint opens 15:00 UTC" 曾被当成 15:00 的截止）。
卡面时间行是 `发现 / 开始 / 截止`，缺哪个写未公布。

**溯源**：每个项目先取候选官网与候选账号，再读官网和账号资料页。官网提到项目名或 handle、
且账号资料页链回同一域名才算 `A`（官方）；只有同名账号是 `B`（社区，未互证，卡片上标出来）。
**B 也必须带官网才建卡**：只有社区号、没有站的项目直接跳过（Polka Agents，2026-10-30, user）。
找不到官方来源是 `C`，不写卡。这条规则与「新机会」是同一条（`trace.verdict()`），所以两页
对「官方」的说法不会分叉。

常规溯源落 `C` 之后才补**二次 X 搜索 + 网页搜索**：帖子里既没有 @ 也没有链接时，这是唯一能跟的线。
不做成每次都搜，是因为搜索会花 X 调用、也会把限流引到后面的资料页上，让本来能判 A 的卡一起掉 C。
单个项目 120 秒超时（`CH_TRACE_LEAD_TIMEOUT`），超时计一次失败、继续下一个；落 C 的项目连同
「有没有找到官网 / 读到几页 / 二次搜索跑没跑」一起写进扫描计数，运行面板的「跳过 N 个 · 看原因」里能展开。

## 两个开关

`启用` 决定这个监控**存不存在于报告页**：关掉后左侧不再列它、卡片一并隐藏（数据保留，重新
启用就回来），自动任务也停；块里其余控件灰显。`计划任务` 只决定**按不按点自动跑**：关掉后
时钟不再为它启动，但项目还在、手动「立即运行」照常可用。两者互不代替（2026-10-29, user）。

## 加一个监控

1. 新建 `monitors/<id>/`，在 `monitor.py` 里写 `MONITOR = Monitor(...)`（默认查询、卡片字段、节奏）；注册表会自动发现它。
2. 在 `<id>/prompts/` 放三份提示词：`<id>.md`（只输出什么 JSON）、`fill.md`（补价格 / 进度 / 截止）、`summary.md`（按官网重写摘要）。
3. 在 `<id>/settings.yaml` 按默认值写查询、开关、节奏、采集参数（不写也行，第一次读取会自动生成）。
4. `web/src/features/monitors/MonitorsView.tsx` 的 `CARDS` 里注册这个监控的卡片组件。

这也正是「每个监控一个定制前端页面」的代价：一次代码改动，而不是一份配置。
