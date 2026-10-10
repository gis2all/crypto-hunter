# 数据源（Solana）

状态：**已探测**（2026-10-11）。这条链上跑得动的是通用源 ＋ 一条链上直读；EVM 那套（goplus / sourcify / blockscout / onchain 的 LP 与 bytecode 读法）在这条链上不适用。

## 结论表

| 源 | 端点 | 用途 | 传输 | 实测 | 需 key |
| --- | --- | --- | --- | --- | --- |
| GMGN | `gmgn.ai/defi/quotation/v1/rank/sol/swaps/{period}` | 换手榜（扫描源） | curl_cffi | 200 | 否 |
| GMGN | `gmgn.ai/api/v1/{token_stat,token_dev_info,token_info}/sol/{ca}` | 持有人、Top10、捆绑、狙击、dev | curl_cffi | 200 | 否 |
| GMGN | `gmgn.ai/api/v1/token_security_sol/sol/{ca}` | Solana 安全表（**只有正面算数**，见 CLAUDE.md 6.2.4） | curl_cffi | 200 | 否 |
| GeckoTerminal | `api.geckoterminal.com/api/v2/networks/solana/{pools,new_pools,trending_pools}` | 池枚举、K 线、池面回撤 | httpx | 200 | 否 |
| DexScreener 榜单 | `dexscreener.com/solana?rankBy={order}` | **全链枚举**（这条链唯一的那个） | 浏览器通道读 SSR HTML | 200；6 个排序并集 380 对 | 否 |
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

这条链没有发射台专用源，所以「榜单」是唯一不靠发射台就能枚举全链的口子。

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
