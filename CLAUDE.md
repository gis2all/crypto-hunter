---
doc: CLAUDE.md
role: 项目唯一真相来源（agent 接手文档）
stack:
  backend: Python 3.13 + FastAPI + SQLite(WAL) + httpx（GMGN 要 TLS 指纹，走 curl-cffi），uv 管理，依赖精确 pin
  frontend: React + TypeScript(strict) + Vite + TanStack Query，无 UI 组件库，样式全部自写
  deploy: 同域单进程 —— FastAPI 托管 web/dist，只有一个端口 8787
  dev: Vite dev server 5173，/api 代理到 8787
  schedule: 服务内 ticker + runs 表唯一约束做幂等（run_key），不用 APScheduler
  icon: lucide-react，统一走 web/src/components/Icon.tsx
run: uv run --project api python -m app
build: cd web; npm run build
verify: cd api; uv run python -m pytest -q      # 基线 479 绿
verify_web: cd web; npm run build                # tsc -b 不过就是构建不过
truth:                                           # 每个领域的唯一出处，本文件只写规矩，不抄内容
  - api/app/store/schema.sql      数据模型
  - web/src/styles/               视觉（tokens -> proto -> app，加载顺序即优先级）
  - config/chains.yaml            出厂设定（每条链一套）
  - api/app/sources/SOURCES.md    逐源端点、传输方式、实测结论
ids: C-### 契约红线（§0.3）| R-### 规则 | P-### 踩过的坑（附录 A）| M-### 实测台账（附录 B）| Q-### 待决（§8）
---

# 维护这份文档

- **只写现行的规则、键名与实现位置，不记改动史。** 改了口径就**改写**那一条，不要追加「某天起改成…」—— 过程与当时的实测数字属于提交信息，不属于这里（它们已经进了附录 A / 附录 B，别再抄一遍）。
- **碰了分层 / 契约 / 运行方式 / 数据模型 / 视觉规则**，就回来改这里。
- 视觉的真相只有一处：`web/src/styles/`（`tokens` → `proto` → `app`），没有别的设计源，旧原型已删。
- 正文只写编号；**同一个编号全文只能有一个定义**。新增规则时接着各自区段的号往下排，不要插队。

---

# 0. 一页纸

## 0.1 是什么

每 3 小时，从一条链上的几千个币里挑出 N 个（默认 5 ＝ `per_round`），每个附一份两分钟能读完的判断。**研究流水线，不是行情终端。**

## 0.2 不做什么

交易、仓位建议、买卖点、实时行情、信号指标、多用户、回测引擎。当前只接了 **Arc** 一条链（左栏只渲染 `config/chains.yaml` 里 `enabled` 的链）。

## 0.3 红线（C-001 – C-006）

- **C-001 分层不成环**：前端不碰数据源与数据库；后端不内嵌 HTML（只托管 `web/dist`）；`api/app/core/` 里**不许有 IO**。
- **C-002 一个源一个适配器，源与源互不感知**：同名指标取谁的值由 `pipeline/collect.py` 的 `DEFAULT_PRIORITY` / `FIELD_PRIORITY` 决定，不由调用顺序决定。
- **C-003 文案只在后端**：页面上的解释性文案由 `api/app/api/v1/forms.py` 给（「说明」icon 的内容），前端不认识任何一条文案。
- **C-004 分数只在后端算**：前端不算分、不补数，页面上每个数字都来自 `/api/v1`。
- **C-005 一轮只写一次**：卡片文字在落库那一刻就是终稿，没有任何阶段回头改写已发布的报告。
- **C-006 缺值语义 ＝ 未知**：没有的数是 `None` / 空，不许用 0 或空字符串替代。

## 0.4 怎么跑

```powershell
# 后端：127.0.0.1:8787。生产也只有一个进程 —— 它同时托管 web/dist。
uv run --project api python -m app

# 前端（开发）：5173，/api 代理到 8787
cd web; npm install; npm run dev
```

- 生产：`cd web; npm run build`（内部是 `tsc -b` + `vite build`），再起上面那条后端命令。前端改完要重新 build 并强刷浏览器。
- **改过 `.py` 之后要重启后端才生效**（常驻进程，不重启跑的还是旧代码）。
- 接口类型：Pydantic 是唯一真源 → `cd api; uv run python -m app.export_openapi` → `web/src/api/types.gen.ts`（`cd web; npm run types` 重新生成）。**不许手写 DTO。**
- 本机快捷方式 `scripts/dev.ps1` / `build.ps1` / `openapi.ps1` **不在仓库里**（`.gitignore` 忽略了 `scripts/`），与上面几条原生命令等价。

## 0.5 怎么验（改完必须做）

| 改了什么 | 跑什么 | 通过标准 |
| --- | --- | --- |
| `.py` | `cd api; uv run python -m pytest -q` | 基线 **479 绿**；然后**重启 API** |
| 前端 | `cd web; npm run build` | exit 0（内含 `tsc -b`） |
| `prompts/*.md` | `pytest tests/test_prompts.py` | prompt 里的 JSON 示例与 `NewTokenEntry` 一致 |
| 视觉 | 1726 视口深 / 浅各截一遍 | 覆盖报告页（折叠 ＋ 全部展开）、设定页（含链下调研块）、本轮总览；坐标与计算值用 `getComputedStyle` 验，不靠肉眼 |
| 数据源解析 | `pytest tests/test_<source>.py` ＋ 清对应前缀缓存 | 见 R-063 |

长命令（build / pytest）把输出重定向到日志再看尾巴，别把整屏刷进上下文。

## 0.6 本机环境与坑（Windows）

- **PowerShell 语义**：不要用 `&&`，用 `;` 或 `if`；含 `&` 的 URL 要加引号。
- **写文件一律 UTF-8 无 BOM**（JSON / TSX / MD / YAML 尤其）；不要用 PowerShell here-string 写非 ASCII 内容。
- 终端里中文显示成 `????` 是 GBK 显示问题，**文件没坏**，用 UTF-8 读一遍再下结论。
- 视觉验证用的 playwright 必须给 Chromium 绝对路径：`C:/Users/12620/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe`。
- 服务常驻 `http://127.0.0.1:8787`，托管的是 `web/dist`：前端改完要 build，浏览器要强刷。
- **有提交历史，但本次重构大部分还没提交**（当前分支 `feat/search`，`git status` 里仍有大量 ` M` / `??`）：删文件前先确认它没被跟踪，动手前想清楚。

# 1. 任务索引

| 我要做… | 读 | 动 |
| --- | --- | --- |
| 加 / 改一个数据源 | §3 ＋ `SOURCES.md` | `api/app/sources/` ＋ `registry.py` ＋ `pipeline/collect.py` |
| 调硬过滤默认值 | R-005 – R-007 ＋ R-013 | `config/chains.yaml`（保存后以 `settings_versions` 表为准） |
| 改评分权重 / 因子 | R-025 – R-027 ＋ §4.3 | `core/scoring.py` ＋ `config/chains.yaml` |
| 改卡片印什么 | R-071 – R-083 | `api/app/report/cards.py` |
| 改卡片文案规则 / 结论写法 | R-037 – R-047 | `api/app/report/prompts/research.md` ＋ `report/schema.py` |
| 改设定页字段 / 说明文案 | R-084 – R-090 | `api/app/api/v1/forms.py` ＋ `web/src/features/settings/` |
| 改视觉（颜色 / 字号 / 主题） | R-097 – R-112 | `web/src/styles/tokens.css`（先改这里，再动 proto / app） |
| 改一轮的阶段或顺序 | §4.0 | `api/app/pipeline/decide.py` ＋ `progress.py` |
| 加 / 改数据表 | R-068 | `api/app/store/schema.sql` ＋ `store/db.py` |
| 改搜索（挖链） | R-031 / R-032 | `api/app/search/`（配置 `config/search.env`） |
| 查「这个数字哪来的」 | 附录 B（M-###） | — |
| 想知道为什么这么设计 | 附录 A（P-###） | — |
| 想动待决里的东西 | **先问用户** | §8 |

# 2. 术语

| 词 | 意思 |
| --- | --- |
| 扫描 | 本轮 pull 拉到的全部行（`scanned`） |
| 淘汰 | 被某条规则判负（`dropped`）；按优先级只归一次账，见 R-012 |
| 候选 | 过完安全门槛的池，**含入选**（`candidates`） |
| 入选 | 最终出卡的那 N 个（`selected`） |
| 未判定 | **没问到**，不是不合格 —— 既不计通过也不计淘汰 |
| 判定池 | 送进安全判定的那一批（按流动性取前 `gate_limit` 个） |
| 链上质量 | 综合分里 55% 的那半，五个链上因子的池内分位 |
| 链下调研 | 综合分里 45% 的那半：五项 0–5 判断 ＋ 卡片文字 |
| 词条 | 一个币本轮的链下调研结果（`lexicon` 表一行） |
| 池内排名 | 该币在本轮候选集里的分位名次（卡片上那一行） |
| 距高回撤 | 距历史最高点跌了多少，见 R-013 |
| 貔貅 | 能买不能卖；判定与门槛是两个不同的数，见 R-014 / R-077 |
| 陷阱占比 | GMGN 的 `top_entrapment_trader_percentage` |
| 捆绑 / 狙击 / 老鼠仓 | GMGN 的筹码标签（bundler / sniper / insiders） |
| 发射台 | launchpad；Arc 上多数新币由它发出来 |
| 主池 | 流动性最大的那个池 |
| gate | 安全判定那一段（`§4.0` 的 `gate`） |
| sharpen | 入选之后补读 GeckoTerminal 那一段 |

# 3. 代码地图

| 路径 | 责任 |
| --- | --- |
| `api/app/api/v1/` | HTTP 层：`chains` / `runs` / `lexicon` / `settings` / `health` |
| `api/app/core/` | 纯逻辑：`filters` / `funnel` / `scoring` / `models`（**不许有 IO**） |
| `api/app/pipeline/` | 一轮的编排：`collect` / `decide` / `states` / `progress` / `run` / `schedule` / `write` |
| `api/app/report/` | 报告：`facts`（唯一事实表）/ `cards` / `assemble` / `lexicon` / `research` / `prompts` |
| `api/app/search/` | 挖链：`base`（命中与 URL 归一）/ `identify`（读页面定 tier）/ `discover`（搜索 ＋ 互证）/ `harvest` / `engines/xsearch` |
| `api/app/sources/` | 一个源一个适配器 ＋ `registry` ＋ `http`（缓存与节流）＋ `SOURCES.md` |
| `api/app/store/` | SQLite：`schema.sql` ＋ `db.py` |
| `api/tests/` | 479 个测试，按模块分文件 |
| `web/src/features/` | `report` / `settings` / `run` / `rail` / `overview` |
| `web/src/styles/` | `tokens.css`（两态颜色）-> `proto.css`（布局）-> `app.css`（覆盖），**加载顺序即优先级** |
| `web/src/components/` | `Icon`（唯一图标入口）/ `CopyCa` / `Rich` / `Mark` / `ArcMark` |
| `config/` | `chains.yaml`（出厂设定）/ `llm.env`（写手）/ `search.env`（X 会话）/ 各自 `.example` 模板 |
| `data/` | `crypto-hunter.db` ＋ `raw/`（每轮原始响应与 HTTP 缓存）；不入库 |

# 4. 流水线

## 4.0 一轮的阶段

`拉扫描源 -> 去重 -> 硬过滤（免费半）-> 补齐链上数据 -> 硬过滤（筹码半）-> 按流动性取前 N -> 安全判定与富集 -> 链下调研与写作 -> 打分排序 -> 选前 N -> 落库出卡 -> 归档 -> 下一轮对比上一轮`

| 阶段（`phase` 键） | 做什么 | 实现 |
| --- | --- | --- |
| `pull` | 每个扫描源拉一次全量 | `pipeline/collect.py` |
| `dedupe` | 按 CA 去重，逐字段按优先级合并 | `collect.py` + R-002 |
| `prefilter` | 免费半硬过滤（只吃粗筛行） | `core/filters.py` `check_prefilter_free`（R-003 / R-005 – R-007 / R-014） |
| `topup` | 逐币补齐持有人 / Top10 / 捆绑 | `decide.Engine._top_up`（R-009 / R-010） |
| `gate` | 按流动性取前 N -> 逐币富集 -> 安全判定 | `decide.Engine._gate`（R-011 / R-015..R-021） |
| `draft` | 链下调研与写作（X 搜索 ＋ 读页面 ＋ 写词条） | `report/research.py`（R-029 – R-036） |
| `score` | 链上分位 ＋ 链下块 = 综合分 | `core/scoring.py`（R-025 – R-028） |
| `sharpen` | 选出前 `per_round` ＋ 入选后补读 GeckoTerminal | `decide.Engine._picks`（R-024 / R-143） |
| `write` | 落库（一笔事务） | `pipeline/write.py`（R-068 / R-143） |

每一步的口径在下面各条；动手改流水线之前先把 4.1 – 4.7 读完。阶段耗时每轮打一行日志（`round <id> phase timings: ...`），额度见 R-141（§7.6）。

## 4.1 硬过滤（R-001 – R-014）

- **R-001 拉取：universe 的主源是「池」不是「币」**
  - 规则：GeckoTerminal 分页拉池（`FULL_PAGES = 12`，全量 ≈ 3249 个池/币），RadarDEX 一次拉全量做长尾补充（`limit=2000`，覆盖 ≈ 4145）。Arc 没有 token 级列表端点。
  - 位置：`sources/geckoterminal.py` 的 `universe_pools()` / `sources/radardex.py`
  - 反例：拿 RadarDEX 的 `sort=mcap` 排序当全链排名 —— 它的市值口径与 GMGN / GT 不是一回事（M-001）
- **R-002 一个源一个适配器，合并按优先级**：同名标量取谁的值由 `collect.py` 的 `DEFAULT_PRIORITY` 决定，字段级例外写在 `FIELD_PRIORITY`；集合类型（`socials` / `websites` / `pool_list`）取并集，空列表不赢走字段。位置：`pipeline/collect.py` 的 `merge`。反例：按「谁先回答」取 → 源换了顺序结论就变。
- **R-003 空壳淘汰（默认开启）**：市值与流动性同时缺失的行直接淘汰 —— 那不是「源没回答」，是「没有可答的东西」。链配置写 `require_market_data: false` 才关；`MISSING_DATA` 在 `PREFILTER_PRIORITY` 排最后。位置：`core/filters.py` 的 `check_prefilter_free`。反例：老快照设置里没有这个键 → 一轮把四千个空壳行全判成「未知 = 通过」。
- **R-004 零数据淘汰**：五个排序因子（`scoring.FACTOR_FIELDS`：流动性 / 24h 成交 / 持有人 / Top10 / 捆绑）里一个数都没有的按 `missing_data` 淘汰 —— 它没法打分。**只在富集之后判**（`check_all`），因为富集前空字段的含义是「粗筛没带这个字段」。关掉：`prefilter.min_scored_fields: 0` 或缺键。位置：`core/filters.py` 的 `check_scored_data` / `scored_fields`。反例：放进 `check_prefilter` → 粗筛没带字段的行整批误杀。
- **R-005 免费半：市值区间**：`market_cap_min_usd = 10000` / `market_cap_max_usd = 200000`。下限压到 1 万是为了不漏刚起步的那批（Arc 上 85% 的币停在 launchpad 发币时的 5000–9000 U 初始 FDV）。位置：`config/chains.yaml` 的 `pipeline.prefilter`（M-002）。
- **R-006 免费半：两条下限**：`liquidity_min_usd = 5000`、`volume_24h_min_usd = 1000`。位置同上（M-002）。
- **R-007 持有人是区间**：`holders_min = 100`，`holders_max` 默认 `null` ＝ 不设上限；上限与下限共用「持有人」这一条规则（`_ceiling`），所以淘汰计数、漏斗行、设定页那一行都不用改。**缺键 ＝ 无上限**（老快照没有它时不能开始砍币）。位置：`core/filters.py` 的 `_ceiling`。反例：随手给 `holders_max` 一个数 ＝ 凭空开始砍币，而 Arc 上「市值 1–20 万的币持有人能到多少」没有实测过（Q-004）。
- **R-008 未判定永不淘汰**：整源缺失记 `None`，既不判通过也不判淘汰。所以硬过滤的筹码半跑在补齐之后 —— 先付钱去看，再裁决。位置：`core/filters.py` 的 `check_prefilter_free` / `check_prefilter_enriched`。反例：池子里混着持有人 2 / 3 / 4 的行，粗筛行读出来是 `None`，不补就直接放行。
- **R-009 补齐链上数据是独立的一次**：`_needs_chips` 决定补谁（缺持有人 / Top10 / 捆绑任一项），只补按流动性排在前 `ENRICH_POOL_MULTIPLE × gate_limit` 的行（`gate_limit: 0` 时补全池）；答案落 `item.metrics`，但**不置 `enriched`、不写 `facts`** —— 安全判定仍然只由 gate 那一遍给。位置：`decide.Engine._top_up`。反例：把补齐当成「已富集」→ gate 会以为这个币已经判过。
- **R-010 补齐走轻量入口**：GMGN 那一半用 `GmGn.fetch_chips`（只发 `token_stat` 一个请求，而不是 `fetch_token` 的四个端点，映射共用 `_stat_facts`）；DexScreener 同批必问 —— 它是行情口径源，而发射台自报的深度是虚的（Tolly 全族 5.52 倍）。位置：`sources/gmgn.py`。反例：用全量 `fetch_token` → 这一步的耗时就是「行数 × 0.45s 每主机节流」，worker 开 8 / 16 / 24 都一样（已到节流下限，加并发无用）（M-003）。
- **R-011 短名单排序键 ＝ 流动性**：`_depth` 按流动性降序，**完全相同**才用 24h 成交额破平 —— 成交额永远不会把浅池排到深池前面。位置：`decide._depth`。反例：用 `min(流动性, 成交额)` 或用成交额当主键 → 深但没量的池会被刷量的浅池挤掉。
- **R-012 淘汰数按优先级累计**：不是独立计数。优先级顺序即设定页 `RULE_ROWS` 顺序：市值 -> 流动性 -> 持有人 -> Top10 -> 捆绑 -> 貔貅 -> 24h 成交 -> 源码 -> LP -> 危险权限；**同时踩真阈值的仍记在阈值那一行**（距高回撤排最后）。位置：`core/funnel.py` 的 `cumulative_drop_counts` / `DROP_PRIORITY`。反例：各行独立计数 → 加起来不等于 `dropped`。
- **R-013 距高回撤**
  - 规则：距历史最高点回撤 ≤ `-70%` 淘汰；**三个读数任一枚举跌够即淘汰**，不是三个都要同意。
  - 读数：① **源高点** `metrics.high_market_cap_usd`（GMGN `history_highest_market_cap`，随 pull 就来，是测量值）；② **库记录高点** `tokens.peak_market_cap_usd`（每轮把源高点回写，upsert 只升不降，所以源沉默的轮次也仍有真高点）；③ **池面高点** `metrics.drawdown_pct`（GeckoTerminal 按池子全历史日线，`ATH_DAYS = 1000`）。
  - 噪声闸：`HIGH_SANITY_MULTIPLE = 50` —— 任何高点超过链市值上限 ×50 视为解析噪声丢弃（三个来源共用）。
  - 无读数时：整源沉默（`drawdown_unread` 未置位）＝ 放行（**首见不算「未知」**）；池面**读失败**（429 / 超时，`fetch_token` 置 `drawdown_unread = True`）＝ 一个没问到的问题、不是答案，记**未判定**而非静默放行。
  - gate **不买**池面读数（一次问二十个会被 GeckoTerminal 打限流），池面由画卡那一遍顺带买（`_picks` 的 `_enrich(only=DEFERRED_ENRICH)` 之后 `check_drawdown` 再判一次，跌够当场淘汰、顺位补下一个，所以卡片数不会因此变少），且**这一遍记 `unknown`**（池面读失败的入选币在报告里显示未判定，不是静默通过）。
  - 位置：`core/filters.py` 的 `drawdown_readings` / `check_drawdown`；键 `pipeline.prefilter.drawdown_max_pct`。配套：`geckoterminal.py` 的 `_ath_pools()` 取最深池 ＋ 第二深池（≥ 最深 10%）各读一条日线、取较大高点；`_read()` 对 429 退避重试（5 / 10 / 15s，`RATE_LIMIT_TRIES = 3`）、404 记 `NEGATIVE`；`OHLCV_TTL` 6h → **24h**。
  - 反例：要求三读数一致 → 那批 169 个已跌够的币一个没淘汰，ARCAT 漏进报告（M-004）。
  - 关联：R-014、Q-002。
- **R-014 硬过滤分两半，中间夹一次补齐**：① **免费半** ＝ 市值区间 / 流动性下限 / 24h 成交下限 / 空壳淘汰 —— 只吃粗筛行，先跑，代价是零请求；② **筹码半** ＝ 持有人区间 / Top10 / 捆绑 / 貔貅 —— 只有 GMGN 的批量行全带这三项，粗筛没带的行读出来是 `None`，所以先按 `MARKET_ENRICH = ("dexscreener", "gmgn")` 把缺的买回来再裁决（R-008 / R-009）。位置：`core/filters.py` 的 `check_prefilter_free` / `check_prefilter_enriched` ＋ `decide.Engine._top_up`。反例：把两半合成一次 → 未知等于放行，上千个发射台自报的行直接进池。

## 4.2 数据源 · 安全判定与富集（R-015 – R-024）

**分工表**（逐源端点、传输方式与实测结论见 `api/app/sources/SOURCES.md`，改源之前先读那份）：

| 用途 | 源 |
| --- | --- |
| 全链扫描（`scan_sources`） | gmgn / radardex / tolly / geckoterminal |
| 安全与源码 | goplus（貔貅、税、危险权限、`dex[]` / `holders[]` / `lp_holders[]`）/ sourcify（源码验证） |
| 只补链接（`registry.ENRICH_ONLY`） | dexscreener：只填 `socials` / `websites` / `description`，**不填任何进分的数** |
| 挖链找页面（在 `app/search/`，不在 `sources/`） | X 高级搜索（`search/engines/xsearch.py`，cookie 子进程，不吃 token）＋ `identify` 读页面互证；只交出候选链接，tier 由 `identify` 挣 |
| 链上直读 | `sources/onchain.py` 走链自己的 RPC（LP 位置、dev 余额） |

- **R-015 扫描源与富集源分开**
  - 规则：`scan_sources` 只放能供全链表的那几家；`registry.ENRICH_ONLY` 的源（当前只有 dexscreener）**不进扫描**，只在逐币那一遍被问，且只允许填链接类字段 —— 任何进分的数都不许从它来。
  - 位置：`api/app/sources/dexscreener.py`、`api/app/sources/registry.py`；调用点 `pipeline/decide.py` 的 `Engine._enrich`。
  - 反例：把 dexscreener 加进 `scan_sources` → 它的行情口径混进分位分母（R-025）。
  - 关联：R-001、R-048。
- **R-016 GoPlus 独占这些字段**
  - 规则：`pool_list` / `top_holders` / `burn_pct` / `locked_holder_pct` / `contract_holder_pct` / `transfer_tax_pct` 只从 goplus 取；`holders` / `top10_pct` / `buy_tax_pct` / `sell_tax_pct` 以 gmgn 为主、goplus 兜底。一次响应 34 个字段，只用其中一部分是浪费。
  - 位置：`pipeline/collect.py` 的 `FIELD_PRIORITY`。
  - 反例：让 gmgn 抢 `pool_list` → 它的空列表把 goplus 的答案挡在外面（R-050）。
- **R-017 blockscout 不在 `scan_sources`**
  - 规则：它仍留在合并优先级链里（做兜底），但**不许进扫描** —— 列表接口只有持有人数，市值与流动性全空，会把候选池灌成四千个。explorer 的 `blockscout_checked` 不走 `http.cached`，所以「浏览器不认识这个地址」每轮都会重问，这是已知的重复代价。
  - 位置：`api/app/sources/blockscout.py`、`pipeline/collect.py`。
  - 反例：为了「更全」把它加回 `scan_sources`。
- **R-018 税率只认 GMGN 安全表**
  - 规则：读 `gmgn.ai/api/v1/token_security_evm/{chain}/{ca}` 的 `total_buy_tax` / `total_sell_tax`（缓存 6h）。GMGN 排行行的税率是占位 0、goplus 的税率在 Arc 上也全是 0，而 **0 是「有值」、会赢走字段** —— 所以 `FIELD_PRIORITY` 里 `buy_tax_pct` / `sell_tax_pct` **只列 gmgn**，没有表的标的宁可显示「—」。
  - 位置：`api/app/sources/gmgn.py`、`pipeline/collect.py` 的 `FIELD_PRIORITY`。
  - 反例：把 goplus 兜底进来 → 报告里全部税率的 0 都是假的（真值在 GMGN 那张安全表上）。
  - 关联：同一张表带 `is_honeypot`，是貔貅判定的第二声（R-077 的三态另有实现）。
- **R-019 LP 判定先链上、答不出才用 GoPlus**
  - 规则：`onchain.lp_lock_from_holders()` 读 `lp_holders[]`（每条带 `percent` / `is_contract` / `is_locked`；`percent` 是 LP 供应占比、整表合计 1）—— **多数 LP 在合约里（锁仓合约 / 金库 / 发射台持仓）＝已锁，多数在钱包里＝未锁**；表覆盖不到 50% 的 LP 时不给答案。链上读答不出才走 goplus。
  - 位置：判定有 `_recorded_lp` 与 `_lp_from` 两条路径，**改口径时两条都要动**；「未判定」的原因取 `LAUNCH_*` 字面值（`LAUNCH_OK` / `LAUNCH_V4` / `LAUNCH_NO_TX` / `LAUNCH_EXPLORER` / `LAUNCH_NODE` / `LAUNCH_NO_TARGET` / `LAUNCH_NO_POOL` / `LAUNCH_BASIS`），实现见 `api/app/sources/onchain.py` 与 `report/cards.py`。
  - 反例：v4 池没有池合约（头寸是头寸管理器里的 NFT），这是链上读答不出的原因 —— 要补最后一段只能问头寸 NFT 的 `ownerOf`（头寸管理器地址在 `sources/onchain.py`）。
  - 关联：R-024、R-078（卡片只分三态）。
- **R-020 源码验证是双源取或**
  - 规则：`source_verification_mode` = 双源（GoPlus ＋ Sourcify），语义是**任一源确认即算已验证**（`dual` 取 `or`）。
  - 位置：`core/filters.py`、`pipeline/collect.py`。
  - 反例：写成 `and` → 在 Sourcify 收录很差的 Arc 上一票否决（R-051）。切「仅 GoPlus」会更宽松。
- **R-021 链上直读走链自己的 RPC**
  - 规则：`sources/onchain.py` 读 LP 位置与 dev 余额，走 `chains.yaml` 里该链自己的 RPC 端点；一条链只有一个端点，没有可换的备份。
  - 位置：`api/app/sources/onchain.py`；记账走同一套 `tally`（R-065）。
  - 反例：把链上读接到第三方 explorer API → 延迟反而更差（RPC 单次硬地板 2.2–2.5s，见 §7.1）。
- **R-022 安全判定限量（`pipeline.gate_limit`，`20`）**
  - 规则：按**流动性降序**取前 20 个送安全判定（排序键见 R-011），其余留「未判定」—— **既不计通过也不计淘汰**，本轮 note 写明「只判定前 N 个，其余 M 个未判定」。
  - 位置：`pipeline/decide.py` 的 `GATE_LIMIT` / `_gate(..., need=)`，测试 `test_engine.py`。
  - 依据：per-token 读取按主机串行节流（GeckoTerminal 2.2s/次），判完 163 个要 20 分钟，加并发无效（R-023）。
  - 两条补充：① **预算是预算，不是少出报告的理由** —— 预算花完若达标候选不足报告张数，继续按批往后判；② 最后一批按剩余预算截断（20 就是 20 个请求）。`0` ＝ 判完整个池子；想再加一层上限就用 `pipeline.gate_rounds`。
  - 反例：**判定预算就是预算**（R-014 的「预算不凑数」）—— 少出几张卡好过拿没数的行凑数；唯一例外是「一个都没通过」（零张卡是对池子的读数，这时才继续往后找）。
- **R-023 逐币富集：同币多源并发，结果按源顺序合并**
  - 规则：`ENRICH_WORKERS = 8`，同一个币的多个源**并发**问；合并顺序仍按源顺序，`_absorb` 结果不随并发顺序变。
  - 位置：`pipeline/decide.py`。
  - 反例：按「谁先回答」合并 → 源换了顺序结论就变（R-002）。
- **R-024 GeckoTerminal 精查延后到入选之后**
  - 规则：`DEFERRED_ENRICH = ("geckoterminal",)`。它是唯一能回答「几个池、价格走过的路」的源，也是最贵的一个（逐币三端点、同主机 2.2s 节流、429 固定退避 10s、加线程无效），而它填的全是卡片字段（不进安全判定、不进分数）。所以 gate 阶段跳过它，`picked` 定下来后只对入选的补一遍（`_enrich(..., only=DEFERRED_ENRICH)`），日志单独一行 `sharpen`。
  - 位置：`pipeline/decide.py` 的 `Engine._picks` / `_enrich`。
  - 反例：第二遍从批量行重建 `views` → 第一遍刚拿到的逐币值会被粗筛行盖掉。正确做法是**并进第一遍的 `views` 再 merge**；`facts` 与 LP 上链读取在这一遍整段跳过。
  - 关联：R-013（池面读数由这一遍顺带买）、R-015。
## 4.3 打分（R-025 – R-028）

- **R-025 分位分母 = 本轮硬过滤幸存者**
  - 规则：不是全历史池，也不是过完安全门槛的池子。实现 `core/scoring.py` 的 `CandidateSetPercentile`，中点法 `(below + 0.5*ties)/n`。分母用 `universe.metrics` 的**粗筛视图**构造。
  - 位置：`api/app/core/scoring.py`。
  - 反例：分母用富集后的 `item.metrics` → 同一批里被富集的 12 个会改变分母，同一支币的分数会随分批顺序漂移。
- **R-026 五个链上因子 ＋ 两个逆向因子**
  - 规则：`scoring.FACTOR_FIELDS` = 流动性 / 24h 成交 / 持有人 / Top10 / 捆绑；逆向因子 `REVERSE_FACTORS = {top10_concentration, bundling}`（越小越好，分位反算）。
  - 位置：`api/app/core/scoring.py`。
  - 反例：把 Top10 / 捆绑当正向因子 → 筹码越集中分越高。
- **R-027 综合分 = 链上质量 ＋ 链下调研**
  - 规则：`onchain_weight` 默认 55%、`offchain_weight` 默认 45%（`config/chains.yaml`）；链下那半由五项 0–5 等权合成（R-088）。
  - 位置：`core/scoring.py` ＋ `config/chains.yaml`（以 `settings_versions` 表为准）。
  - 反例：把权重写死在代码里 → 设定页改了不生效（R-096）。
- **R-028 链下调研没落地的标的 = `pending`（冷启动）**
  - 规则：本轮链下调研还没回来的，既不参与排名也不显示 0 分 —— 等它落地后下一轮才有分数（`scoring.score_token` 的 `pending`）。
  - 位置：`core/scoring.py`、`report/research.py`。
  - 反例：给 0 分 → 一个刚部署、还没来得及调研的币会被排到末尾当成「差」。
  - 关联：R-047（没词条就不进排名）、R-062（落选池要写明理由）。
## 4.4 链下调研（R-029 – R-036）

- **R-029 调研是一步，且写在记分之前**
  - 规则：判定池的每个币都读项目自己的页面（官网 / X / GitHub / TG），一次模型调用先把词条写好、读过的页面进 `lexicon_sources`，再拿这份词条去打分、落库 —— 它在轮次里跑，**不再有发布之后的异步动作**。
  - 位置：`api/app/report/research.py` 的 `research_entries`。
  - 反例：先打分、事后异步补词条 → 卡片文字与分数不同源，同一轮出现两个版本的判断。
  - 关联：R-024、R-027。
- **R-030 每轮全新调研，没有跨轮复用**
  - 规则：五项链下分与卡片正文都来自**本轮**的调研结果，词条带 `run_id`；落库前 `lexicon.forget_other_rounds(run_id)` 把其它轮次的词条删掉（连带 `lexicon_sources`）。旧设计的「2 天过期 / 市值波动 40% / 6h 跳过窗口 / 人工确认」**全部取消**。
  - 位置：`api/app/report/lexicon.py` ＋ `report/research.py`。
  - 反例：拿上一轮的词条顶这一轮 → 只要入选，这一轮就重新写一遍文字、重新打一遍分，卡片上的字永远是本轮的。
- **R-031 挖链：X 高级搜索两问**
  - 规则：`app/search/engines/xsearch.py` 拿 `config/search.env` 里的 cookie 跑 X 高级搜索，每个标的**两问**：CA 走 `Latest`、`$TICKER` 走 `Top`。合并后按「名字贴合 ticker」排序。
  - 位置：`api/app/search/discover.py`、`search/engines/xsearch.py`。
  - 反例：只发 CA 那一问 → 实测只有 ticker 那一问能捞到某些官方号（地址一问 20 条里一个都没有），所以两问都要发。
  - 关联：**搜索不走模型**（不花 token）；`SearchService` 按顺序问引擎、第一个有结果就停。没配 cookie（`config/search.env` 空着）时整层静默失效，卡片只用本轮登记的链接，**不阻塞流程**（R-138）。
- **R-032 互证：只读前 8 个候选页，tier 由页面自己挣**
  - 规则：`identify.read_profiles` 只读前 8 个候选页（刷屏账号会把这 8 个名额占满，所以排序决定谁先被读）；`tier` 仍旧由页面自己挣 —— **bio 带 CA 才是 own**。站点链接先归一到根域再判。
  - 位置：`api/app/search/identify.py`、`search/base.py`。
  - 反例：不归一 → 一个 launchpad 的三个深链会被逐个读成 none。
- **R-033 读页面：素材上限**
  - 规则：正文合计 **15000 字符**上限，链接最多 **8 个**；官方 X 时间线另给 **15000 字符**（X 走 `user-posts` 取最近约 20 条，全部带时间戳与互动数）。`READABLE_SOCIALS` **含 TG** —— `t.me/<name>` 的公开预览页带频道名、成员数与最近几条帖子，正是社媒行要的东西。
  - 位置：`report/research.py`（上限常量）、`app/search/`（抓取）。
  - 反例：把上限压到「够写一段」的量 → 模型只能凭事实表写，叙事退化成数字堆砌。
- **R-034 空壳页面交浏览器画，账号页永远不画**
  - 规则：静态 lane 不跑 JS，而 Arc 上的发射台官网多是 React 空壳。判定阈值 `identify.SHELL_WORDS = 40`（同一批次最低的官网有 94 个词，两边差得很开）。渲染只在**已证明是空壳**时发生，且只有**候选官网**走这条路（`identify.fetch_page`）；X 与 TG 是例外（`identify.NO_RENDER`）：X 只把 bio 给静态客户端、浏览器看到的是登录墙，TG 预览页画前画后都是 27 个词 —— X 账号页仍旧静态抓（`fetch_html`），浏览器只伺候候选官网（`fetch_page`）。
  - 位置：`api/app/sources/browser.py`（一个 Chromium、自己一个线程跑一个 event loop —— playwright 同步 API 跨线程会崩、标签页上限 6）、`search/identify.py`。等待用 `DOMContentLoaded + 2.5s`，`networkidle` 在 memespad 上是 21.8s，拦图片/CSS 反而更慢还会把 SPA 打残。实测单页 2.4–12.0s。
  - 反例：2026-09-27 实测 `memespad.com` / `www.arcdex.online` / `usearckit.online` 静态只有 8–10 个词、连 CA 都搜不到，渲染后是 377–827 个词且两个带 CA —— 之前 `usearckit.online` 因此判成 none，卡片上挂的是第三方站 `myarcade.fun`。第三方 `r.jina.ai` 已去掉。
  - 关联：R-033、R-036。
- **R-035 写词条：一个币一次调用，宽度是实测值**
  - 规则：每个币一次 `complete_json`（一个币的输出约 35k token 也稳）。并行宽度：找链接 `SEARCH_WORKERS = 15`、写词条 `CH_RESEARCH_WORKERS = 12`（默认）。
  - 位置：`report/research.py`、`config/llm.env`。
  - 反例：① 一次调用写完所有条目 → 必然撞天花板，已废弃；② 把 `SEARCH_WORKERS` 提到 20 → 实测 15 个并发跑 20 个标的零 429，20 个并发有 3 个 429 而且更慢。
  - 关联：一个币失败只丢它自己那条，原因进本轮 note（`链下调研未完成：…`），不阻塞整轮（R-047）。
- **R-036 LLM 客户端：配置、换 key、与三个坑**
  - 规则：词条写作走一个 OpenAI 兼容端点。配置在 `config/llm.env`，由 `api/app/config.py` 读取 —— **真实环境变量优先**，而这个文件**每次调用时重读**，所以改完下一轮生效、不用重启。模板见 `config/llm.env.example`。当前配置：`https://opencode.ai/zen/go/v1` ＋ `deepseek-v4.1-flash`（写作）；有没有写手看 `/api/v1/health` 的 `llm` / `llm_model`。

| 变量 | 作用 |
| --- | --- |
| `CH_LLM_BASE_URL` | 端点，客户端自己拼 `/chat/completions` |
| `CH_LLM_API_KEY` | Bearer。**逗号分隔可写多个**：第一个是主 key，后面是备用，按顺序试 |
| `CH_LLM_API_KEY_FALLBACK` | 追加的备用 key，同样逗号分隔；只想加备用、不动主 key 那行时用 |
| `CH_LLM_KEY_COOLDOWN` | 被拒的 key 跳过多少秒，默认 600；0 关闭 |
| `CH_LLM_MODEL` | 模型名 |
| `CH_LLM_TIMEOUT` | 单次超时，当前 300s。**必须高于成功调用的长尾** |
| `CH_LLM_MAX_TOKENS` | **推理模型必填**：思考也吃这个额度（该端点接受 65536，当前 32000） |
| `CH_LLM_REASONING` | 思考预算 `none` / `minimal` / `low` / `medium` / `high`，作为 `reasoning_effort` 发出。**当前 `none`** |
| `CH_RESEARCH_WORKERS` | 一次并行调研几个币（一个币一次调用），默认 12 |
| `CH_LLM_EXTRA_HEADERS` | JSON，额外请求头（本端点必须要 `x-opencode-session`） |
| `CH_LLM_TRIES` | 单个 key 的尝试次数，当前 1；只重试 5xx 与网络错误 |
| `CH_X_AUTH_TOKEN` / `CH_X_CT0` | X 搜索的会话 cookie（`app/search/engines/xsearch.py`），只放在 `config/search.env`。**搜索不走模型，所以没有「搜索模型」这一项** |
| `CH_X_CLI` | 承载该会话的搜索客户端，默认 `twitter`：PATH 上的子进程，不花 token |

  - **换 key 的规则**（`client._call_payload`）：只有 401 / 403 / 429 —— key 失效或额度用尽 —— 才换下一个 key，并且把换掉的那个晾 `CH_LLM_KEY_COOLDOWN` 秒，所以一个死掉的主 key 一轮只白撞一次。别的 4xx（模型名写错、请求体不合规）换 key 也是一样的错，直接失败；5xx 与网络错误在同一个 key 内按 `CH_LLM_TRIES` 重试。**日志里只出现 key 的 sha256 前 8 位指纹，不出现 key 本身。**
  - **`CH_LLM_TIMEOUT` 的口径**：实测 79 次 research 调用 p90 = 147s、最长成功 296s，180s 会砍掉正常调用，而砍掉之后还会重试一次。思考关掉后 research 单次实测 11–17s，300s 只剩兜底作用。**与重试相乘**：180s × 2 曾让一次死掉的调用独占 366s（那一轮 draft 421s）。
  - **`CH_LLM_REASONING` 的口径**：**只有 `none` 是真开关** —— `minimal` / `low` 照样花 680–899 思考 token，`none` 是 0。实测（同 5 个币、同一条生产路径、并发）：`none` 94.9s / 5 个词条全过 / 正文 8711 字符，端点默认 340.1s / 只过 4 个（丢的那个 248s 后回空消息）/ 正文 6199 字符 —— **快 3.6 倍、卡更全、字更多**。唯一代价是约四成草稿漏必填分项（`f_community` 这类），**由 R-045 的重问兜住**：那 5 个里触发 2 个、2 个都补回来。历史对照：同一批币在**没有重问**的旧代码上 `none` 只过 3/5，所以这一行和重问是一套，**不能只上一个**。
  - **三个坑（都已踩过）**：① **推理模型的思考计入 `max_tokens`** —— 额度给小了返回的是**空消息**（`finish_reason = length`），不是报错，客户端把空消息当失败，于是额度调小会静默降级成「没有文字」。② **该网点偶发 503** `Endpoint is unavailable`（`gpt-5.6-luna` 直接不可用），所以有上面那条重试规则。③ **一次调用只写一个币的词条**（R-035）。
  - 位置：`api/app/llm/client.py`、`api/app/config.py`。
  - 反例：hermes 里那两个 deepkey key（`sk-fm7NO…`、`sk-72CtQ…`）实测都是 401，**都不要用**。
## 4.5 词条文字（R-037 – R-047）

prompt 文件只有一份：`report/prompts/research.md`，`PROMPT_VERSION` 随它升级。**另有第二段提示词内联在代码里**：`research.py` 的 `REPAIR_HEAD`，只在校验没过时发出去（R-045），落库记为 `prompt_version = research.md@7+repair`、`purpose = research_repair` —— 后缀表示「research.md ＋ 一段代码里的重问指令」。

- **R-037 篇幅上限写在 prompt 里，代码只在超长时记日志、不退回**
  - 规则：一个币的词条丢不起（丢了就没有链下分），代价比文字长一点大得多。上限落在 `report/schema.py` 的 `CAPS`，超长走 `log.warning`，`validate_*` 返回值不变。同一个超长字段会记**两条**：`ask`（决定要不要重问）与 `one`（决定要不要丢这个币）各校验一次，两个判定点都要结果。
  - 位置：`api/app/report/schema.py` ＋ `report/prompts/research.md`。
  - 反例：超长就退回（重写或丢弃）→ 丢报告的概率远大于「文字长一点」的代价。

| 字段 | 上限 |
| --- | --- |
| `concl` | 500（prompt 要求 200–500，代码下限 160） |
| `prod_text` / `substance_text` / `team_text` / `narr_text` / `social` | 各 200 |
| `badge` | 16 |

- **R-038 安全只照抄**
  - 规则：涉及源码 / 权限 / LP 的句子必须**一字不改**地引用事实表 `【安全】` 行的原文（例：`源码 已验证 ｜ 危险权限 无危险权限 ｜ LP 已锁`）。
  - 位置：`report/prompts/research.md`、`report/facts.py`。
  - 反例：卡片曾一边显示「LP 已锁」一边在词条里写「三项全部未判定」。
- **R-039 链接统一成「说明 + 可点击词」**
  - 规则：`官网 <a href="...">solonpad.fun</a>` 这种形式。prompt 要求；`report/segments.py` 把 anchor / markdown / 裸 URL 都变成 link 段；前端 `Rich.tsx` 渲染成 `target="_blank"` 的 `<a>`。
  - 位置：`api/app/report/segments.py`、`web/src/components/Rich.tsx`。
  - 反例：**全链路没有 `dangerouslySetInnerHTML`** —— 不要让模型文本直接进 HTML。
- **R-040 页脚列所有入口，正文不放链接**
  - 规则：卡片的页脚来自 `cards.links()`：交易页 `GMGN ｜ Debot ｜ DexScreener`，然后是 `X ｜ TG ｜ GitHub`（取本轮指标里的社媒）和**第一个**官网。全部是文字下划线链接、**没有打开图标**。正文（含 `social`）不重复放这些链接。
  - 位置：`api/app/report/cards.py` 的 `links()`。
  - 反例：一排七个外链图标基本只剩图标（R-083）。
- **R-041 三条纪律**
  - 规则：① `concl` 与 `narr_text` **严禁重复**（确实没有新内容就留空 `""`）；② 提到外部信息必须附**实读到**的原文 URL；③ `social` 只写判断、用时间线的互动率与发帖节奏说话，**不放链接、不拿粉丝数当卖点**（粉丝数无效），第三方讨论只当「有多少人在谈」的计数用。
  - 位置：`report/prompts/research.md`。
  - 反例：拿 X 粉丝数当卖点 —— 新项目粉丝便宜，它证明不了任何事。
- **R-042 结论的写法**
  - 规则：`concl` 200–500 字（代码下限 160），prompt 定的五段 —— 定位约 110 / 实质约 100 / 亮点约 130 / 风险约 110 / 收尾约 40 字，**不加小标题**。① 第一句必须是身份句（链 ＋ 赛道 ＋ 定位）且**叙事先行**；② 风险段写最致命的 1–2 条、要事实性。
  - 位置：`report/prompts/research.md`。
  - 反例：**不写观点句**（「本批唯一…」「第一梯队」一律不写，判断留给读者）、**不写数字段**（市值 / 流动性 / 持有人 / 量能 / 筹码不罗列，表格与卡片上都有）。
  - 关联：卡片**不为叙事单开一块** —— 叙事落在结论里，`narr_text` 退为补充位（定位 / 机制 / 竞争位），通常留空（R-076）。
- **R-043 写作输入必须带页面正文**
  - 规则：`research.md` 的输入是「读到的页面 ＋ 事实表」；页面读不到时那个币就**没有词条**，而不是让模型凭记忆编 —— 所以 prompt 里没有「没读到就照事实表写」这种口径。
  - 位置：`report/research.py`、`report/prompts/research.md`。
  - 反例：给模型一句「如无资料请据常理推断」→ 报告里出现不存在的官网与产品。
- **R-044 调研必须带安全块**
  - 规则：`research._latest_metrics` 一并读 `safety_json`，`_facts_line(ca, symbol, metrics, safety)` 交给 `facts.block`。
  - 位置：`api/app/report/research.py`、`report/facts.py`。
  - 反例：不带安全块 → 管线里最宽的那张事实表恰好看不到安全块，词条会永远写「安全块三项全部未判定」。
- **R-045 先重问一次，再降级**
  - 规则：`ask` 把上一次的 JSON 和校验器原话（`REPAIR_HEAD`）一起发回去，要一份**完整重写**；第二次还不过才按降级处理（R-047）。漏写一个分项是笔误，不该因此丢一张卡。
  - 位置：`report/research.py`（`REPAIR_HEAD`）、`report/schema.py`。
  - 反例：不重问 —— 思考关掉后约四成草稿漏必填分项，`none` 那一档会直接损失卡片（R-036 的实测）。
- **R-046 「读不到」和「没东西」分开**
  - 规则：① **空壳页面等于没读** —— 静态 HTML 正文不足 `MIN_CHARS`（80）时 `_read` 返回 `None`，而失败的页面不落库，表面「读了 2 个链接」实际只剩一个；解法是两处读页面走同一道门（`identify.fetch_page`）：静态两条 lane 先走、正文不足 `SHELL_WORDS = 40` 个词就交给 Chromium 重抓、**两份取词多的**（R-034）。② **TG 要读**（`READABLE_SOCIALS` 含 TG）。
  - 位置：`app/search/identify.py`、`report/research.py`。
  - 反例：把「页面空」当成「项目没有官网」→ 模型看到的世界里这个项目凭空少一个入口。
- **R-047 降级 = 该币无词条**
  - 规则：`research.research_entries` 对读不到页面 / 答不出契约的币**不写词条**，该币没有链下分（`scoring.score_token` 的 `pending`）、不进排名 —— **不会用「确定性拼装的正文」冒充一份调研**。失败原因进本轮 note（`链下调研未完成：…`），**不阻塞整轮**。
  - 位置：`report/research.py`、`core/scoring.py`。
  - 反例：拿确定性模板拼一段文字顶上 → 卡片上有字、却没有调研，读者无从分辨。
  - 关联：R-028、R-062。
## 4.6 取值与合并（R-048 – R-056）

这些规矩都是被具体的错纠过之后定下的，**别再犯**。

- **R-048 判据是「谁的值更可信」，不是「谁先回答」**
  - 规则：同名的**标量**取谁的值由 `collect.py` 的 `DEFAULT_PRIORITY` 决定，字段级的例外写在 `FIELD_PRIORITY`（源侧分工见 R-015 – R-017）。
  - 位置：`pipeline/collect.py` 的 `merge`。
  - 反例：按「谁先回答」取 → 源换了顺序结论就变。
- **R-049 0 是答案，不是缺失**
  - 规则：`decide._absorb` 用 `is not None`（空列表也是答案）；GMGN 的 `X or Y` 用 `_first(X, Y)`；`creator_created_count` 的 `_positive` 用 `_num`（dev 发币数 0 是发现）。
  - 位置：`pipeline/decide.py`、`sources/gmgn.py`。
  - 反例：用 `if value:` 判空 → 0 被当成没答，dev 发币数 0（重要发现）被丢掉。
- **R-050 集合类型的空值算「没答」**
  - 规则：`collector.merge` 里 `[]` 不赢走字段（与 `price_series` / `socials` / `websites` 一直以来的写法一致）。
  - 位置：`pipeline/collect.py`。
  - 反例：不挡 → GMGN 的空 `pool_list` 会把 goplus 的答案挡在外面。
- **R-051 `source_verdict` 的 `dual` 取 `or`**
  - 规则：任一源确认即通过。
  - 位置：`core/filters.py`、`pipeline/collect.py`。
  - 反例：写成 `and` → 在 Sourcify 收录很差的 Arc 上一票否决（R-020）。
- **R-052 覆盖方向：`overlay(universe_bulk, per_token_merge)`**
  - 规则：逐币富集值盖在批量视图上，**别写反**。
  - 位置：`pipeline/decide.py`。
  - 反例：`overlay` **不复制 `provenance`**，所以 `provenance` 要单独合并。
- **R-053 卡片读数取「先有值的那一份」**
  - 规则：同一批读数库里存两份（`metrics` 批量榜单、`safety` 逐币判定），`cards._either(metrics, safety, key)` 逐字段取。
  - 位置：`api/app/report/cards.py`。
  - 反例：硬过滤与安检读的是 `metrics`，所以取错只是显示错、不是判断错 —— 但显示错同样要修。
- **R-054 税率只认 GMGN 安全表**
  - 规则：见 R-018。`dex[].pool_fee` 在 v4 上不可读，**池子清单不印费率**。
  - 位置：`report/cards.py`、`pipeline/collect.py`。
  - 反例：把 `pool_fee` 印上卡片 → v4 池子那一列全是「—」。

- **R-055 事实表只有一份实现**
  - 规则：`report/facts.py` 的 `lines()` / `block()` / `safety_line()` 由**卡片与词条两处共用**；缺值打印「—」（「五个源都没这个数」本身就是事实）。
  - 位置：`api/app/report/facts.py`。
  - 反例：卡片与词条各拼一份事实表 → 同一轮里两处对同一个字段的取值不一样（R-044 的「永远写未判定」就是这么来的）。
- **R-056 链接与社媒的取值规则**
  - 规则：链接**逐币从 DexScreener 读**（`sources/dexscreener.py`：只填 `socials` / `websites` / `description`，**不填任何进分的数**；它不在 `sources:` 里，而是 `registry.ENRICH_ONLY`，由 `Engine._enrich` 在逐币那一遍问 —— 位置在写词条之前，所以链接是写作输入的一部分；X 搜索挖到的候选链接是另一条来源，见 R-031 / R-032）。社媒的 handle / 裸域名由 `base.social_url` / `base.web_url` 补主机与 scheme（RadarDEX / Tolly / GMGN 共用）。
  - 取值规则：只认 `baseToken.address` 命中的那一对，否则若答案里只有一条链才接受。
  - 位置：`api/app/sources/dexscreener.py`、`api/app/search/base.py`、`pipeline/collect.py`。
  - 反例：另一条链的 X 句柄写到这张卡上 —— 比空着更糟。
## 4.7 读数与漏斗（R-057 – R-062）

**四个数（扫描 / 淘汰 / 候选 / 入选）必须一次数出来**，口径只有一个实现。

- **R-057 一次从 items 的 decision 数出来**
  - 规则：`core/funnel.py` 的 `summarize()`（`RoundResult.funnel_summary`，`write.py` 照抄），于是 `scanned = dropped + candidates` 是**恒等式**，不再是四个各自来源的数字碰巧排在一起。
  - 位置：`api/app/core/funnel.py`、`pipeline/write.py`。
  - 反例：四个数各自统计 → 加起来对不上（读者第一眼就会去加）。
- **R-058 候选含入选**
  - 规则：落选池标题的「候选」必须含入选（否则「候选 − 入选」不等于表里的行数）；设定页那句「漏斗」文案同理用 `universe − candidates`。
  - 位置：`core/funnel.py`、`api/app/api/v1/forms.py`。
  - 反例：候选不含入选 → 落选池行数与标题差一个。
- **R-059 `runs.dropped` 是总淘汰，份额单独列**
  - 规则：面板「硬过滤」那一行的份额单独写进 `run_summary.dropped_hard`（前端**不靠减法猜**）；「硬过滤」的份额由 `prefilter_dropped` 算（按优先级归属，含富集之后才被硬过滤规则淘汰的标的）。
  - 位置：`pipeline/write.py`、`core/funnel.py`。
  - 反例：前端用 `总淘汰 − 其他` 倒算 → 规则一改就错位。
- **R-060 落选池收全部候选**
  - 规则：有分的按分数排，没分的排在其后并写明理由（`未建档（本轮不参与排名）` / `数据缺失，本轮不参与排名`）。
  - 位置：`report/assemble.py`。
  - 反例：只收有分的 → 「候选 78」但表里只有 20 行。
- **R-061 退出项的名字从 `tokens` 取（CA 主档）**
  - 规则：退出项被画出来时那个币已经不在本轮报告里，只有主档还记得它叫什么。
  - 位置：`pipeline/decide.py` 的 `_changes`、`store/db.py`。
  - 反例：退回 `ca[:10]` → 读者看到的是一串没有意义的地址片段。
- **R-062 前端不重算这些数**
  - 规则：全部来自 `/api/v1`。
  - 位置：`web/src/features/`。
  - 反例：前端自己算一遍 → 与后端口径漂移（C-004）。
# 5. 数据与状态

## 5.1 缓存与记账（R-063 – R-067）

- **R-063 「对方说没有」也是答案**
  - 规则：生产者用 `http.NEGATIVE` 标记「对方答了，答案是空」，`cached` 会缓存它（读回来仍是 `None`，调用方无感）；真正的 `None`（超时、5xx）照旧不缓存。
  - 位置：`api/app/sources/http.py`。
  - 反例：不缓存否定答案 → Arc 上多数币在 GeckoTerminal 没有池，等于每轮重问、每次 3 次重试 ＋ 2.2s 节流。
- **R-064 失败退避只在失败路径上生效**
  - 规则：传输层默认 `sleep` 0.8、GT 逐币端点 1.0；成功路径的节流没动。Sourcify 的 404 是答案不是失败，所以 `tries=1`。
  - 位置：`sources/http.py`、`sources/sourcify.py`。
  - 反例：成功路径也退避 → 每轮白等几十秒。
- **R-065 记账要覆盖最贵的那台主机**
  - 规则：`http.tally()` 是公开的记账入口，`onchain.rpc_checked` 按同一套计数（ok / retry / fail ＋ 耗时）记账，所以链节点会出现在每轮的主机行里。
  - 位置：`api/app/sources/http.py`、`sources/onchain.py`。
  - 反例：漏记链节点 → 每轮主机行里最慢的那台看不见。
- **R-066 日志是唯一的耗时真相**
  - 规则：`app/logging_setup.py` 在 `python -m app` 启动时给 `crypto-hunter` 命名空间挂 stderr handler（`CH_LOG_LEVEL` 可调，默认 INFO，`propagate = False` 不污染 uvicorn）。秒表由 `run_chain` 持有、按 `run(clock=...)` 传入，引擎只填不打印（`owns_clock`），所以写词条与落库的耗时也在内。
  - 位置：`api/app/logging_setup.py`、`pipeline/run.py`、`pipeline/progress.py`。
  - 反例：不挂 handler → `logging.lastResort` 只写 WARNING 及以上，`progress.Timer` 算出的阶段耗时会被静默丢弃。
- **R-067 不要拿单轮数字下结论**
  - 规则：节点延迟轮间 2.2–4s 波动，`eth_getLogs` 的调用次数也轮间不可比（R-121）。
  - 位置：`pipeline/progress.py`。
  - 反例：用单轮 A/B 的调用次数当性能结论。

## 5.2 数据模型（R-068 – R-070）

**11 张表**，以 `api/app/store/schema.sql` 为准。

- **R-068 表清单**
  - `chains` · `settings_versions`（不可变，一次改动一行 JSON）· `tokens`（CA 主档，含 `peak_market_cap_usd`）· `runs` · `run_items`（每币进出原因）· `snapshots`（不可变，每轮每币）· `run_summary`（含 `dropped_hard`）· `reports`（已发布的卡片文字，冻结）· `lexicon`（**本轮的链下调研**：五项分 ＋ 卡片文字，带 `run_id`）· `lexicon_sources`（这一轮调研读过的页面）· `llm_calls`（模型 / 耗时 / 成本 / prompt 版本）。
  - 位置：`api/app/store/schema.sql`。
- **R-069 加列走 `db.init_db` 的 `_ADDED_COLUMNS`（配 `SCHEMA_VERSION`）**
  - 规则：老库靠它补齐并回填。
  - 位置：`api/app/store/db.py`。
  - 反例：**别手改 `.db`** —— 老库不会自动长出这一列。
- **R-070 三种表不许改写**
  - 规则：`settings_versions`（不可变，改设定 = 追加一行）、`snapshots`（每轮每币一写）、`reports`（落库即冻结）。
  - 位置：`store/db.py`、`pipeline/write.py`。
  - 反例：就地 UPDATE 已发布报告 → 历史轮次与当时的卡片对不上。
# 6. 界面

## 6.1 报告卡（R-071 – R-083）

- **R-071 卡片张数与折叠等高**
  - 规则：每轮卡片张数 = `per_round`（默认 5），在「本轮总览」里横向一排。**不要求每张高度对齐**，但折叠状态下**所有卡片等高**：`结论` 固定高度（10 行，`--verdict-lines`），放不下显示「…」、hover 出全文（`.verdict-pop`）。
  - 位置：`web/src/features/report/`、`web/src/styles/proto.css`。
  - 反例：默认折叠下高度不一 → 一眼扫读的排版就散了（约 12 张卡片要能同时放下，允许超出一部分）。
- **R-072 一键开合**
  - 规则：「本轮总览」左侧一个按钮，一次开合全部卡片的两段折叠内容。做法是**不接管 `details`** —— 换 key ＋ 给新的默认值（`key={quality-${tick}}` ＋ `open={openAll}`），让五张卡的两段 `<details>` 重挂一遍，各卡自己的开合用不着变成页面状态。
  - 位置：`web/src/features/overview/`、`features/report/`。
  - 反例：「全部展开」时结论要显示完整、不再需要 hover；「全部收起」时回到固定高度、hover 看全文。
- **R-073 卡片宽度不随展开变化**
  - 规则：栅格 `.grid` 是 `repeat(auto-fill,minmax(345px,1fr))`，展开一张卡不会把五张一起挤窄或换列（列数只由容器宽度决定）。
  - 位置：`web/src/styles/proto.css`。
  - 反例：展开链上数据 / 链下调研就让卡片宽度变化。
- **R-074 前三名用金银铜背景区分**
  - 规则：`--podium-1/2/3` 与 `--podium-1/2/3-bg`。
  - 位置：`tokens.css`、`proto.css`。
  - 反例：只给名次数字变色 → 横排里看不出哪三张是前三。
- **R-075 「链上质量」与「链下调研」两块都默认折叠**
  - 规则：同一套 `<details>` 语法，各卡自己开合（一键开合见 R-072）。
  - 位置：`features/report/`。
  - 反例：折叠态把卡片撑高 → 「结论在前、数据一键展开」的重排白做。
- **R-076 卡片头部与身份行**
  - 规则：头部是 `名次 · 标的 · 新进/续榜`（左）＋ `综合分`（右）；下面一条评分条把 `链上质量` / `链下调研` 两个分项画成两段（**不带 ×55% 这类占比文案**，两块各自靠两端对齐）；再一行是 CA（截断）＋ **复制 icon**（点击后显示「已复制」，icon 与 CA 同字号）＋ 池内排名（`流动性 · 成交 · 持有人`）；行情行是 6d 折线 ＋ 市值。
  - 位置：`api/app/report/cards.py`、`web/src/components/CopyCa.tsx`、`features/report/`。
  - 反例：给两项分挂「说明性文字」→ 换行（卡片窄）。
- **R-077 貔貅是判定不是数字**
  - 规则：读 GoPlus 的 `is_honeypot`，输出「是 / 否 / —」三态带**色框**（`cards.honeypot_tone()` → `MetricOut.tone`；是＝`--danger-border`、否＝`--ok-border`、—＝`--warn-border`，直角、复用安全区 pill 的配方）。CSS 必须写成 `.kv .kv-v.hb-*` 才压得住缺值行的灰字（两层的写法会静默失效）。
  - 位置：`api/app/report/cards.py`、`web/src/styles/proto.css`。
  - 反例：**陷阱占比由「陷阱」chip 承担**（读 `honeypot_pct`，也就是 GMGN 的 `top_entrapment_trader_percentage`）—— 两者不是一个数，不要把同一个值挂在两个名字下。
- **R-078 LP 只分三态**
  - 规则：已锁定 / 未锁定 / 未判定。显示「已锁」时**不跟括号解释**（标签本身就是答案）；未锁与未判定保留括号里的原因，多个原因用「·」分隔（理由自己带括号时会套娃）。
  - 位置：`report/cards.py`。
  - 反例：已锁还写「（LP 在合约里）」→ 多一行解释，读者还是要读一遍才知道结论。
  - 关联：R-019。
- **R-079 「链上质量」表不印这些**
  - 规则：**不印**开盘、总供应（快照里仍存 `open_at` / `total_supply`）；不印洗量（`wash_pct` 已从 `TokenMetrics` / `METRIC_KEYS` 删除）；不印机器人 / 新钱包 / 私人金库 / 优质持有人 / 信号数（GMGN 在 Arc 上对这些一律回 0，一整行 0.0% 是噪声；采集与快照保留，换链若真有数再显示）。**筹码分布与池子清单也从卡片上撤了**（payload 里仍带 `safety.pools` / `safety.holders`，供归档与 API）。
  - 位置：`report/cards.py`。
  - 反例：把 0.0% 的一整行印上去 → 有价值的那几行被噪声淹没。
- **R-080 退出 chip 要说清是哪一条**
  - 规则：不是「跌出硬过滤或排名」这种含糊话，detail 五选一（硬过滤的具体项 / 排名多少被挤出 / …）。
  - 位置：`pipeline/decide.py` 的 `_changes` —— 上下文（`items`、`pool_rank`、`per_round`、生效的 `prefilter` / `safety`）在 `_round` 里本来就够得着，递进去即可。
  - 反例：只说「退出」→ 读者要自己去猜哪条规则把它挤掉了。
- **R-081 「一轮那一条」**
  - 规则：`.bhead` ＝ 日期步进 ＋ `扫描 → 淘汰 → 候选 → 入选` ＋ `已判定 / 未判定` ＋ 本期变化 chips（新进 / 退出），**一行到底**，高度压低、区域分割线不与边框相连；`.bhead{margin:0 16px}` 与卡片同列。退出项显示**币的名称**，不是 CA。
  - 位置：`features/overview/`、`web/src/styles/proto.css`。
  - 反例：三块分割线画到边框上 → 整行看起来像被切成三个盒子。
- **R-082 日期控件属于报告页**
  - 规则：它在报告页里与首张卡片对齐，外侧矩形控件与日期控件之间留左边距。
  - 位置：`features/report/`。
  - 反例：把日期步进放到全局顶栏 → 设定页上也出现一个改了没用的控件。
- **R-083 展示层的链接一律「说明 + 可点击词」**
  - 规则：见 R-039；点开跳 `target="_blank"`。
  - 位置：`web/src/components/Rich.tsx`。
  - 反例：直接把 URL 印在卡片上 → 又长又不可点。
## 6.2 设定页（R-084 – R-090）

- **R-084 每条链一套设定**
  - 规则：`/settings` 保存写进 `settings_versions`（不可变），之后以表为准；出厂默认在 `config/chains.yaml`。**链与链之间不共享设定**（后面接 BSC 等链时，过滤条件与阈值各写各的）。
  - 位置：`api/app/api/v1/settings.py`、`api/app/api/v1/forms.py`、`web/src/features/settings/`。
  - 反例：做一份「全局设定」→ 一条链调松，另一条链跟着变。
- **R-085 解释折进「说明」icon**
  - 规则：`GroupOut.detail` / `SettingsFormOut.note_detail`。判据是「这句话是这一轮的**读数**，还是关于字段本身的规定」—— 读数留屏幕（漏斗计数、违规提示、本轮入选均值），规定进 icon。口径文案留在 `forms.py`，**前端不认识任何一条文案**（C-003）。
  - 位置：`api/app/api/v1/forms.py`、`web/src/components/Icon.tsx`。
  - 反例：标题下面挂一行解释 → 一屏里全是灰字，读者找不到真正的读数。
- **R-086 块内等高（含分两栏）**
  - 规则：`.blk-cols{align-items:stretch}` 花掉栅格已经算好的行高，一行里所有框下边齐平；空白从框外搬进框内，所以组尾读数（`.pset` / `.note-inline`）用 `margin-top:auto` 顶到框底。每块里最高的那一组**分两栏**：切片不是对半（`rows.slice(0, cut)`），切点落在语义边界上，阅读顺序「左栏读完再读右栏」；判据是「没有东西必须折行」而不是「这行短」。宽度不够时挤压其他块的列宽，不换行。
  - 位置：`web/src/styles/proto.css`、`features/settings/`。
  - 反例：框高不齐 → 一行里参差错落（读者会以为排版坏了）。
- **R-087 读数靠右**
  - 规则：没有淘汰格的分组也是「label 左、读数右」（`margin-left:auto`），不要留成例外。`推送与依赖` 的读数**允许折行** —— 它是最窄的一栏，值本身就是答案，长值绝不许把轨道撑出横向滚动条。
  - 位置：`web/src/styles/proto.css`。
  - 反例：用 `GMGN · Rad…` 这种截断当读数 —— 那不是读数。
- **R-088 复选框是直角硬线条**
  - 规则：原生控件内部不吃 `border-radius:0`（圆角蓝块是 Chrome 画的 → 自绘），**与标题同一行、同一列起点**（`.prow.wide .field{flex:1 0 auto;max-width:100%}`，行本来就是 `flex-wrap:wrap`，缩不缩由行自己判断）；对勾是内联 SVG data-URL，浅色单独一条 `background-image`（自定义属性进不去 data-URL）。
  - 位置：`web/src/styles/proto.css`。
  - 反例：留原生的圆角蓝勾 → 与全站直角冲突。
- **R-089 区间字段**
  - 规则：`市值` 与 `持有人` 都是区间；上限 `holders_max` 与下限共用「持有人」这一条规则（`_ceiling(...)`），所以淘汰计数、漏斗行、设定页那一行都不用改。**缺键 = 无上限**（老快照没有这个键时不能开始砍币），默认留空见 Q-004。空格子必须能存回去：`config.coerce_like` ＋ `_infer`（空 → `None`，`2000` → `int`；已存 2000 再清空，`None` 要原样活过类型转换）。
  - 位置：`core/filters.py`、`api/app/config.py`、`forms.py`。
  - 反例：把上限写成 `0` → 变成「一个都不留」。
- **R-090 链下调研块**
  - 规则：五项指标（产品完成度 / 项目实质与亮点 / 团队与 dev 可信度 / 叙事与官方关系 / 社区活跃度）各 0–5，等权合成 `offchain_weight`（默认 45%）。这五行是**只读**的 —— 它们是 `forms.py` 里 `OFFCHAIN_LABELS` 的口径说明，前端不认识任何一条文案。
  - 位置：`api/app/api/v1/forms.py`、`core/scoring.py`。
  - 反例：把五项也做成可输入权重 → 等于让「口径」变成「调参」（Q-008）。

## 6.3 侧栏与顶栏（R-091 – R-096）

- **R-091 左栏只渲染 `enabled` 的链**
  - 规则：未接的整行不出现（不是变灰）；链名旁边不挂计数（入选数在「一轮那一条」里）。左栏宽度 **260px**。
  - 位置：`web/src/features/rail/`、`proto.css`。
  - 反例：把没接的链渲染成灰行 → 每次都提醒读者「这里缺东西」。
- **R-092 总览那一行**
  - 规则：「读数（扫描 / 淘汰 / 候选 / 入选）」放在本期变化**上方**；「本轮总览」在**导出左侧**。
  - 位置：`features/overview/`、`features/report/`。
  - 反例：三个区域的分割线画到边框上（R-081）。
- **R-093 链自己的标记**
  - 规则：`Rail.tsx` 的 `chainIdMark()` 按 id 分发；Arc 的 artwork 是单路径**填充**图标（线重画会把记号画没），用 `currentColor` ＋ prop 尺寸（侧栏 14，与 12.5px 那一档一致），深浅主题自动适配。
  - 位置：`web/src/components/ArcMark.tsx`、`features/rail/Rail.tsx`。
  - 反例：把标记代码分叉逻辑写死在侧栏组件里 → 加链时要改侧栏。
- **R-094 顶栏右端 = 当前页面的动作**
  - 规则：同一时刻只存在一份：报告页 `本轮总览 › ｜ 导出 ｜ 推送 TG`；设定页 `保存并生效 ｜ 立即运行`（在「本轮总览 / 导出 / 推送」的位置）。不可用的动作**灰显不可点**，`title` 写明原因。
  - 位置：`web/src/App.tsx`、`features/`。
  - 反例：把两页的动作同时列出来 → 读者分不清哪个属于当前页。
- **R-095 中止按钮**
  - 规则：两次按压（第一下变 `确认中止`，4 秒后自解除 —— 一次误触不能留下「下一轮上来第二下就停」的按钮），请求在飞时换成 `正在中止`（转圈、不可点），图标 `Square`，颜色 `currentColor`。
  - 位置：`web/src/features/run/RunPanel.tsx`、`App.tsx`。
  - 反例：一次按压就中止 → 误触代价太大；图标用 `Pause` 与「正在中止」语义不符（R-123）。
- **R-096 主题切换在侧栏下方**
  - 规则：黑 / 白两态，见 R-106。
  - 位置：`web/src/theme.ts`、`features/rail/`。
  - 反例：放到顶栏 → 与「当前页面的动作」抢位置（R-094）。
## 6.4 字体、字号、字重、颜色（R-097 – R-102）

- **R-097 字体全站只用 sans**
  - 规则：`--mono` 与 `--serif` 都是 `var(--sans)`（`tokens.css`），所以任何地方再写 `var(--mono)` 都是 no-op —— 想让 CA 恢复等宽，改这一行即可。等宽原本承担的对齐交给 `body{font-variant-numeric:tabular-nums}`（等宽是为了让数字列对齐，不是为了换字族）。IBM Plex Mono 的 `@font-face` 与 woff2 已全部删掉。
  - 位置：`web/src/styles/tokens.css`。
  - 反例：混排（正文 sans、数字 mono）→ 一屏里出现两套字形。
- **R-098 字号只有 7 档**
  - 规则：`9.5 / 10.5 / 11.5 / 12.5 / 15 / 19 / 21`。按钮与表单控件 11.5（`.btn` / `.ghost` / `.step` / `.panel-h .x` / `.ok-btn` / `.frow input` / `.prow input` / `.prow .sel` 同档）；图标按钮 9.5（`.ca-copy` / `.note-help-ico`）；表格正文 10.5（表头与脚注 9.5）；块标题三层 —— 面板 / 大块 12.5、子组 11.5、卡片内块 10.5，行标签 11.5，`参数设置` 15。
  - 位置：`tokens.css`、`proto.css`。
  - 反例：随手写 `13px` → 字号表之外出现一个值，之后没人敢改。
- **R-099 字重只用 400 / 500 / 600**
  - 规则：`b` / `strong` / `h1`–`h6` 一律 600。
  - 位置：`tokens.css`。
  - 反例：CSS 没写就落回浏览器默认 700，会出现「同为小标题、一个字更黑」。
- **R-100 颜色只有一条来源 `tokens.css`**
  - 规则：页面里不写字面 hex。文字色用 `--fg` / `--fg-2` / `--muted` / `--muted-2`；`--accent` 只做填充与边框，**文字用 `--accent-fg`**；状态文字用 `--ok-fg` / `--warn-fg` / `--danger-fg`（`--ok` / `--warn` / `--danger` 给填充与边框）。
  - 位置：`web/src/styles/tokens.css`（先改这里，再动 proto / app）。
  - 反例：在组件里写 `#c00` → 浅色主题下变成看不清的深红。
- **R-101 直角是全局的**
  - 规则：`*{border-radius:0!important}`，新控件不要再引入圆角。
  - 位置：`tokens.css`。
  - 反例：原生控件内部不吃 `border-radius`（R-088）。
- **R-102 浮层用卡片那一套色**
  - 规则：`.verdict-pop` / `.note-help-pop` 底 `--surface-warm`、字 `--fg`、边 `--border`，比所在那层底浅一档；hover 里文字仍是默认文字色（不要变蓝）、**横向永不出滚动条**（`white-space:normal`，长值折行）。
  - 位置：`proto.css`。
  - 反例：hover 弹层里出现横向滚动条 → 一行的长值把弹层撑破。

## 6.5 图标与 Logo（R-103 – R-105）

- **R-103 图标只有一套**
  - 规则：`lucide-react`（只进用到的）＋ 一个组件 `web/src/components/Icon.tsx` ＋ 一条规则 `.ico{display:block;stroke-width:1.5;stroke-linecap:square;stroke-linejoin:miter}`。**几何在样式表里否掉 lucide 的默认**（24 格、描边 2、圆角端点与连接）—— `stroke-*` 是继承属性、样式表声明赢过表现属性，所以后加的图标不可能忘记，调用点也不用重复传 prop。
  - 位置：`web/src/components/Icon.tsx`、`tokens.css`。
  - 反例：直接在组件里用 lucide 原组件 → 混进圆角端点与 2px 描边。
- **R-104 尺寸三档，颜色一律 `currentColor`**
  - 规则：不新开色值，自动适配主题；**icon 大小跟随旁边的文字大小**。
  - 位置：`Icon.tsx`、`proto.css`。
  - 反例：图标比旁边文字大一圈 → 行高被撑开。
- **R-105 Logo**
  - 规则：`web/src/components/Mark.tsx` 是雷达扫掠（方框 ＋ 一道扫掠 ＋ 一个回波点），用 `createLucideIcon` 声明以便走 `Icon`（`.ico` 那条规则与 `currentColor` 自动套上）。侧栏里它与 `CRYPTO HUNTER` 同行、大小适配（**下方不留横线**）；标签图标只用同一个记号、不铺底色、跟着主题走。
  - 位置：`web/src/components/Mark.tsx`、`features/rail/`。
  - 反例：给 Logo 单独铺一块底色 → 浅色主题下那块底色比周围还深。

## 6.6 主题（深 / 浅两态）（R-106 – R-112）

- **R-106 两套 token ＋ 一个持久化键**
  - 规则：两套 token 都在 `tokens.css`：`:root` 深色、`[data-theme="light"]` 浅色。持久化只写 `ch.theme` 一个键（`web/src/theme.ts`）；`index.html` 里一段内联脚本抢在首屏前设好 `data-theme`（否则深色用户每次刷新先看到一帧白），脚本用 `try/catch` 包住 `localStorage`。
  - 位置：`web/src/styles/tokens.css`、`web/src/theme.ts`、`web/index.html`。
  - 反例：只在 React 里设主题 → 首屏闪一帧白。
- **R-107 浅色不是反色**
  - 规则：链接 / 状态文字色、四种 `--*-bg` 淡底、金银铜、`--shadow` 都要按浅底重取；状态文字色要按**它能落到的每一个底**算对比度（`--surface-warm`）。要求：字号 ≥18px 按 3:1，其余 4.5:1。
  - 位置：`tokens.css`。
  - 反例：直接 `filter: invert()` → 图表与照片全反。
- **R-108 浅色下 `--bg` / `--surface` 的分工**
  - 规则：浅色下 `--bg` 是页面底（白）、`--surface` 是面板底；**右区与左栏整体互换** —— 左栏（与菜单、分组与行、输入框与下拉、运行面板及其头脚、复制 CA 芯片）跟 `--surface`，**报告卡片也重新提出来跟 `--surface`**（`--surface` 在 `proto.css` 里有几十处引用，卡片只是其中之一），没有一处写死颜色。
  - 位置：`tokens.css`、`proto.css`。
  - 反例：浅色下右侧一片灰、左栏反而白 → 视觉重心跑到侧栏上。
- **R-109 运行面板的边框用 `--border`**
  - 规则：与它正下方的分组同一个 token，不用正文墨色。
  - 位置：`proto.css`。
  - 反例：用 `--fg` 画边框 → 浅色下就是一只纯黑盒子（它是全屏唯一用正文墨色画边框的地方）。
- **R-110 运行面板跑完不收起**
  - 规则：七行步骤一直可见（**运行完不自动收起**，只有 `收起` 能关；失败 / 已中止 / 跑完三种终态一视同仁），每行右列是那一阶段自己的实测墙钟（`RunPanel` 的 `took()`：把 `batch.phase_timings` 里属于该行的 phase 相加，`STAGE_PHASES` 定义了归类，例如「生成报告」= `draft` ＋ `write`、「硬过滤」= `prefilter` ＋ `topup`）。**正在运行与运行结束的边框与主题一致**（R-109）。
  - 位置：`web/src/features/run/RunPanel.tsx`。
  - 反例：早期版本跑完会自动 `setRunPanelOpen(false)` 并靠 `.runpanel.done` 把步骤列表 `display:none`，等于把「这一轮时间花在哪」写出来又立刻藏掉。
- **R-111 表单控件不吃继承**
  - 规则：`tokens.css` 里 `button,input,select,textarea{font-family:inherit}` 兜底（UA 样式会给控件直接设字体族，明确写了族名的规则靠优先级照样赢）。
  - 位置：`tokens.css`。
  - 反例：不兜底 → 输入框里冒出一个不是全站字族的字体。
- **R-112 已知残留：深色状态边框对比度不达标**
  - 规则：深色主题的状态边框（貔貅三态框 / pill / chip）是 2.29 / 2.37 / 1.63:1，低于非文字元素 3:1 的建议线，**没有为了对齐浅色去改它**（见 Q-006）。
  - 位置：`tokens.css` 的 `--ok-border` / `--warn-border` / `--danger-border`。
  - 反例：悄悄改深色 token 去凑 3:1 → 三块「有值」的底变得抢眼。
# 7. 运行与验证

## 7.1 读路径（R-113 – R-117）

- **R-113 硬地板与三个常量**
  - 规则：单次上链请求 2.2–2.5s，与查什么无关（`eth_getCode` 与 `eth_call` 同量级）；并发几乎免费（1 / 4 / 8 / 12 并发 = 2.16 / 2.37 / 2.88 / 3.56s）；批量确实省 —— 同样两次请求，单发 4.61s、打包成一个 JSON-RPC 批量 3.54s。**一条链只有一个 RPC 端点，没有可换的备份。**
  - 位置：`sources/onchain.py`。
  - 反例：指望靠加并发把链上段从 30s 压到 5s → 硬地板在，做不到。
- **R-114 提速只做「把本该同时问的一次问完」**
  - 规则：`sources/onchain.py` 的 `rpc_batch(chain, calls)`（回包按 id 映射回顺序；非 list 回包 = 整批重试，重试耗尽逐项 `None`；用 `get` 同一套 `tally` 记账）、`nft_balances(chain, managers, who)`、`contracts_of(chain, addresses)`（批量 `eth_getCode` 判别 EOA）；`_pool_logs` 窗口并发（`LOG_WINDOW_WORKERS = 4`，窗口互不相交、`pmap` 保序）；`pipeline/decide.py` 的 `ENRICH_WORKERS = 8`（R-023）。
  - 位置：`api/app/sources/onchain.py`、`pipeline/decide.py`。
  - 反例：并发但不保序 → 回包错位（所以回包按 id 映射）。
- **R-115 效果要如实报，别当战果**
  - 规则：RPC 请求数 40 → 30 / 轮（这是唯一干净可比的数字，≈ −25%），`gate` 36–44s → 29s。等待时间基本没改善（次数降了 25%，但单次延迟轮间能从 2.2s 飘到 4s，再叠 retry 就吃回去了）。
  - 位置：`pipeline/progress.py`（阶段耗时日志）。
  - 反例：把 −25% 的请求数说成「快了三成」。
- **R-116 `eth_getLogs` 的调用次数轮间不可比**
  - 规则：`_logs_in` 在节点拒绝窗口宽度时对半递归，拒几个窗口由节点侧决定。**结论看整轮的 `ok=` 总数，别用单次 A/B 的调用次数下结论**（R-067）。
  - 位置：`sources/onchain.py`。
  - 反例：拿两轮的 `eth_getLogs` 次数做对比 → 波动全来自节点。
- **R-117 还没解决：30 次 / 轮里没有可去的重复调用**
  - 规则：探针证明一轮内没有重复读同一个 CA；要再压只能减少「哪些读是必需的」（例如把 `lock_facts` 的部分读挪到入选后的 5 个标的上，而不是 20 个候选都读）。
  - 位置：`sources/onchain.py`、`pipeline/decide.py`。
  - 反例：**未擅自改，等拍板**（Q-007）。

## 7.2 中止一轮（R-118 – R-126）

- **R-118 口径：只在内存里立一面旗**
  - 规则：`POST /runs/{id}/cancel` 只在**进程内存里立一面旗**（`api/app/cancel.py`）就立刻返回 —— 一轮跑在 `schedule.py` 的单线程 worker 上，HTTP 处理器够不到那个线程，所以不进数据库。轮次自己在下一个检查点读旗、抛 `RoundCancelled`。旗按 run id 存、轮次结束清掉，所以下一轮绝不会继承上一轮的停止请求。
  - 位置：`api/app/cancel.py`、`pipeline/schedule.py`、`api/app/api/v1/runs.py`。
  - 反例：把停止请求写进数据库 → 重启后上一轮的请求还在。
- **R-119 一轮没有回滚可做，这才是它能这么简单的原因**
  - 规则：落库只有最后一笔事务（`store_round` ＋ `store_reports`），停在哪就是停在哪 —— 已经问出去的源、已经判过的标的留在日志与 `run_summary` 里，**报告一张也不写**。
  - 位置：`pipeline/write.py`。
  - 反例：搞一套「回滚」→ 复杂度全花在不需要的地方。
- **R-120 检查点只用轮次本来就有的边界**
  - 规则（没有为中止新增计时器或线程）：`collect.py` 两个源之间；`decide.py` 每个阶段边界与 `_gate` 的批次循环头与**每个标的**、picks 那次兜底 enrich 之前；`research.py` 每个币的词条之间（一个币要么有词条要么没有，停在两个币之间不会留下半条）；`llm/client.py` 一次调用的前 / 后与**读响应体的每一块之间**。
  - 位置：`pipeline/collect.py`、`pipeline/decide.py`、`report/research.py`、`llm/client.py`。
  - 反例：加一个每秒轮询的计时器 → 多一个线程，还让每轮都变慢。
- **R-121 在途的模型答案可以被放弃，但不能退化成 `LLMUnavailable`**
  - 规则：`client.stream` 按块查旗，命中就关连接抛 `RoundCancelled`；关键是 `complete()` 里单开 `except cancel.RoundCancelled: raise`。
  - 位置：`api/app/llm/client.py`。
  - 反例：退化成 `LLMUnavailable` → 一轮会用「没写词条」的样子把报告发出去。
- **R-122 排队中的那一轮是唯一的例外**
  - 规则：没人开始跑就读不到旗，而 `schedule.recover` 会把停在 `pending` 的轮次再交给 worker 一次 —— 所以这一种由路由直接落库（`run.stop_run`），并且 `run_chain` 对已经是终态的轮次直接返回。
  - 位置：`pipeline/schedule.py`、`pipeline/run.py`、`api/app/api/v1/runs.py`。
  - 反例：只立旗 → 队里那一轮永远不会停。
- **R-123 响应时间是秒到二十秒的量级**
  - 规则：拉取段按中止是**秒级**；安全判定中中止约 **20 秒**（`enrich_limit` 默认 20 个标的 × 8 并发；最慢的一档，因为「一批」是轮次原有的最小单位，再往下拆只会让每一轮都变慢）；写作段在下一次响应块到达时（单位是秒）。
  - 位置：`pipeline/decide.py`、`report/research.py`。
  - 反例：承诺「点一下立刻停」。
- **R-124 故意留下的窗口**
  - 规则：写库之后不再有检查点 —— 停止请求若恰好在最后一笔事务提交之后到达，这一轮照常发布（已经发出去的报告不该被改写成「已中止」）。
  - 位置：`pipeline/write.py`。
  - 反例：在事务里插检查点 → 报告半写半不写。
- **R-125 前端：中止后与跑完同一处理**
  - 规则：`已中止` 的面板标题是 `已中止 · 本轮`，步骤列表照常显示（已结束的阶段有耗时，没走到的画 `—`）；按 `收起` **或刷新**，报告页都退回**最近一轮有卡的批次**（`App.tsx`：最近一轮里第一个 `selected > 0` 或还没停的批次；开页只答一次，之后位置归翻页器）；没有卡片结果时不留白页。
  - 位置：`web/src/features/run/RunPanel.tsx`、`web/src/App.tsx`。
  - 反例：刷新后又跳回那个没有结果的批次。
- **R-126 新状态 `cancelled`**
  - 规则：不是故障就不该记成 `failed`，`states.TERMINAL` 含它。
  - 位置：`api/app/pipeline/states.py`，测试 `tests/test_cancel.py` ＋ `tests/test_llm.py` ＋ `tests/test_states.py`。
  - 反例：中止记成 `failed` → 报表里「失败率」虚高。
## 7.3 状态机（R-127 – R-129）

- **R-127 状态链**
  - 规则：`pending → collected → scored → selected → written → researched`；任何在跑的状态都可以跳到 `cancelled` / `failed`。
  - 位置：`api/app/pipeline/states.py`。
  - 反例：给 `written` 之后再排一个异步阶段 → 报告已发布却还会被改写。
- **R-128 一轮结束了 = 到了终态**
  - 规则：`TERMINAL = {researched, cancelled, failed}` 决定 `finished_at`，页面停表。`researched` / `cancelled` / `failed` 是终态；`written` 之后一步就是终态（`_finish()` 推 `written → researched`，没有推送出口要等，所以 `researched` 既是「文字已落库」也是「本轮结束」）。
  - 位置：`states.py`、`pipeline/run.py`、`pipeline/progress.py`。
  - 反例：把 `written` 也当终态 → 推送那一步没走完就停表。
- **R-129 展示口径的锚点**
  - 规则：文字落库一次（`reports.payload_json`），落库之后不再改写；落库那笔事务之前，任何阶段失败都可以重跑。卡住的一轮靠**状态机上的重试与补发**恢复，不另起看门狗脚本。
  - 位置：`pipeline/write.py`、`pipeline/schedule.py`。
  - 反例：另写一个 watchdog 脚本扫库改状态 → 两个写者抢同一行。

## 7.4 工作方式（和这个用户合作）（R-130 – R-135）

- **R-130 先讨论再动手**
  - 规则：他说「我们先讨论」时，只出判断和方案，**不动代码**。没让改的别改。
  - 反例：把「先讨论」当成「讨论完直接实现」。
- **R-131 一次只动该动的**
  - 规则：不顺手重构、不修无关的 bug（可以在汇报里提一句，由他决定）。
  - 反例：顺手把「看起来多余」的一段删掉 → 它可能是某个口径的兜底。
- **R-132 不 `git commit`、不建分支**
  - 规则：除非他明确要求。
  - 反例：自动提交一版「阶段性成果」。
- **R-133 改动贴现有风格**
  - 规则：直角（`border-radius:0 !important` 全局已定）、颜色只从 `tokens.css` 取、文案口径跟 `settings/form` 走（C-003）。
  - 反例：新写一个带圆角的卡片。
- **R-134 汇报短，先结论后数据**
  - 规则：先说结论，再给实测数字与 `文件:行`；UI 改动附截图。他对 UI 细节极其挑剔，一处一处改、改完复述「改了什么 ＋ 实测多少」。
  - 反例：把改动过程按时间顺序写成流水账。
- **R-135 用中文沟通**
  - 规则：代码注释与标识符按现有习惯（注释英文、文案中文）。
  - 反例：报告正文里混英文句子。

## 7.5 还没接的部分（别当成 bug）（R-136 – R-140）

- **R-136 TG 推送**
  - 规则：`api/app/out/` 是占位模块（`out.configured()` 返回 `False`）。一轮跑到那步写「未配置（跳过推送，报告已落库）」并正常收尾，**不阻塞**；顶栏「推送 TG」与「导出」两个按钮都带 `disabled`，按不动 —— 导出**已经把下载接上了**（`web/src/App.tsx` 的 `onExport`），只是按钮还锁着；推送完全没接。
  - 位置：`api/app/out/`、`web/src/App.tsx`。
  - 反例：把「未配置」当成失败 → 整轮卡在「正在运行」。
- **R-137 一键重算**
  - 规则：`POST /settings/recompute` 返回 501：一轮的 universe 没落库。
  - 位置：`api/app/api/v1/settings.py`。
  - 反例：前端以为它可用 → 点了没反应。
- **R-138 搜索没配 cookie**
  - 规则：`config/search.env` 空着时 X 搜索整层静默失效（`discover` 返回空），卡片只用本轮登记的链接，**不阻塞流程**（R-031）。
  - 位置：`api/app/search/`。
  - 反例：把「没配 cookie」当报错 → 没有链接的币也进不了报告。
- **R-139 链下调研没回来的标的**
  - 规则：本轮不参与排名（不是 0 分），等它落地后下一轮才有分数（R-028 / R-047）。
  - 位置：`core/scoring.py`。
  - 反例：给它一个 0 分 → 被当成「差」。
- **R-140 无上限的规则**
  - 规则：`holders_max` 默认 `null`（见 Q-004）。
  - 位置：`config/chains.yaml`、`core/filters.py`。
  - 反例：随手填一个上限数字。
## 7.6 进度与完成态（R-141 – R-144）

- **R-141 进度百分比按阶段额度填**
  - 规则：一轮的五个阶段不是等长的（拉源约两分钟、补齐链上数据是一次逐币的等待、安全判定占四分之三、打分是算术、写作是模型在答），按阶段均分会先躺二十分钟再跳一格。所以每个阶段占一段额度，段内按**实际完成的工作量**填充：

| 阶段 | 额度 | 段内单位 |
| --- | --- | --- |
| `pull` | 0–8 | 每个扫描源拉完一次 |
| `topup` | 8–14 | 每补一个候选的链上数据 |
| `gate` | 14–82 | 每判完一个候选 |
| `score` | 82–86 | 一次 |
| `draft` | 86–100 | 每调研并写完一个币的词条 |

  - 位置：`api/app/pipeline/progress.py`。
  - 反例：额度是按实测墙钟占比定的 —— 改阶段就得回来改这张表。
- **R-142 进度只增不减**
  - 规则：`_set_progress` 落库用 `MAX(progress, ?)` —— 段内额度是估的，回跳比走得慢更伤信任。`percent` 单调，`progress_label` 仍如实跟着阶段变。`runs.progress` / `runs.progress_label` 存最新值，前端 `RunPanel` 顶部显示 `NN% · 阶段名`。
  - 位置：`pipeline/progress.py`、`store/schema.sql`。
  - 反例：阶段回退让进度条倒着走（测试 `test_the_bar_never_walks_backwards`）。
- **R-143 `sharpen` 与 `write` 不占额度**
  - 规则：只记日志 —— 为一两秒的阶段切一条进度带只会让另外四段变窄（它们分别停在 86% 与写几百行库）。阶段耗时每轮打一行 `round <id> phase timings: ...` 进日志；**数据库只留结论、不留耗时，日志是唯一能复核这些额度的地方**。
  - 位置：`pipeline/progress.py`、`pipeline/run.py`。
  - 反例：给 `write` 也切一段 → 卡在 99% 等落库。
- **R-144 推送出口未配置 = 不阻塞完成**
  - 规则：推送是落库之后的可选动作（`app/out`），`configured()` 为 `False` 时只在推送那一步提示「未配置」，轮次照常 `written → researched`。将来接出口改的是 `configured()`，流程不动。
  - 位置：`api/app/out/`、`pipeline/run.py`。
  - 反例：把「未配置」当失败 → 整轮停在「正在运行」（R-136 / R-134）。

# 8. 待决（都没擅自改）

**想动这里面的任何一条，先问用户。**

- **Q-001 硬过滤的松紧**：现在是 `market_cap_min_usd` = $10,000 ～ `market_cap_max_usd` = $200,000、`liquidity_min_usd` = $5,000、`holders_min` = 100（参考实现的下限是 0）。两头一起收窄，是为了把「微市值 ＋ 零数据」和「已经涨上去的老盘」同时挡在门外；代价是候选里会少掉两头的新面孔，松紧随时可在设定页改。
- **Q-002 同一行快照里 `market_cap_usd` 与 `price_usd × 供应` 对不上**：两个端点各自缓存、TTL 不同步，同一行拼了两个时刻的数。它对「距高回撤」的影响限于读数本身：①源高点与②库记录高点都以 `market_cap_usd` 当 `now`，刻度不自洽会让跌幅偏深或偏浅，也让设定页的「未判定」数偏高（参与判定的仍旧是三个读数、任一枚举跌够就淘汰，R-013）。三条路 —— 用 `price_usd` 当序列 / 把市值那列的 TTL 与价格对齐 / 先修自洽再谈阈值。
- **Q-003 `num` ＋ `enabled_key` 行的 `开启` 标签被折成两行**：`.chk` 被 `flex:0 1 auto` 压扁到 29×31px，与复选框长什么样无关。三条路 —— `.chk{white-space:nowrap}` / 把 `开启` 缩成 `开` / 这一列不再可收缩。
- **Q-004 持有人的上限默认留空**（＝ 不设上限）：给它一个数就等于凭空开始砍币，而 Arc 上「市值 1–20 万的币持有人能到多少」没有实测过。
- **Q-005 进程死在 `collected` / `scored` 中途时，那一轮和「正在跑」长得一样**：目前只恢复 `pending` 的轮。要修得给轮次加心跳时间戳。
- **Q-006 深色主题的状态边框对比度不达标**（2.29 / 2.37 / 1.63:1，R-112）：要达标就把 `--ok-border` / `--warn-border` / `--danger-border` 各提一档，一行一个。
- **Q-007 链上读路径要再压，只能减少「哪些读是必需的」**：30 次 / 轮里没有可去的重复调用（R-117）。
- **Q-008 链下调研五项的边界**：等权还是加权、第 2 项（项目实质与亮点）与第 4 项（叙事与官方关系）的界线、粗筛条件、`per_round` 的取值、判定规则细则、链下调研块的其他参数（R-090）。

# 附录 A：为什么这么定（P-###）

每条规则都是被某个具体的错纠出来的。这里只记「曾经怎么错」，**现行口径看对应的 R-**。

| 编号 | 曾经的错 | 现在写在哪 |
| --- | --- | --- |
| P-001 | 淘汰数按各行独立计数，加起来不等于 `dropped` | R-012 |
| P-002 | 距高回撤要求三个读数一致 → 169 个已跌够的币一个没淘汰，ARCAT 漏进报告 | R-013 ｜ M-004 |
| P-003 | 库记录高点从不回写 → 那一半永远只是下界，「只报未判定、从不动手」 | R-013 |
| P-004 | gate 曾买池面读数 → 被 GeckoTerminal 打限流，gate 54s 涨到 242s | R-013 ｜ R-024 ｜ M-005 |
| P-005 | 硬过滤曾是一遍 → 未知等于放行，上千个发射台自报的行直接进池 | R-014 |
| P-006 | 补齐曾用全量 `fetch_token`（四个端点）→ 29 行 53.1s | R-014 ｜ M-003 |
| P-007 | 短名单排序键曾用成交额 / `min(流动性, 成交额)` → 刷量的浅池把深池挤掉 | R-011 |
| P-008 | 词条曾跨轮复用（2 天过期 / 市值波动 40% / 6h 跳过窗口 / 人工确认） | R-030 |
| P-009 | 「一次调用写完所有条目」已废弃 —— 必然撞天花板 | R-035 |
| P-010 | 词条看不到安全块 → 永远写「安全块三项全部未判定」 | R-044 |
| P-011 | 卡片曾一边显示「LP 已锁」一边在词条里写「三项全部未判定」 | R-038 |
| P-012 | 运行面板跑完自动收起 → 把「这一轮时间花在哪」写出来又立刻藏掉 | R-110 |
| P-013 | 空壳官网静态抓只判出 none → `usearckit.online` 的卡片上挂了第三方站 `myarcade.fun` | R-034 ｜ M-011 |
| P-014 | 池面读数读失败时静默放行（而不是记未判定） | R-013 |
| P-015 | 税率曾用排行榜行 / GoPlus 的占位 0 → 报告里全部税率都是假的 | R-018 ｜ R-054 |
| P-016 | `source_verdict` 的 `dual` 曾写 `and` → 在 Sourcify 收录很差的 Arc 上一票否决 | R-051 |
| P-017 | 曾用单次 A/B 的 `eth_getLogs` 调用次数下结论 → 波动全来自节点 | R-116 |
| P-018 | 推理模型的思考计入 `max_tokens`，额度给小了返回**空消息**（不是报错）→ 客户端把空消息当失败，静默降级成「没有文字」 | R-036 |
| P-019 | `CH_LLM_TIMEOUT` 180s × `CH_LLM_TRIES` 2 → 一次死掉的调用独占 366s（那一轮 draft 421s） | R-036 |
| P-020 | 第三方渲染 `r.jina.ai` 与 `networkidle` 等待策略都试过并放弃 | R-034 ｜ M-012 |

# 附录 B：实测台账（M-###）

正文里的每个数字都在这里有一行；**改口径时把旧数字一并改掉**，别留两套。

| 编号 | 实测 | 现在写在哪 |
| --- | --- | --- |
| M-001 | RadarDEX 的市值口径与 GMGN / GeckoTerminal 不是一回事（拿 `sort=mcap` 当全链排名会错） | R-001 |
| M-002 | 全链口径：GeckoTerminal 分页全量 ≈ 3249 个池/币，RadarDEX 一次拉全量覆盖 ≈ 4145；Arc 上 85% 的币停在发行时的 5000–9000 U 初始 FDV（这就是市值下限压到 1 万的依据） | R-001 ｜ R-005 ｜ R-006 |
| M-003 | 补齐 29 行：全量 `fetch_token` 53.1s、轻量 `fetch_chips` 13.6s；worker 开 8 / 16 / 24 都是 13.6s（已到每主机节流下限，加并发无用） | R-014 |
| M-004 | 169 个已跌够的币一个没淘汰；ARCAT：库内高点 $266K、GMGN 源高点 $5.05M、现市值 $113.8K → 免费半段 −97.7% 直接淘汰 | R-013 |
| M-005 | gate 一次问二十个候选被 GeckoTerminal 打限流：`ok=24 fail=18 retry=78`，gate 54s → 242s | R-013 ｜ R-024 |
| M-006 | RPC 请求数 40 → 30 / 轮（≈ −25%）；`gate` 36–44s → 29s | R-115 |
| M-007 | 单次上链请求 2.2–2.5s；并发 1 / 4 / 8 / 12 = 2.16 / 2.37 / 2.88 / 3.56s；同样两次请求，单发 4.61s vs 打包成 JSON-RPC 批量 3.54s | R-113 |
| M-008 | 79 次 research 调用 p90 = 147s、最长成功 296s | R-036 |
| M-009 | 思考 `none`：94.9s / 5 个词条全过 / 正文 8711 字符；端点默认：340.1s / 只过 4 个 / 正文 6199 字符。旧代码（无重问）`none` 只过 3/5 | R-036 |
| M-010 | 找链接并发：15 个并发跑 20 个标的零 429；20 个并发有 3 个 429 而且更慢 | R-035 |
| M-011 | 空壳页面：`memespad.com` / `www.arcdex.online` / `usearckit.online` 静态只有 8–10 个词、连 CA 都搜不到，渲染后是 377–827 个词且两个带 CA | R-034 |
| M-012 | 浏览器单页 2.4–12.0s；`networkidle` 在 memespad 上是 21.8s | R-034 |
| M-013 | `SHELL_WORDS = 40`；同一批次里次低的官网有 94 个词（两边差得很开） | R-034 |
| M-014 | 补齐一次 45.5s（改轻量后约 14s）；候选池里持有人 < 100 的行 16 → 0 | R-014 |
| M-015 | Tolly 全族自报深度是虚的：5.52 倍（所以 DexScreener 同批必问） | R-014 |
| M-016 | 池子 80 个候选里混着持有人 2 / 3 / 4 的行 | R-014 |
| M-017 | 判完 163 个标的要 20 分钟（不买 GeckoTerminal 池面读数的依据） | R-022 |
| M-018 | GeckoTerminal 逐币端点 2.2s/次节流；GMGN 0.45s/主机节流 | R-022 ｜ R-023 |
| M-019 | 思考关掉后 research 单次 11–17s（300s 只剩兜底作用） | R-036 |
| M-020 | 节点延迟轮间 2.2–4s 波动，`eth_getLogs` 的调用次数也轮间不可比 | R-067 ｜ R-116 |

