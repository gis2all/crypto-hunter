# 数据源（Solana）

状态：**已探测**（2026-10-11）。这条链上跑得动的是通用源 ＋ 一条链上直读 ＋ 两个发射台（`launchlab` / `pumpfun`，见第 3 节）；EVM 那套（goplus / sourcify / blockscout / onchain 的 LP 与 bytecode 读法）在这条链上不适用。

## 结论表

| 源 | 端点 | 用途 | 传输 | 实测 | 需 key |
| --- | --- | --- | --- | --- | --- |
| GMGN | `gmgn.ai/defi/quotation/v1/rank/sol/swaps/{period}` | 换手榜（扫描源） | curl_cffi | 200 | 否 |
| GMGN | `gmgn.ai/api/v1/{token_stat,token_dev_info,token_info}/sol/{ca}` | 持有人、Top10、捆绑、狙击、dev | curl_cffi | 200 | 否 |
| GMGN | `gmgn.ai/api/v1/token_security_sol/sol/{ca}` | Solana 安全表（**只有正面算数**，见 CLAUDE.md 6.2.4） | curl_cffi | 200 | 否 |
| LaunchLab | `launch-mint-v1.raydium.io/get/list?sort=hotToken&size=100`（信封带 `nextPageId`） | 发射台清单：Raydium LaunchLab（bonk.fun 是它的一个 `platformId`） | httpx | 200，100 条/页 | 否 |
| pump.fun | `frontend-api-v3.pump.fun/coins?limit=70&sort=last_trade_timestamp&offset=N` | 发射台清单（**裸 JSON 数组**，offset 分页） | httpx | 200，70 条/页 | 否 |
| GeckoTerminal | `api.geckoterminal.com/api/v2/networks/solana/{pools,new_pools,trending_pools}` | 池枚举、K 线、池面回撤 | httpx | 200 | 否 |
| DexScreener 榜单 | `dexscreener.com/solana?rankBy={order}` | **全链枚举**（不靠发射台的那一个） | 浏览器通道读 SSR HTML | 200；6 个排序并集 380 对 | 否 |
| DexScreener 逐币 | `api.dexscreener.com/latest/dex/tokens/{ca}` | 官网 / 社媒 / 图、市场口径校准 | httpx | 200 | 否 |
| Solana RPC | 链 yaml 的 `rpc`（`api.mainnet-beta.solana.com`） | mint 账户：铸造权 / 冻结权 | JSON-RPC | 200 | 否 |

不适用（不是漏配）：`goplus` 与 `sourcify` 的 chain id 表是 EVM 的；`blockscout` 要一个 Blockscout 实例；`sources/onchain.py` 的余额 / bytecode / LP 读法整块是 EVM 的。

## 1. mint 账户：铸造权与冻结权（`sources/solana/mint.py`）

这两个权限还在，就是项目方随时能增发或冻结持有人的币 —— 这是 Solana 上「危险权限」的唯一来源，EVM 那套 GoPlus 标志在这里没有。

**答案不来自 GMGN。** `token_security_sol` 里有 `renounced_mint` / `renounced_freeze_account`，实测不可信：USDC（铸造权仍在 Circle 手里 `BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG`）回 `renounced_mint: true`；上一轮 Solana 池 14 个币全部 `true`；权限**确实**已放弃的 PUMP 回 `null`。「全 true」和「全 null」都不能当判据，所以改读链上 mint 账户。

mint 账户布局（实测）：

| offset | 字段 | 读法 |
| --- | --- | --- |
| 0 | `COption<Pubkey> mint_authority` | 4B tag（LE）＋ 32B key |
| 46 | `COption<Pubkey> freeze_authority` | 同上 |
| 82 | supply / decimals 之后 | `MINT_BYTES = 82`：短于这个长度的账户不是 mint，记未判定 |

`tag == 0` → 已放弃；`tag != 0` → 仍持有。

判定语义（同 6.2.5 / 6.2.8 那条规则）：

| 读到的 | 含义 | 后果 |
| --- | --- | --- |
| `[]` | 读完了，两个都放弃 | 通过 |
| `None` | 节点没答 / 不是 token program 的账户 / 字节太短 | **未判定，永不淘汰** |

实测的 RPC 坑：

- `getAccountInfo` ＋ base64：单次冷启 0.7–2.4s；15 个并发在 2.4s 内全回来。
- **`getMultipleAccounts` 传 ≥3 个「不同」key 的裸数组 → `Invalid param: WrongSize`**（重复 key 反而正常，100 个 key 的批量也报错，行为不稳定）→ 放弃批量，逐币问；补齐那一遍本来就在跨币并发，够用。
- 只读 tag，不解 base58：没有地方要打印「谁持有」。

落在哪里：`registry.ALL` 里有它（不在 `ENRICH_ONLY`）；`config/chains/solana.yaml` 的 `sources:` 里挂着，但**不在 `scan_sources`** —— 它没有全链列表，扫不出行来。设定页那行「无危险权限」读的是 `dangerous_perms`，与 EVM 是同一条规则、同一个字段。

## 2. DexScreener 榜单（`sources/dexscreener_rank.py`）

这条链的发射台源（第 3 节）只枚举自己平台上的币；不靠发射台、能枚举全链的口子只有一个，就是「榜单」。

- `dexscreener.com/solana?rankBy={order}` 的 SSR HTML 里带整块榜单（protobuf-JSON：`BigInt("33")` / `void 0` / `undefined`）；`api.dexscreener.com` 没有列表端点，`io.dexscreener.com` 403/404，无头浏览器被 Cloudflare 拒 —— 只能读文档。
- 一页固定 100 条，`&page=` 被忽略 → 换排序取并集：6 个排序拿到 380 对（实测 2026-10-11）。
- 市值与流动性是**服务端过滤**，所以 `collect.set_scan_band` 把本轮 `pipeline.prefilter` 的区间交给它，拿回来的 100 行就是本轮可能留下的行。
- 实测（10k–200k 市值 / 流动性 ≥ 5000）：**350 行**，带 symbol 350 / 带图 239 / 带官网 190 / 带社媒 232，用时 15.2s。

并入效果（2026-10-11 实测，同一套阈值）：

| 口径 | 合并后的池 | 第一半硬过滤通过 |
| --- | --- | --- |
| 只有 gmgn ＋ geckoterminal | 401 | 96 |
| 加上榜单 | 692 | 391 |

代价是有的：过关池从 96 涨到 391，全部进逐币补齐与安全判定（`gate_limit: 0` 就是全池）。

## 3. 发射台（`launchlab` / `pumpfun`）

两个源回答的是同一件事：**刚发出来、还没有池的币在哪**。榜单只列已经有池、并且已经排到前面的币，这两个列的是刚发出来的那批。

### 3.1 Raydium LaunchLab（`sources/solana/launchlab.py`）

- `launch-mint-v1.raydium.io/get/list` 是 LaunchLab 唯一的公开清单端点；`api-v3.raydium.io` 的 Swagger 里没有它。免鉴权，请求带上站点自己的 `Referer`。
- **bonk.fun 不是一个独立源**，是这套程序的一个 `platformId`。不传 `platformId` 时返回所有平台 —— 探针第一页第一行就是 `letsbonk.fun` —— 所以一次扫描覆盖 bonk.fun、Raydium 自家和其它平台，卡片上的发射台名取行里的 `platformInfo.name`。
- `sort` 只认 `hotToken` / `new` / `marketCap`（其它值 500 `sort type error`）；`size` 上限 100，下一页从信封的 `nextPageId` 取（不是 offset）。
- **`volumeU` 不读**：它是曲线段的累计成交，币迁移后停更。实测同币在 DexScreener 上 24h 是 62,759 / 21,138 / 9,331，而这里读出来是 0。`marketCap` **是美元**（实测与 DexScreener 逐美元相等），`createAt` 是毫秒。
- 行里没有发布币自己的历史高点和日涨跌，所以这两个字段留给别的源。

### 3.2 pump.fun（`sources/solana/pumpfun.py`）

- `frontend-api-v3.pump.fun/coins` 返回**裸 JSON 数组**，offset 分页；旧域名 `frontend-api.pump.fun` 已废（530），逐币端点 404 —— 清单就是这个源的全部。要 100 行也只回 70 行。
- **两个市值列，只有一个是以美元计的**：`market_cap` 以 SOL 计，`usd_market_cap` 才是美元。实测同一行 98.40 对 10,831.74（比值 110 ＝ SOL 价格），读错列会把币压到真实位置的两个数量级之下。
- `ath_market_cap` **也是美元**（实测新币的 `ath_market_cap` / `usd_market_cap` 比值 1.0–2.13；若按 SOL 计价这个比值会落在 108 附近）。距高回撤读的就是它。
- **没有成交量列**，也不借别的源的成交量：24h 成交与成交量因子记未判定（未判定永不淘汰）。曲线上的币还没有池，流动性同理。

并入效果（2026-10-11 实测，阈值 10k–200k 市值 / 流动性 ≥ 5000）：LaunchLab 落带内 32 个、净新 30 个；pump.fun 落带内 39 个、净新 20 个。边际成本按每个净新候选约 5 秒（`ENRICH_WORKERS = 8`）算，约 ＋4–5 分钟。