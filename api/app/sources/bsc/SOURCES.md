# 数据源（BSC）

状态：**已探测**（2026-10-11）。这条链是 EVM，链 id 56，所以 GoPlus / Sourcify / `sources/onchain.py` 的读法都能用。它的特点是**一个发射台占据大半新币**：四种发射台里只有 four.meme 值得接入，其余活跃池子靠通用源。

## 结论表

| 源 | 端点 | 用途 | 传输 | 实测 | 需 key |
| --- | --- | --- | --- | --- | --- |
| four.meme | `four.meme/meme-api/v1/public/token/search`（**POST**） | 本链第一大发射台，**只扫已毕业** | httpx POST + 站点自带 key | 200，50 条/页 | 否 |
| GMGN | `gmgn.ai/defi/quotation/v1/rank/bsc/swaps/{period}`、`gmgn.ai/api/v1/token_security_evm/bsc/{ca}` | 换手榜、持有人 / Top10 / 捆绑、税 | curl_cffi | 200（httpx 直连 403，与其它链相同） | 否 |
| GeckoTerminal | `api.geckoterminal.com/api/v2/networks/bsc/{pools,new_pools,trending_pools}` | 池枚举、K 线、池面回撤 | httpx | 200，20 条/页 | 否 |
| DexScreener 榜单 | `dexscreener.com/bsc?rankBy={order}` | 服务端接收 `minMarketCap` / `maxMarketCap` / `minLiq` | 浏览器通道读 SSR HTML | 200，2.1 MB | 否 |
| DexScreener 逐币 | `api.dexscreener.com/latest/dex/tokens/{ca}` | 官网 / 社媒 / 图、市场口径校准（ENRICH_ONLY） | httpx | 200 | 否 |
| GoPlus | `api.gopluslabs.io/api/v1/token_security/56?contract_addresses={ca}` | 貔貅、危险权限、`dex[]` / `holders[]` / `lp_holders[]` | httpx | 200 `code: 1` | 否 |
| Sourcify | `sourcify.dev/server/v2/contract/56/{ca}` | 源码已验证（双源之一） | httpx | 200 `exact_match` | 否 |
| BSC RPC | 链 yaml 的 `rpc`（`bsc-dataseed.binance.org`） | LP 位置、dev 余额 | JSON-RPC | 可达 | 否 |

链 id 都写在 `goplus.CHAIN_IDS` / `sourcify.CHAIN_IDS`（`"bsc": 56`），GMGN 的 slug 就是 `bsc`（不需要 `CHAIN_SLUGS` 那一层）。

## four.meme（`sources/bsc/fourmeme.py`）

`four.meme/meme-api/v1/public/token/search` 是站点自身的端点：**POST + JSON**，带其 web 客户端使用的 `meme-api-key` 头（站点自己的 bundle 里明文放着 `axios.create({baseURL: "/meme-api", headers: {"meme-api-key": ...}})`）。实测不需要 cookie、不需要签名，普通客户端即可通过，不需要浏览器通道。

| 事项 | 实测（2026-10-11） | 后果 |
| --- | --- | --- |
| 只扫**已毕业**（`status: TRADE` + `listType: NOR_DEX`） | 曲线行（`status: PUBLISH` + `listType: NOR`）市值 4–593 U，**没有任何池子** | 没有池的币流动性读成「未判定」会被放行，还会占用调研名额；它也不能与已毕业的币放在一起排序（与 PONS 同一条理由） |
| 两个排序并集：`CAP` + `NEW` | `CAP` 第 1 页 892,076 起、第 2 页 274,547–872,237（65 个落在 200K–2M），**第 3 页返回空**；`NEW` 6 页共 202 个去重 CA，其中 193 个 ≤200K、中位 3,138 | 两个排序各覆盖一半：上限那一半只有 `CAP` 有，刚毕业那一批只有 `NEW` 有 |
| `VOL` 排序**不扫** | 它的行就是 DexScreener 榜单已经在排的那些 | 每多一个排序就多一百个待补齐的行，不划算 |
| `pageIndex` **不可信** | 第 3 页逐行重复第 1 页（首个 CA、`createDate` 都一样）；`NEW` 5 页 250 行只有 153 个去重 CA | 按 CA 去重之后全部接收，页数是上限而不是保证 |
| `minCap` / `maxCap` **服务端不识别** | 带上与不带上返回结果相同（`ranking` 带上时甚至返回 0 行） | 因此这个适配器**不声明** `market_cap_floor` 那几个旋钮：它无法应用这一区间 |
| 逐币查询按 `keyword` | `{"keyword": "0x…"}` 返回 1 行，正好是这个 CA；**`0X…` 返回 0 行**（`0x` 必须小写，后面 40 位大小写不敏感） | 查询时使用经 `ca_key` 归一的小写地址 |

### 读什么，不读什么

行里带有 `cap`、`volume` 和一整排窗口成交量，**一律不读**：

| 列 | 实测 | 不读的原因 |
| --- | --- | --- |
| `cap` | AKE 这里 2,703,401,221，DexScreener 616,653,496（×4.4）；旁边一行两边一致到五位（560,303,067 / 560,082,849） | 两个口径共用一个字段名。`market_cap_usd` 不从这里写，而该源**不在** `collect.FIELD_PRIORITY` 那一栏里 |
| `day1Vol` | AKE 595,445.97 对真值 621,626.82；`$BANANA` 360.33 对 1,652,020.09；Tag 55,976,851.46 对 208,838.20 | 通常近似当日成交，有时不是；同一行的 `volume` 又与它互相矛盾（实测 `volume` 859.69 陪 `day1Vol` 599,093.06） |
| `hold` | 20/20 行是 0，含七位市值的行 | 不是持有人数 |
| `symbol` | 每行都是计价币（`"BNB"`） | 代号在 `shortName` |

留下的是只有这个源才有的字段：**代号、部署者、创建时间、图，以及「该币来自 four.meme」**。`createDate` 是 unix 毫秒。

图：`img` 是站点自己的资源路径（`/market/<id>.png`），主机由站点自己在 `/v1/public/file/host` 公布 —— `https://static.four.meme`（实测 200 `image/png`）。读不出这两个形状的值表示没有图，不会丢行。

## 这条链与其它链不同的地方

- **没有 Blockscout**：BSC 的浏览器是 bscscan，不是 Blockscout 实例，所以 `blockscout` 不进这条链的 `sources:`（它是 Arc / Robinhood 的全链底表）。全链枚举由 `geckoterminal` + `dexscreener_rank` + `fourmeme` 三份清单取并集。
- **市值下限出厂是 0**：这条链的 `prefilter.market_cap_min_usd` 尚未按自身分布校准，而 four.meme 刚毕业的那一批中位市值只有 3,138 U；真正把它们挡在外面的是**流动性下限 8,000**（没有池或池子薄）与「空壳淘汰」。需要收紧时只改设定页那几个数，不改适配器。
