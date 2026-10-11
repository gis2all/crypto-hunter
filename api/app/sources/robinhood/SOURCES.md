# 数据源（Robinhood）

状态：**已探测**（2026-09-30 首轮；第四个发射台 PONS 于 2026-10-11 接入）。这条链是 EVM，所以 GoPlus / Sourcify / Blockscout / `sources/onchain.py` 的读法都能用；它的特点是**发射台多** —— 四个平台各给一份整链清单。

## 结论表

| 源 | 端点 | 用途 | 传输 | 实测 | 需 key |
| --- | --- | --- | --- | --- | --- |
| CoinBarrel | `coinbarrel.com/market/api/v1/...` | 本链自己的索引，最全的一个（293 个币，分页 200 一封顶） | httpx | 200 | 否 |
| PEZ | `api.pez.family/api/...` | 发射台清单 | httpx | 200，100 条/页 | 否 |
| Peeps | `peeps.wtf/api/launchpad/tokens?tab=trending&pageSize=100` | 发射台清单，`pageSize` 不被夹 | httpx | 200，84 个币一页 | 否 |
| PONS | `ponsfamily.com/api/launches?sort={active,newest}&stage=graduated` | 这条链最大的发射台，**只扫 graduated** | httpx | 200，40 条/页，cursor 不透明 | 否 |
| GMGN | `gmgn.ai/defi/quotation/v1/rank/robinhood/swaps/{period}`、`gmgn.ai/api/v1/token_security_evm/robinhood/{ca}` | 换手榜、持有人 / Top10 / 捆绑、**税** | curl_cffi | 200 | 否 |
| GeckoTerminal | `api.geckoterminal.com/api/v2/networks/robinhood/{pools,new_pools,trending_pools}` | 池枚举、K 线、池面回撤 | httpx | 200 | 否 |
| DexScreener 榜单 | `dexscreener.com/robinhood?rankBy={order}` | **不靠发射台的全链枚举口**：发射台只列自己的币，普通 Uniswap 池只有它枚举得出来 | 浏览器通道读 SSR HTML | 200，10k–200k 市值 355 行 | 否 |
| DexScreener 逐币 | `api.dexscreener.com/latest/dex/tokens/{ca}` | 官网 / 社媒 / 图、市场口径校准 | httpx | 200 | 否 |
| GoPlus | `api.gopluslabs.io/api/v1/token_security/4663?contract_addresses={ca}` | 貔貅、危险权限、`dex[]` / `holders[]` / `lp_holders[]` | httpx | 200 | 否 |
| Sourcify | `sourcify.dev/server/v2/contract/4663/{ca}` | 源码已验证（双源之一） | httpx | 200 命中 / 404 未收录 | 否 |
| Robinhood RPC | 链 yaml 的 `rpc`（`rpc.mainnet.chain.robinhood.com`） | LP 位置、dev 余额 | JSON-RPC | 可达 | 否 |

## 1. 四个发射台

四个源回答的是同一件事：**这条链上刚发出来的币在哪**，再加上榜单没收录的长尾池。每个适配器只认自己那份清单，彼此不感知。

落在哪里：四个都在 `registry.ALL`，都在 `config/chains/robinhood.yaml` 的 `sources:` 与 `scan_sources:` 里。`collect.FIELD_PRIORITY` 里它们排在 GMGN / GeckoTerminal **之后** —— 自报的数字不能盖过市场口径。

## 2. PONS（`sources/robinhood/pons.py`）

`ponsfamily.com/api/launches` 是发射台自己的端点，与它渲染 `/launchpad` 的页面同源、免鉴权。`api.ponsfamily.com` 是**另一个要 key 的服务（401）**，不是这个。四处必须按实测来读：

| 事项 | 实测 | 后果 |
| --- | --- | --- |
| 只扫 `stage=graduated` | `stage=all&sort=active` 是 319 个曲线行比 1 个毕业行；其中 34 个曲线行落在本轮市值带内，却**一点流动性都没有** | 曲线上的币还没有池，流动性读成「未判定」会被放行 —— 没池的币不能和已毕业的币放在一个池子里排 |
| `sort` 取 `active` ＋ `newest` | `sort=marketCap` 首页起点 $922K，高出上限一个数量级 | 它对带内没有贡献，所以不进并集 |
| `volumeUsd` 只读毕业行 | 三个毕业行这里是 0，而它们当天的池 24h 成交是 61,856 / 62,759 / 21,138（DexScreener） | 这一列是**曲线段自己的**成交量，迁移后停更；一个有两种含义的数不能被一条下限读 |
| `artwork` 走站点自己的网关 | `ipfs://<cid>` → `https://dbk-vercel.vercel.app/api/ipfs/content/<cid>?variant=card`（实测 200 `image/webp`；ipfs.io 403、dweb.link 429） | 公共网关拒我们这个客户端；网关挂了只丢一个图标，不丢行 |

其它：`createdAt` 是 unix 秒（不是毫秒）；市值读 `marketCapUsd`；creator 取 `deployer`；`socials` 里的 `website` 进 `websites`。分页 cursor 是**不透明字符串**（猜 `cursor=0` 回 503），`MAX_PAGES = 8`，两个排序各 8 页。

## 3. 另外三个发射台

- **CoinBarrel**（`sources/robinhood/coinbarrel.py`）：本链自己的索引，也是最全的一个。三个列必须先读再用 —— `market_cap_usd` 全空（实测 0/50），市值在 `fdv_usd`；`liquidity_primary_usd` / `volume_usd_24h` 只有部分有值，缺的时候用「原生池数量 × 本行的 `quote_usd_rate`」换算；`price_change_24h` 是**带符号**的百分数（-28.72 / +1.52），用只认正数的读法会把每次下跌读成空。`peak_fdv_usd` 是它自己的历史高点，距高回撤读它。
- **PEZ**（`sources/robinhood/pez.py`）：成交量以**池子的报价资产**计价，这条链上那不是稳定币（有一行是一只代币化股票），所以要乘本行的 `priceQuote` 换成美元；`priceChangePct` 是小数不是百分数，要乘 100。它是这条链上**唯一**回答社媒的源。
- **Peeps**（`sources/robinhood/peeps.py`）：USD 列都是原生的（`marketCap` / `liquidityUsd` / `volumeUsd24h`），`athMarketCap` 是币自己的历史高点（与 GMGN 的 `history_highest_market_cap` 同一类测量，不是池面价格历史）。`tab=new` 回空列表而 `tab=trending` 回全部 84 行，所以两个都用 `trending`。这一份清单**没有任何社媒和外链**。