# 数据源（Arc）

状态：**已探测**（2026-09-24 10:04 +08）；X 搜索挖链层 2026-09-28 接入（见「挖链」一节）。

- 探测脚本：`api/scripts/probe_sources.py`（在仓库里）
- 原始结果与样本 payload：`data/probe/*.json` / `*.raw` —— **本机产物，`data/` 不入库**，重跑探针即可再生

## 结论表

| 源 | 端点 | 用途 | 传输 | 实测 | 需 key |
| --- | --- | --- | --- | --- | --- |
| GeckoTerminal | `api.geckoterminal.com/api/v2/networks/arc/{pools,new_pools,trending_pools}?page=N` | 全量池：市值、流动性、成交、创建时间 | httpx | 200，20 条/页 | 否 |
| GeckoTerminal | `.../networks/arc/tokens/{ca}`、`.../tokens/{ca}/pools` | 单币补池、池列表 | httpx | 200 | 否 |
| GeckoTerminal | `.../networks/arc/pools/{pool}/ohlcv`、`/trades` | K 线、成交明细（要用真实池地址） | httpx | 200 | 否 |
| RadarDEX | `api.radardex.pro/tokens?limit=2000&sort=mcap&dir=desc&window=24h` | 持有人、Top10、捆绑、狙击、老鼠仓 | httpx | 200，2.1 MB，count≈4145 | 否 |
| GMGN | `gmgn.ai/defi/quotation/v1/rank/arc/swaps/{period}` | 补持仓、活跃度 | **curl_cffi** | httpx/urllib 403；curl_cffi 200 | 否 |
| GMGN | `gmgn.ai/api/v1/{token_stat,token_dev_info,token_info}/arc/{ca}` | 貔貅占比、机器人 / 新钱包 / 私人金库 / 信号数、dev 持仓与改名史、开盘时间与供应量 | **curl_cffi** | 同上 | 否 |
| GMGN | `gmgn.ai/api/v1/token_security_evm/arc/{ca}` | **买卖税率**（`total_buy_tax` / `total_sell_tax`，0..1 小数）、貔貅判定、`lock_summary`、`top_10_holder_rate`、权限 | **curl_cffi** | 200；`/defi/quotation/v1/tokens/security/arc/{ca}` 只回 `{"goplus": {}}`，没有税率 | 否 |
| GoPlus | `api.gopluslabs.io/api/v1/token_security/5042?contract_addresses={ca}` | 危险权限、貔貅判定、池清单（`dex[]`）、大户表（`holders[]`）、**LP 持有人表（`lp_holders[]`，LP 锁定的数据源口径）**、owner/creator 持仓、10 个 yes/no 标志（34 个字段里用 31 个） | httpx | 200 | 否（免费档） |
| Sourcify | `sourcify.dev/server/v2/contract/5042/{ca}` | 源码已验证（双源之一） | httpx | 200 命中 / 404 未收录 | 否 |
| Blockscout | `explorer.arc.io/api/v2/...` | 备份：验证状态、创建者 | httpx | 200 | 否 |
| Arc RPC | `rpc.arc-scan.org` | 链上直读：decimals、转账与池事件 | JSON-RPC | 可达 | 否 |
| DexScreener | `api.dexscreener.com/latest/dex/tokens/{ca}` | **社媒 / 官网 / GitHub 链接**（`pairs[].info.socials` / `info.websites`）；逐币，不参与全链扫 | httpx | 200，7 个样本 4 个有链接 | 否 |

未采用：`/networks/arc/tokens`（列表形式）返回 404 —— Arc 没有 token 级列表端点，universe 只能从池聚合出来。

## 关键结论

1. **GMGN 必须走 `curl_cffi`（`impersonate="chrome"`）。** httpx / urllib 一律 403，返回的是 Cloudflare 挑战页（`text/html`），不是限频。依赖已固化为 `curl-cffi`，代码走 `sources/http.py` 的 `TRANSPORT = "browser"`。
2. **universe 的主源是「池」不是「币」。** GeckoTerminal 分页拉池（`FULL_PAGES = 12`，全量 ≈ 3249 个池/币），RadarDEX 一次拉全量做补充（`limit=2000`，覆盖 ≈ 4145）。
3. **Sourcify 在 Arc 覆盖很差。** 收录少，所以「双源验证」（GoPlus + Sourcify）会把 165 个候选砍到 1–2 个入选。这是入选数偏少的直接原因，不是 bug；想放宽就把设定页的源码验证切到「仅 GoPlus」。
4. 没有任何源返回 `X-RateLimit-*` 头；GMGN 的拒绝是反爬，不是限频。
5. 探测到的响应都是 `application/json`（GMGN 被拒时是 `text/html`），字符集 UTF-8。
6. **RadarDEX 的「市值」和 GMGN 不是一个东西，别把它的排序当全链排名。** 实测：`sort=mcap&dir=desc` 第 1 页 2000 行覆盖 mcap $5,149–$1.94M（响应里 `count` = 4147），但一轮里被富集的 82 个候选只有 2 个落在这 2000 行内 —— 入选的 5 个按 GMGN 市值是 $11K–$154K，按 RadarDEX 市值却排在页外。所以它对我们的作用是**长尾补充**，不是「全链底表」；市值口径以 GMGN / GeckoTerminal 为准（`FIELD_PRIORITY.market_cap_usd` 也是这个顺序）。
7. **GeckoTerminal 的例行页每轮都拉，只有长尾缓存。** `universe_pools()` 的 `ROUTINE_PAGES` 页 + `new_pools` / `trending_pools` 不走缓存（实测每轮 5 次请求、9–18s），12 页的长尾扫描挂在 `gt:sweep`（6h）上。逐币的 `gt:pools` 缓存 12h、`gt:ohlcv` 6h，所以「GT 没收录这个币」只会每 12h 重问一次。池数量（`pools`）另有 RadarDEX 兜底：它是唯一在批量行里带这个字段的源（3013/3013）。
8. **Arc 上「税」只有 GMGN 一张表，别把别处的 0 当读数。** 三个地方会给出一个买/卖税，其中两个是占位符：① GMGN 排行行 `rank/arc/swaps/*` 的 `buy_tax`/`sell_tax` —— 实测 100/100 全是 `"0"`；② GoPlus `token_security/5042` 的 `buy_tax`/`sell_tax`/`transfer_tax` —— 抽 24 个标的实测 24/24 全 0；③ 只有 `gmgn.ai/api/v1/token_security_evm/arc/{ca}` 给出真数（抽样 6 个：1% / 2% / 2% / 3% / 4% / 5%）。注意这个端点自己的 `buy_tax` 也是 `"0"`，要读 `total_buy_tax` / `total_sell_tax`（0..1 小数，含全局税的转账税部分），即 GMGN 页面上显示的那个数。因此 `FIELD_PRIORITY` 的 `buy_tax_pct` / `sell_tax_pct` **只列 gmgn**：把 GoPlus 留在兜底里，等于让它的占位 0 变成卡片上的数字（这正是「报告里税率全是 0」的根因）。GMGN 没有表的标的显示「—」。

9. **Sourcify 的 404 只值得一次尝试。**
sourcify note 404 = 「我这儿没有它的记录」，是答案不是失败：`tries=1`，并把 `match: none` 记进缓存（24h）。否则每个候选白等一个 2s 退避（实测 20 个候选浪费 65.6s）。
10. **Arc 节点的单次 `eth_call` ≈ 2.2–2.5s，这是读路径的硬地板。** 实测：单次请求 2.2–2.5s，与查什么无关（`eth_getCode` 与 `eth_call` 同量级）；并发几乎免费（1/4/8/12 并发分别 2.16/2.37/2.88/3.56s），所以「多问几件事」的正确姿势是并发或打包批量，不是省着问。批量确实省：同样 2 次请求，单发 4.61s、打包成一个 JSON-RPC 批量 3.54s。`eth_getLogs` 的窗口按 `LOG_WINDOW = 20000` 切、4 个窗口并发（`LOG_WINDOW_WORKERS`）；窗口被节点拒绝对半递归，所以**同一地址的 getLogs 调用次数轮间不可比**（见 `CLAUDE.md` 的 9.1 读路径）。

## 分级策略（已实现）

粗筛只吃池级数据：一次分页拉全量，套市值 / 流动性 / 成交三条硬过滤，把三千多个币砍到一两百个候选；再对候选逐币查持仓与安全。**不对全量逐币发请求。**

## 链下调研读的「源」：项目自己的页面 + 本轮搜到的页面

`report/research.py` 不接新的数据台：它读的是外链 —— 本轮登记的链接（官网、X、GitHub、文档），
加上 X 搜索新挖出来、并由 `search/identify.py` 判成「这是项目自己的」的那些（见下一节）。
两拨链接合起来用，不是二选一：源登记了搜索找不到的币，搜索找到源从没登记的账号，
而两类候选都靠同一道「读页面」的门才算数。

每个标的 **最多 `MAX_PAGES = 8` 个外链**，**页面正文合计 `MAX_CHARS = 15000` 字符**；
官方账号的推文另走一份 **`POST_CHARS = 15000`** 的额度，条数上限是 `xsearch.TWEET_LIMIT = 20`（约两周的量，只取项目自己的原创帖，转推不计）。
页 + 帖合计 30000 字符，是一轮能放到模型面前的上限。低于 `MIN_CHARS = 80` 字符的页面
（登录墙、空壳页、抓不到正文）当作「没有证据」，绝不拿模型的记忆补上。读到的页面写进
`lexicon_sources`，卡片能点回原文。

**它在轮内，不在发布之后。** 链下调研是打分里 45% 的输入（另 55% 是链上质量），
所以顺序是 `gate → draft（调研 + 写作）→ score`：卡片上的话和卡片旁的分必须来自同一份证据，
这个慢计入本轮耗时（`draft` 阶段）。一个标的的模型调用被校验拒了，会带着它自己的 JSON
和校验器原话**再问一次**（`research.REPAIR_HEAD`，落库 `prompt_version = research.md@7+repair`）；
再失败才当作缺席 —— 那个币不进排名，本轮 note 里写明。

## 挖链：X 高级搜索（`app/search/`，登录态在 `config/search.env`）

数据源给的社媒是 **handle 与裸域名**，新币更常常一个都没有 —— 但 X 上多半已经有人发过它的
合约地址。这一层做的就是「CA 进、项目自己的链接出」；它在 `identify` 之前，交出来的一律是
**候选**，不是事实。

- **引擎**（`search/engines/xsearch.py`）：走 X 站点自己那套 advanced search，由外部 `CH_X_CLI`
  （默认 `twitter`，一个在 PATH 上的 CLI）带着登录态跑。**一次搜索是一个 subprocess，不吃模型
  token** —— 上一版拿搜索模型问同一片地，一次调用约 15 万输入 token，现在是 0，所以「每个候选
  都问一遍」才付得起。
- **两个问题**：合约地址（转述不了的那串）找写下它的人；ticker 找那个从没写过地址的账号。
  实测里 ticker 是唯一能把某些官方账号找出来的那条。
- **登录态**：`config/search.env` 的 `CH_X_AUTH_TOKEN`（session cookie）与 `CH_X_CT0`
  （CSRF token，随 bearer 一起发 `X-Csrf-Token`）。请用**小号**：搜索是被限流计数的读，429 要让
  这个号歇一会。这份文件在启动时读一次（`llm.env` 是每次模型调用重读，两者不同），换了 cookie
  要重启。
- **找 ≠ 信**（`search/identify.py`）：搜索只给候选与阅读顺序（`xsearch.affinity` 把 handle / name
  像 ticker 的排前面；`PROFILE_HITS = 12` 个账号交给 `read_profiles` 读 8 个，`check_sites` 另读
  6 个站）。谁是真的由「读页面」决定，两条路任一成立即可：页面自己的描述里带 CA（X 上就是 bio）；
  或「站点描述写了这个币」＋「账号链回这个站」两头对上。
- **互证**（`search/harvest.py`）：一个已经判成项目自己的页面，是它所有频道最好的索引 —— 页脚写
  账号、bio 写频道。这里取到的链接同样只当候选，照样要过读页面那道门；**网站不在这里取**（卡片
  只印一个站，印的是被判过的那个，不是某个页面顺带提到的）。
- **宽度**：挖链这一半按 `SEARCH_WORKERS = 15` 并发（subprocess + 几页读，不吃 token）；写词条
  那一半是模型调用，按 `CH_RESEARCH_WORKERS`（默认 12）并发。15 实测干净，20 会在 20 个标的里
  的 3 个上撞 429。

## 社媒与官网：逐币读 DexScreener

三个登记源（RadarDEX / Tolly / GMGN）都带社媒列，但给的是 **handle 与裸域名**（`Solonlabs1`、
`solonpad.fun`），而且只有 X 与 TG —— 官网、文档、GitHub 常常只挂在项目自己的页面上。链接因此由
`sources/dexscreener.py` 逐币补：`pairs[].info.socials[]` / `info.websites[]` 已经是完整 URL，
与上面三源在 `collector.merge` 里取并集（`socials` / `websites` 是并集，不是赢家通吃）。

四条口径：

- **handle 与裸域名先归一。** `sources/base.py` 的 `social_url` / `web_url` 一处实现，
  RadarDEX / Tolly / GMGN 三处共用：`Solonlabs1` → `https://x.com/Solonlabs1`，
  `solonpad.fun` → `https://solonpad.fun`，已经是 URL 的原样留着。
- **不参与 pull。** 它没有全链列表端点，`fetch_universe` 返回空，挂在 `registry.ENRICH_ONLY`，
  由 `Engine._enrich` 的**逐币那一遍**问 —— 凭据行里没有它，扫描耗时也不受影响（≤20 个被判定标的
  各一次请求，与其它源并发）。`decide.Engine` 只有在调用方没自带 sources 时才带上它，
  所以测试永远是离线的。
- **只填链接。** `socials` / `websites` / `description`（`description` 目前只有它有）。它的市值 /
  流动性 / 成交不进任何字段：数字多一个意见，规则没改而分数会动。
- **取值只认本 CA 的池。** 优先 `baseToken.address == CA` 的 pair；一条都没有时，只有整个响应里
  只出现一条 `chainId` 才接受最好的一条，否则返回空 —— 另一条链的 X 句柄写到这张卡上比空着更糟。
- 无磁盘缓存：每轮每个被判定标的问一次（它答的是「这个 CA 现在挂着哪些链接」，本来就会变）。

## 读页面：静态优先，空壳交浏览器（`app/search/identify.py` 的 `fetch_page`）

两处读页面走同一道门：①判「这是不是项目官网」（`app/search/discover.py`）；②写卡片正文
（`report/research.py` 的 `_read`，每卡最多 `MAX_PAGES = 8` 个外链、正文合计 `MAX_CHARS = 15000`；官方账号的推文另走 `POST_CHARS = 15000`）。

`fetch_page` 先走 `sources/http.py` 的静态两条 lane（httpx → curl_cffi 伪装）；正文不足
`SHELL_WORDS = 40` 个词（或两条 lane 都被拒）时，把同一个 URL 交给 `app/sources/browser.py` 的
Chromium 重新抓，**两份取词多的那份**。

- 阈值来自实测：2026-09-27 三个空壳站是 8、8、**10** 个词，同一批里次低的官网有 **94** 个词。
- 不渲染的代价是错的：`usearckit.online` 曾判 none，卡片上挂成了第三方站 `myarcade.fun`。
- **X 永远不渲染**（`identify.NO_RENDER`）：X 只把 bio 给静态客户端，未登录的浏览器看到的是登录墙 ——
  画一次五秒，拿回来的比原来还少。TG 的 `t.me/<name>` 预览页是另一个方向：内容本来就在页里，
  画它实测 27 个词换 27 个词，白花 7.5 秒，所以也在名单里。
- 渲染开销 2.4–12.0 s/页，只在空壳时才发生；一个进程一个 Chromium（标签页上限 6），自己一个线程跑
  一个 event loop —— playwright 的同步 API 跨线程会崩。退出时 `main.py` 的 lifespan 关掉它。
- 等待策略：`DOMContentLoaded` 后再等 2.5 s。`networkidle` 在 memespad.com 上是 21.8 s 且内容一模一样；
  拦图片与样式表反而更慢（16 页 5.0 s → 8.1 s）并让部分页面渲染不全。
- **`fetch_page` 只读交给它的 URL，自己不猜地址。** 负责「找」的是上面那一层搜索，它交上来的每一条都是候选，要过这道读页面的门才算数 —— 一条猜错的链接比没有链接更糟。
- 渲染之后仍不足 `_read` 的 `MIN_CHARS = 80` 字符就不进 prompt：交上去只会诱导模型拿记忆补空白。

## 缓存与限频

源本身没有常量限频头，用磁盘缓存 `data/raw/cache.json` 自己兜：

| 缓存键前缀 | TTL | 说明 |
| --- | --- | --- |
| `gt:` | `POOLS_TTL` 12h、`FULL_SWEEP_TTL` 6h、`OHLCV_TTL` 6h | GeckoTerminal |
| `rd:tokens:` | 900s | RadarDEX 全量 |
| `gmgn:rank:` | 240s | GMGN 排行 |

改了任何源的字段解析后，要清掉对应前缀的缓存，否则会一直吃到旧结构的响应。
