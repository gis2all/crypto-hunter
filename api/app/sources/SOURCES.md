# 数据源（Arc）

状态：**已探测**（2026-09-24 10:04 +08）。

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
10. **Arc 节点的单次 `eth_call` ≈ 2.2–2.5s，这是读路径的硬地板。** 实测：单次请求 2.2–2.5s，与查什么无关（`eth_getCode` 与 `eth_call` 同量级）；并发几乎免费（1/4/8/12 并发分别 2.16/2.37/2.88/3.56s），所以「多问几件事」的正确姿势是并发或打包批量，不是省着问。批量确实省：同样 2 次请求，单发 4.61s、打包成一个 JSON-RPC 批量 3.54s。`eth_getLogs` 的窗口按 `LOG_WINDOW = 20000` 切、4 个窗口并发（`LOG_WINDOW_WORKERS`）；窗口被节点拒绝对半递归，所以**同一地址的 getLogs 调用次数轮间不可比**（见 `CLAUDE.md` 的 5.1 读路径）。

## 分级策略（已实现）

粗筛只吃池级数据：一次分页拉全量，套市值 / 流动性 / 成交三条硬过滤，把三千多个币砍到一两百个候选；再对候选逐币查持仓与安全。**不对全量逐币发请求。**

## 重档调研读的「源」是项目自己的页面

`report/research.py` 不接新的数据台：它读的是本轮已经知道的链接（官网、X、GitHub、文档），
每个标的最多 4 个、正文合计 8000 字符上限、每个 URL 只试一次（`UA_HTML` + `transport=http`）。
登录墙、空壳页、抓不到正文一律当作「没有证据」，并在 `cons` 里直说 —— 绝不拿模型的记忆补上。
读到的页面写进 `lexicon_sources`，卡片与词条库都能点回原文。它是发布之后的异步动作，
所以这里的慢不进入任何一轮的耗时。

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

## 读页面：静态优先，空壳再渲染（`report/research.py`）

重档调研按 `links_for` 逐页读（官网 → X → TG → GitHub → 文档，最多 `MAX_PAGES = 4`）。默认走
`sources/http.py` 的静态 lane；正文不足 `MIN_CHARS = 80` 时，**同一个 URL** 再交给
`READER_PREFIX = https://r.jina.ai/` 渲染一次，两份取更长的那个。

- **为什么需要第二遍**：Arc 上的发射台前端多是 React/Vite 空壳，静态 HTML 只有 title 与一个 div。
  在打开渲染之前，这类站点读回来是 `None`，而失败的页面不落库 —— 卡片上看起来就是「这个项目没有官网」。
- **请求头就是判据**：jina 对浏览器 UA（`UA_HTML`）返回 403，对 `curl/8.5.0`（`UA_READER`）返回正文。
  两条 lane（httpx / curl_cffi）都试过才定位到，因为它不是 TLS 指纹问题（对比下表 GMGN 那条）。
- **不是搜索引擎**：只读这一轮已经学到的链接，不去猜 URL —— 猜错的一条链接比没有链接更糟。
- **X 用静态抓**：`x.com/<handle>` 的静态 HTML 已含简介、推文数、粉丝数与注册地，不必渲染；
  TG 走 `t.me/<name>` 的公开预览页。
## 缓存与限频

源本身没有常量限频头，用磁盘缓存 `data/raw/cache.json` 自己兜：

| 缓存键前缀 | TTL | 说明 |
| --- | --- | --- |
| `gt:` | `POOLS_TTL` 12h、`FULL_SWEEP_TTL` 6h、`OHLCV_TTL` 6h | GeckoTerminal |
| `rd:tokens:` | 900s | RadarDEX 全量 |
| `gmgn:rank:` | 240s | GMGN 排行 |

改了任何源的字段解析后，要清掉对应前缀的缓存，否则会一直吃到旧结构的响应。
