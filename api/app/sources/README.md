# 数据源与适配器

跨链适配器放本目录根，只服务一条链的放 `<chain>/`；都从 `registry.ALL` 取，共用 `http`（缓存与节流）。逐源的端点、传输方式与实测结论写在各自的 `SOURCES.md`。

## 数据源、安全判定与富集

**分工表**（逐源端点、传输方式与实测结论见该链的 `api/app/sources/<chain>/SOURCES.md`，改源之前先读那份）：

| 用途 | 源 |
| --- | --- |
| 全链扫描（`scan_sources`） | Arc：gmgn / radardex / tolly / geckoterminal；Robinhood：gmgn / geckoterminal / coinbarrel / pez / peeps / pons / dexscreener_rank；Solana：gmgn / geckoterminal / dexscreener_rank / launchlab / pumpfun；BSC：gmgn / fourmeme / geckoterminal / dexscreener_rank |
| 安全与源码 | goplus（貔貅、税、危险权限、`dex[]` / `holders[]` / `lp_holders[]`）/ sourcify（源码验证） |
| 补链接 ＋ 校准行情（`registry.ENRICH_ONLY`） | dexscreener：填 `symbol` / `socials` / `websites` / `description`，**并在补齐那一步优先写入市值 / 流动性 / 24h 成交 / 价格 / `price_change_24h_pct`**；同一个站点的批量那一半是 `dexscreener_rank`（榜单），它**进 `sources:` 当扫描源**，不是 ENRICH_ONLY |
| 挖链找页面（在 `app/search/`，不在 `sources/`） | X 高级搜索（cookie 子进程，不消耗 token）＋ `identify` 读页面互证；只交出候选链接，tier 由 `identify` 判定 |
| 链上直读 | `sources/onchain.py` 走链自己的 RPC（LP 位置、dev 余额）；Solana 另有 `sources/solana/mint.py` 读 mint 账户的铸造权 / 冻结权（即「危险权限」在这条链上的全部来源，见 `sources/solana/SOURCES.md`） |

**新增源时的四个易错点**（后果都是静默的，编译器不会报错）：

1. **`FIELD_PRIORITY` 决定一个字段能不能输出**。合并逐字段走那张表，表里没列的源对该字段**无法胜出**，哪怕它答了 —— 行会以「未判定」通过硬过滤、以「空壳」被淘汰。新源要么进已有字段的表项，要么落进 `DEFAULT_PRIORITY`（链专属源排最后）。
2. **只服务一条链的源不应留在根目录**。RadarDEX / Tolly 在 Robinhood 上返回的是 Arc 的整份数据（`?chain=robinhood` 的 CA 集合与默认调用逐字节相同），放进 `robinhood.yaml` 会让另一条链的代币进入候选池。
3. **链自己的 id 未必是源要的 slug**。`geckoterminal` / `dexscreener` 把 `chain_id` 直接当路径段或 `chainId` 用，所以 `config/chains/<id>.yaml` 的 `id` 要用那条链在外部站点上的名字（Solana 就叫 `solana`）；`gmgn` 是例外 —— 它自己的榜单把 Solana 拼成 `sol`，所以有 `gmgn.CHAIN_SLUGS` 这张表；`goplus` / `sourcify` 则要另给 EVM chain id。**拼错的代价是静默的**：GMGN 对不认识的链名回 200 ＋ 空榜，报表上就是「这条链没有币」。
4. **yaml 里加源不等于生效**。一轮的拉取清单读 yaml，但设定页的「扫描源」行与凭证行走**存档版本**（`settings_versions`，四个 section 含 `sources`）：一套设定保存过之后，新加的源**会被拉、页面上却不显示**。新增源后须在设定页保存一次（等价 `PUT /api/v1/chains/<chain>/settings?config_id=<id>`）；从未保存过的链（如 Solana）以 yaml 为准。

| 规则 | 说明 / 反例 |
| --- | --- |
| 扫描源与富集源分开：`registry.ENRICH_ONLY` 的源（当前只有 dexscreener）**不进扫描**，只在逐币那一步被查询 —— 它没有全链列表，`fetch_universe` 返回空，所以只可能赢下逐币那一步。**同一个站点的批量那一半是 `dexscreener_rank`（榜单）：它有全链列表，进 `sources:` 与 `scan_sources:`，不是 ENRICH_ONLY，两者并非同一对象。** | 反例一：把 dexscreener 加进 `scan_sources`，只会在凭证行里显示一个 0；真正的代价在另一处 —— 只有被逐币补齐的那批行带市场口径，池子里其余行还是粗筛口径，分位分母因此是混的（见 `CLAUDE.md` · 边界与待决）。反例二：`symbol` 是唯一一个它排在**表尾**的字段 —— 须排在发射台之后，否则自报的名称会被覆盖；但表里不能没有它，否则 Robinhood 上一只普通 Uniswap 池的名字会直接变成「—」。 |
| 逐字段归属写在 `pipeline/collect.py` 的 `FIELD_PRIORITY`，改前先读那张表。两处不显然：GoPlus 独占 `pool_list` / `top_holders` / 几个筹码与税字段；**blockscout 不在 `scan_sources`**（列表接口只有持有人数、市值与流动性全空，会把候选池灌成四千个），它只在合并链里做兜底，且 `blockscout_checked` 不走 `http.cached`。 | 若让 gmgn 抢占 `pool_list`，其空列表会遮蔽 goplus 的结果（见 `api/app/pipeline/README.md` · 取值与合并）。「浏览器不认识这个地址」每轮都会重新查询，这是已知的重复代价。 |
| 税率只认 GMGN 安全表（路径按链取：EVM 是 `token_security_evm`、Solana 是 `token_security_sol`，见 `SECURITY_PATHS`；缓存 6h）；同表带 `is_honeypot`，是貔貅判定的第二声。**Solana 的表只有正面结论可用**：十个实测样本里它一律没有 `lock_percent`、没有 `lock_tags`，对 BONK 与 WIF 都回 `false` —— 那是「GMGN 没有锁仓记录」，不是「未锁」，所以 `LP_LOCK_ONLY_WHEN_TRUE` 只认 `true`，`false` 落成未判定。 | GMGN 排行行的税率是**占位 0**、goplus 在 Arc 上也全是 0，而 **0 是「有值」、会赢走字段** —— 把 goplus 兜底进来，报告里全部税率的 0 都是假的。`false` 是硬淘汰（`core/filters.py`），照搬一个「没有记录」会把整条链按沉默淘汰。 |
| LP 判定先链上、答不出才用 GoPlus：读 `lp_holders[]`（每条带 `percent` / `is_contract` / `is_locked`）—— **多数 LP 在合约里（锁仓合约 / 金库 / 发射台持仓）＝已锁，多数在钱包里＝未锁**；表覆盖不到 50% 的 LP 时不给答案。判定有两条路径（`_recorded_lp` / `_lp_from`），**改口径时两条都要动**；「未判定」的原因取 `LAUNCH_*` 字面值。 | v4 池没有池合约（头寸是头寸管理器里的 NFT），这就是链上读答不出的原因 —— 要补最后一段只能问头寸 NFT 的 `ownerOf`。卡片只分三态（见 `web/src/features/README.md` · 报告卡）。 |
| 源码验证是双源取或：语义是**任一源确认即算已验证**（`dual` 取 `or`）。 | 写成 `and` 会在 Sourcify 收录很差的 Arc 上一票否决。 |
| 链上直读走链自己的 RPC：`sources/onchain.py` 读 LP 位置与 dev 余额，走该链 `yaml` 里的 RPC；一条链只有一个端点、没有备份，记账走同一套 `tally`（见 `api/app/pipeline/README.md` · 缓存与记账）。 | **不得再添加默认端点**：曾经兜底到 Arc 的节点，链配置漏写 `rpc` 就会拿另一条链的数据、还标着本链的名字。缺 `rpc` / `explorer` 键现在**直接报错**。 |
| 安全判定判**整个过关池**（`pipeline.gate_limit`，默认 `0` ＝ 全池）：读不出来的是「未判定」（既不计通过也不计淘汰）。**这里没有「凑够 N 张卡就停止」的预算** —— 按流动性取前 N 再判，就是把名额先给了最深的那批，而链上排最深的行正是筹码 / 安全规则最想淘汰的那批。真正的预算上限在花钱那一步：链下调研的 `research_limit`（见 `api/app/report/README.md` · 链下调研）。 | 反例：默认取前 20 判定 —— 深而质量差的行占满名额，浅而干净的行无人裁决。 |
| 逐币富集：同币多源并发问、**结果按源顺序合并**，`_absorb` 的结果不随并发顺序变。 | 反例：按「哪个源先回答」合并，源换了顺序结论就变。 |
| GeckoTerminal 精查延后到入选之后（`DEFERRED_ENRICH`）：它是唯一能回答「几个池、价格走过的路」的源，也是最贵的一个（逐币三端点、同主机 2.2s 节流、429 固定退避、加线程无效），而它填的全是卡片字段（不进安全判定、不进分数）。 | 第二遍**不应**从批量行重建 `views`（第一遍刚拿到的逐币值会被粗筛行覆盖），要并进第一遍的 `views` 再 merge。 |
| **链本币不是代币**：GeckoTerminal 与 DexScreener 都用零地址 `0x000…0000` 表示链自己的币（BSC 上是 BNB、Robinhood 上是 WETH）。冷热页合并时按 `models.is_placeholder_ca` 剔掉 —— 检查点必须在合并处，冷页那一半是缓存中已拼好的行。 | 这行能通过**全部**硬过滤：它的市值是空的，每条市值规则都判「未判定」，而「未判定」从不淘汰；它的流动性却是全链本币池的总和，于是每个排名都拿第一。BSC 上三轮都把它当作第 1 张卡发出（链下调研按「BNB」写成了另一家的词条），Robinhood 上同一行是 WETH，被 20 万上限挡掉，所以没有出现在报告里。 |

## 为什么这么定

每个条目都是由一个具体的错误纠正出来的；现行口径见右列。

| 曾经的错 | 现在写在哪 |
| --- | --- |
| gate 曾自行获取池面读数（不进卡片、不进分）→ 被 GeckoTerminal 限流，gate 54s 涨到 242s | 本文 · 数据源、安全判定与富集（GeckoTerminal 精查延后） |
| 安全判定曾只判按流动性取的前 20 个候选 → 深而质量差的行占满名额，健康币从未被查询；进度五段的额度也还是当时的墙钟定下的 | 本文 · 数据源、安全判定与富集（安全判定判整个过关池）；`api/app/pipeline/README.md` · 进度与完成态 |
