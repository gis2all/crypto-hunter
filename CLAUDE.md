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
verify: cd api; uv run python -m pytest -q      # 基线见 2.6
verify_web: cd web; npm run build                # tsc -b 不过就是构建不过
truth:                                           # 每个领域的唯一出处，本文件只写规矩，不抄内容
  - api/app/store/schema.sql      数据模型
  - web/src/styles/               视觉（tokens -> proto -> app，加载顺序即优先级）
  - config/chains/*.yaml          出厂设定（一条链一个文件，文件里的 configs 是一套套设定）
  - api/app/sources/<chain>/SOURCES.md  逐源端点、传输方式、实测结论（链专属源）
ids: 2.3.x 红线 | 6/8/9.x 规则 | 11.x 曾经的错 | 12.x 实测 | 10.x 待决
---

# 1. 这份文档怎么用

## 1.1 图谱优先：先问 code-review-graph

本仓已建 `.code-review-graph/graph.db`。凡定位代码、追调用链、讲架构、评改动影响、评审或动手前理解代码，**先调 `code-review-graph` MCP 工具**，不要默认全文读取或全仓搜索。

| 步 | 用法 |
| --- | --- |
| 1 | 起步一律 `get_minimal_context_tool`（带 `task`、`detail_level="minimal"`），由它决定下一步用哪个工具。 |
| 2 | 架构用 `get_architecture_overview_tool`；评审改动用 `detect_changes_tool` ＋ `get_review_context_tool`；调用 / 依赖关系用 `query_graph_tool` 或 `semantic_search_nodes_tool`。 |
| 3 | 影响面用 `get_impact_radius_tool` / `get_affected_flows_tool` / `get_flow_tool`，别靠猜文件名或通读目录。 |
| 4 | 图缺失或过期先 `build_or_update_graph_tool`（或 `code-review-graph build/update`），不许跳过图谱硬搜。**`head_matches_build` 只比 commit**：改了代码没提交时它仍是 `true` —— 改完代码就 update，别拿它当新鲜度。 |

## 1.2 维护这份文档

| 规则 | 内容 |
| --- | --- |
| 写什么 | **只写现行的规则、键名与实现位置，不记改动史。** 改了口径就**改写**那一条，不要追加「某天起改成…」—— 过程与当时的实测数字属于提交信息，不属于这里（它们已经进了 11 / 12，别再抄一遍）。 |
| 何时改 | **碰了分层 / 契约 / 运行方式 / 数据模型 / 视觉规则**，就回来改这里。 |
| 视觉真相 | 只有一处：`web/src/styles/`（`tokens` → `proto` → `app`），没有别的设计源，旧原型已删。 |
| 编号 | 正文只写编号；**一个编号下可以有多行，每行讲一件事、各自自洽**；跨节引用只写编号。新增规则时接着各自区段的号往下排，不要插队。 |

---

# 2. 一页纸

## 2.1 是什么

每 3 小时，从一条链上的几千个币里挑出 N 个（默认 5 ＝ `per_round`），每个附一份两分钟能读完的判断。**研究流水线，不是行情终端。**

## 2.2 不做什么

交易、仓位建议、买卖点、实时行情、信号指标、多用户、回测引擎。当前接了 **Arc** 与 **Robinhood** 两条链（左栏只渲染 `config/chains/*.yaml` 里 `enabled` 的链）。

## 2.3 红线

| 编号 | 红线 |
| --- | --- |
| 2.3.1 | 分层不成环：前端不碰数据源与数据库；后端不内嵌 HTML（只托管 `web/dist`）；`api/app/core/` 里**不许有 IO**。 |
| 2.3.2 | 一个源一个适配器，源与源互不感知：同名指标取谁的值由 `pipeline/collect.py` 的 `DEFAULT_PRIORITY` / `FIELD_PRIORITY` 决定，不由调用顺序决定。 |
| 2.3.3 | 文案只在后端：页面上的解释性文案由 `api/app/api/v1/forms.py` 给（「说明」icon 的内容），前端不认识任何一条文案。 |
| 2.3.4 | 分数只在后端算：前端不算分、不补数，页面上每个数字都来自 `/api/v1`。 |
| 2.3.5 | 一轮只写一次：卡片文字在落库那一刻就是终稿，没有任何阶段回头改写已发布的报告。 |
| 2.3.6 | 缺值语义 ＝ 未知：没有的数是 `None` / 空，不许用 0 或空字符串替代。 |

## 2.4 一轮的阶段

`拉扫描源 -> 去重 -> 硬过滤（不花钱那一半：市值区间 ＋ 空壳淘汰）-> 补齐链上数据（持有人 / Top10 / 捆绑 / 24h 成交 ＋ 市场口径行情）-> 硬过滤（花钱那一半：流动性 / 24h 成交下限 ＋ 筹码 ＋ 零数据）-> 安全判定与距高回撤（判完整池）-> 链下调研与写作（过关池按流动性取前 N）-> 打分排序 -> 选前 N -> 落库出卡 -> 归档 -> 下一轮对比上一轮`

| 阶段（时间线键） | 做什么 | 实现 |
| --- | --- | --- |
| `pull` | 每个扫描源拉一次全量 | `pipeline/collect.py` |
| `dedupe` | 按 CA 去重，逐字段按优先级合并 | `collect.py` + 6.1.2 |
| `prefilter` | 硬过滤第一半：粗筛行就能定的规则（市值区间 ＋ 空壳淘汰） | `core/filters.py` `check_prefilter_free`（6.1.3 / 6.1.5 – 6.1.7 / 6.1.14） |
| `topup` | 逐币补齐持有人 / Top10 / 捆绑 / 24h 成交，并带回市场口径行情（默认补整个过关池） | `decide.Engine._top_up`（6.1.9 / 6.1.10） |
| `gate` | 硬过滤第二半（流动性 / 24h 成交下限 ＋ 筹码 ＋ 零数据）＋ 安全判定与距高回撤，**判完整个过关池** | `core/filters.py` `check_prefilter_enriched` ＋ `decide.Engine._gate`（6.1.11 / 6.2.1..6.2.8） |
| `draft` | 链下调研与写作：过关池按流动性取前 `research_limit` 个（X 搜索 ＋ 读页面 ＋ 写词条） | `report/research.py`（6.4.1 – 6.4.8） |
| `score` | 链上分位 ＋ 链下块 = 综合分 | `core/scoring.py`（6.3.1 – 6.3.4） |
| `sharpen` | 选出前 `per_round` ＋ 入选后补读 GeckoTerminal | `decide.Engine._picks`（6.2.10 / 9.6.3） |
| `write` | 落库（一笔事务） | `pipeline/write.py`（7.2.1 / 9.6.3） |

表的顺序就是 `decide.py` 里 `clock.mark` 的顺序（`write` 在 `run.py`）。每一步的口径在 6 的各节；动手改流水线之前先把 6.1 – 6.7 读完。阶段耗时每轮打一行日志（`round <id> phase timings: ...`），额度见 9.6.1。

**三个粒度别混**：这九个键是耗时日志与运行面板的键 —— 面板折成六格（`RunPanel.tsx` 的 `STAGE_PHASES`：`collect` = `pull`、`dedupe` 单列、`prefilter`（硬过滤）含 `prefilter` ＋ `topup` ＋ `gate`、`score`（打分排序）含 `score` ＋ `sharpen`、`report`（生成报告）含 `draft` ＋ `write`、`push` 无阶段）；进度条只用五段（`progress.py` 的 `PHASES`：`pull` / `topup` / `gate` / `draft` / `score`，见 9.6.1）。加 / 改阶段要三处一起动（12.21）。

## 2.5 怎么跑

```powershell
# 后端：127.0.0.1:8787。生产也只有一个进程 —— 它同时托管 web/dist。
uv run --project api python -m app

# 前端（开发）：5173，/api 代理到 8787
cd web; npm install; npm run dev
```

- 生产：`cd web; npm run build`（内部是 `tsc -b` + `vite build`），再起上面那条后端命令。前端改完要重新 build 并强刷浏览器。
- 接口类型：Pydantic 是唯一真源 → `cd api; uv run python -m app.export_openapi` → `web/src/api/types.gen.ts`（`cd web; npm run types` 重新生成）。**不许手写 DTO。**
- 本机快捷方式 `scripts/dev.ps1` / `build.ps1` / `openapi.ps1` **不在仓库里**（`.gitignore` 忽略了 `scripts/`），与上面几条原生命令等价。

## 2.6 怎么验（改完必须做）

| 改了什么 | 跑什么 | 通过标准 |
| --- | --- | --- |
| `.py` | `cd api; uv run python -m pytest -q` | 基线 **555 绿**；然后**重启 API**（常驻进程，不重启跑的还是旧代码） |
| 前端 | `cd web; npm run build` | exit 0（内含 `tsc -b`） |
| `prompts/*.md` | `pytest tests/test_prompts.py` | prompt 里的 JSON 示例与 `NewTokenEntry` 一致 |
| 视觉 | 1726 视口深 / 浅各截一遍 | 覆盖报告页（卡片折叠 ＋ 卡片展开）、设定页（含链下调研块）、本轮总览；坐标与计算值用 `getComputedStyle` 验，不靠肉眼 |
| 数据源解析 | `pytest tests/test_<source>.py` ＋ 清对应前缀缓存 | 见 7.1.1 |

长命令（build / pytest）把输出重定向到日志再看尾巴，别把整屏刷进上下文。

## 2.7 本机环境与坑（Windows）

| 坑 | 说明 |
| --- | --- |
| PowerShell 语义 | 不要用 `&&`，用 `;` 或 `if`；含 `&` 的 URL 要加引号。 |
| 写文件一律 UTF-8 无 BOM（JSON / TSX / MD / YAML 尤其） | 不要用 PowerShell here-string 写非 ASCII 内容。 |
| 终端里中文显示成 `????` | 是 GBK 显示问题，**文件没坏**，用 UTF-8 读一遍再下结论。 |
| 视觉验证的 playwright | 必须给 Chromium 绝对路径：`C:/Users/12620/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe`。 |
| 服务常驻 `http://127.0.0.1:8787` | 托管 `web/dist`（前端改完要 build ＋ 强刷，见 2.5 / 2.6）。 |
| 跑 pytest 前先想一下常驻服务 | 保存设定会用 `os.replace` 换掉 `config/chains/<chain>.yaml`，Windows 上目标文件正被别的进程读着的那一刻换不动 —— 抛 `PermissionError [WinError 5]`，整套里偶发一条红（实测 `test_api.py::test_a_text_knob_can_still_be_cleared`，重跑就绿）。判据是「单独跑这条过、只在全套里偶尔不过」——那是抢文件，不是代码坏了。 |
| 删文件前先确认它没被跟踪 | 分支 `improvement`。工作区干净时 `git status -s` 就是空的 —— 判一个文件在不在跟踪用 `git ls-files <path>`，别拿 `git status` 当「一定有东西待提交」。 |

# 3. 任务索引

| 我要做… | 读 | 动 |
| --- | --- | --- |
| 加 / 改一个数据源 | 5 ＋ 该链的 `SOURCES.md` | 通用源 `api/app/sources/`，链专属源 `api/app/sources/<chain>/`；**新源必须同时进 `registry.ALL` 和 `pipeline/collect.py` 的 `FIELD_PRIORITY` + `DEFAULT_PRIORITY`** |
| 调硬过滤默认值 | 6.1.5 – 6.1.7 ＋ 6.1.13 | `config/chains/*.yaml`（保存后以 `settings_versions` 表为准） |
| 改评分权重 / 因子 | 6.3.1 – 6.3.3 | `core/scoring.py` ＋ `config/chains/*.yaml` |
| 改卡片印什么 | 8.1.1 – 8.1.13 | `api/app/report/cards.py` |
| 改卡片文案规则 / 结论写法 | 6.5.1 – 6.5.12 | `api/app/report/prompts/research.md` ＋ `report/schema.py` |
| 改设定页字段 / 说明文案 | 8.2.1 – 8.2.9 | `api/app/api/v1/forms.py` ＋ `web/src/features/settings/` |
| 加 / 删一套设定（配置） | 8.2.2 – 8.2.3 | `api/app/api/v1/chains.py` ＋ `config/chains/*.yaml` ＋ `web/src/features/settings/ConfigManager.tsx` |
| 改视觉（颜色 / 字号 / 主题） | 8.4.1 – 8.6.7 | `web/src/styles/tokens.css`（先改这里，再动 proto / app） |
| 改一轮的阶段或顺序 | 2.4 | `api/app/pipeline/decide.py` ＋ `progress.py` |
| 加 / 改数据表 | 7.2.1 | `api/app/store/schema.sql` ＋ `store/db.py` |
| 改搜索（挖链） | 6.4.3 / 6.4.4 | `api/app/search/`（配置 `config/search.env`） |
| 查「这个数字哪来的」 | 12 | — |
| 想知道为什么这么设计 | 11 | — |
| 想动待决里的东西 | **先问用户** | 10 |

# 4. 术语

| 词 | 意思 |
| --- | --- |
| 扫描 | 本轮 pull 拉到的全部行（`scanned`） |
| 淘汰 | 被某条规则判负（`dropped`）；按优先级只归一次账，见 6.1.12 |
| 候选 | 过完安全门槛的池，**含入选**（`candidates`） |
| 入选 | 最终出卡的那 N 个（`selected`） |
| 未判定 | **没问到**，不是不合格 —— 既不计通过也不计淘汰 |
| 判定池 | 过完硬过滤、送进安全判定的那批，**默认全部判定**（`gate_limit`，`0` ＝ 全池） |
| 调研池 | 过关池里按流动性取前 `research_limit` 个、这一轮真去写词条的那批（`0` ＝ 全部） |
| 链上质量 | 综合分里 55% 的那半，五个链上因子的池内分位 |
| 链下调研 | 综合分里 45% 的那半：五项 0–5 判断 ＋ 卡片文字 |
| 词条 | 一个币本轮的链下调研结果（`lexicon` 表一行） |
| 池内排名 | 该币在本轮候选集里的分位名次（卡片上那一行） |
| 距高回撤 | 距历史最高点跌了多少，见 6.1.13 |
| 貔貅 | 能买不能卖；判定与门槛是两个不同的数，见 6.1.14 / 8.1.7 |
| 陷阱占比 | GMGN 的 `top_entrapment_trader_percentage` |
| 捆绑 / 狙击 / 老鼠仓 | GMGN 的筹码标签（bundler / sniper / insiders） |
| 发射台 | launchpad；Arc 上多数新币由它发出来 |
| 主池 | 流动性最大的那个池 |
| gate | 安全判定那一段（`2.4` 的 `gate`） |
| sharpen | 入选之后补读 GeckoTerminal 那一段 |
| `0` | **是答案，不是缺失**：判空用 `is not None` 而不是 `if value:`，见 6.6.2 |
| 空列表 | 集合类的 `[]` 算「没答」，不赢走字段，见 6.6.3 |
| 市场口径 | 一个数的最终读法以交易市场为准（DexScreener 的 `liquidity.usd` / `h24`），不是榜单或发射台自报的那一个，见 6.1.3 |

# 5. 代码地图

| 路径 | 责任 |
| --- | --- |
| `api/app/api/v1/` | HTTP 层：`chains` / `runs` / `lexicon` / `settings` / `health` |
| `api/app/core/` | 纯逻辑：`filters` / `funnel` / `scoring` / `models`（**不许有 IO**） |
| `api/app/pipeline/` | 一轮的编排：`collect` / `decide` / `states` / `progress` / `run` / `schedule` / `write` |
| `api/app/report/` | 报告：`facts`（唯一事实表）/ `cards` / `assemble` / `lexicon` / `research` / `prompts` |
| `api/app/search/` | 挖链：`base`（命中与 URL 归一）/ `identify`（读页面定 tier）/ `discover`（搜索 ＋ 互证）/ `harvest` / `engines/xsearch` |
| `api/app/sources/` | 一个源一个适配器：**跨链的放根目录，只服务一条链的放 `<chain>/`**（`arc/`、`robinhood/`）；两者都从 `registry.ALL` 取 ＋ `http`（缓存与节流）＋ 各自 `SOURCES.md` |
| `api/app/store/` | SQLite：`schema.sql` ＋ `db.py` |
| `api/tests/` | 测试，按模块分文件（基线数见 2.6） |
| `web/src/features/` | `report` / `settings` / `run` / `rail` / `overview` |
| `web/src/styles/` | `tokens.css`（两态颜色）-> `proto.css`（布局）-> `app.css`（覆盖），**加载顺序即优先级** |
| `web/src/components/` | `Icon`（唯一入口）/ `CopyCa` / `Rich` / `Mark` / `ArcMark` / `RobinhoodMark` / `Pick` |
| `config/` | `chains/*.yaml`（出厂设定，一条链一个文件）/ `llm.env`（写手）/ `search.env`（X 会话）/ 各自 `.example` 模板 |
| `data/` | `crypto-hunter.db` ＋ `raw/`（每轮原始响应与 HTTP 缓存）；不入库 |

**分层是硬约束**（2.3.1）：`api/app/core/` 不许有 IO；`api/app/api/v1/` 是唯一 HTTP 层，页面文案只在 `forms.py`；前端不碰数据源与数据库、不自己算分（2.3.3、2.3.4）。动一个模块前先在图里确认它的调用方（1.1）。

# 6. 流水线

## 6.1 硬过滤

先免费后花钱：第一半只放粗筛行就能定的规则（现在只剩市值区间与空壳淘汰），缺数据的先按 `MARKET_ENRICH` 买回来，再跑第二半。两半的分界是「这个数是不是每个源答得一样」，不是「要不要花钱」。补齐默认覆盖**整个过关池**（`gate_limit: 0`）—— 第二半的规则要对每一行都给出判决，只判一个样本不算过滤。

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.1.1 | 拉取：universe 的主源是「池」不是「币」。GeckoTerminal 分页拉池（`FULL_PAGES = 12`，全量 ≈ 3249 个池/币），RadarDEX 一次拉全量做长尾补充（`limit=2000`，覆盖 ≈ 4145）；Arc 没有 token 级列表端点。 | 别拿 RadarDEX 的 `sort=mcap` 当全链排名 —— 它的市值口径与 GMGN / GT 不是一回事（12.1）。 |
| 6.1.2 | 合并按优先级：一个源一个适配器，同名标量取谁的值由 `collect.py` 的 `DEFAULT_PRIORITY` 定，字段级例外写在 `FIELD_PRIORITY`；集合类型取并集、空列表不赢走字段。 | 细则与反例见 6.6.1 / 6.6.3。 |
| 6.1.3 · 6.1.5 – 6.1.7 | 阈值都在 `config/chains/*.yaml` 的 `pipeline.prefilter`，但**分两半跑**（6.1.8）：第一半（粗筛行就能定、不花钱）只剩市值 1 万 – 20 万（`market_cap_min_usd` / `market_cap_max_usd`）＋ 空壳淘汰（`require_market_data`，默认开：市值与流动性**同时**缺失的行直接淘汰 —— 那不是「源没回答」，是「没有可答的东西」；`MISSING_DATA` 在 `PREFILTER_PRIORITY` 排最后）；流动性 ≥ 5000（`liquidity_min_usd`）、24h 成交 ≥ 1000（`volume_24h_min_usd`）与持有人区间在第二半。**持有人是区间**：`holders_min = 100`，`holders_max` 默认 `null` ＝ 不设上限，上下限共用「持有人」这一条规则（`_ceiling`），**缺键 ＝ 无上限**。 | 为什么只有市值留在第一半：同一个币，市值在 GMGN / DexScreener 两家按美元一致（中位差 2%，实测 2026-09-29；Tolly 有 4.7% 的行差 2 倍以上，12.23），而流动性 / 成交不一致（12.15 / 12.22）——第一半的判决是终审，不能拿没校准的读数开判（动一下就整体重排池子）。下限压到 1 万是为了不漏刚起步的那批（Arc 上 85% 的币停在 launchpad 发币时的 5000–9000 U 初始 FDV）。两个反例：老快照没有 `require_market_data` 这个键 → 一轮把四千个空壳行全判成「未知 ＝ 通过」；随手给 `holders_max` 一个数 ＝ 凭空开始砍币，而「市值 1–20 万的币持有人能到多少」在 Arc 上没实测过（10.4）。以后加上限，淘汰计数、漏斗行、设定页那一行都不用跟着改。 |
| 6.1.4 | 零数据淘汰：五个排序因子（`scoring.FACTOR_FIELDS`：流动性 / 24h 成交 / 持有人 / Top10 / 捆绑）一个数都没有的按 `missing_data` 淘汰 —— 它没法打分。**只在富集之后判**（`check_all`）；关掉用 `prefilter.min_scored_fields: 0` 或缺键。 | 富集前空字段的含义是「粗筛没带这个字段」；放进 `check_prefilter` 会把粗筛没带字段的行整批误杀。 |
| 6.1.8 · 6.1.14 | 第二半（`check_prefilter_enriched`，跑在补齐之后）：流动性 ≥ 5000、24h 成交 ≥ 1000、持有人区间、Top10 / 捆绑上限、貔貅。**未判定永不淘汰**：整源缺失记 `None`，既不判通过也不判淘汰 —— 所以凡是要读单个币才能定的数都只能放这一半，粗筛没带的行读出来是 `None`，先按 `MARKET_ENRICH = ("dexscreener", "gmgn")` 把缺的买回来再裁决。 | 反例：两半合成一次，未知等于放行，上千个发射台自报的行直接进池。 |
| 6.1.9 · 6.1.10 | 补齐链上数据是独立的一次：`_needs_chips` 决定补谁（缺持有人 / Top10 / 捆绑 / 24h 成交任一项），补多少受 `ENRICH_POOL_MULTIPLE × gate_limit` 限制 —— `gate_limit: 0`（默认）就是补整个过关池；答案落 `item.metrics`，但**不置 `enriched`、不写 `facts`** —— 安全判定仍只由 gate 那一遍给，否则 gate 会以为这个币判过了。补齐走轻量入口 `GmGn.fetch_chips`（只发 `token_stat` 一个请求，不是 `fetch_token` 的四个端点，映射共用 `_stat_facts`）；DexScreener 同批必问 —— 它是市场口径源，而发射台自报的深度是虚的（Tolly 全族 5.52 倍）；短窗口读出来的成交量在这里留空，等这一遍按币问回来（12.22）。 | 用全量 `fetch_token` 的话，这一步的耗时就是「行数 × 0.45s 每主机节流」，worker 开 8 / 16 / 24 都一样（已到节流下限）。 |
| 6.1.11 | 短名单排序键 ＝ 流动性：`_depth` 按流动性降序，**完全相同**才用 24h 成交额破平 —— 成交额永远不会把浅池排到深池前面。 | 反例：用 `min(流动性, 成交额)` 或拿成交额当主键，深但没量的池会被刷量的浅池挤掉。 |
| 6.1.12 | 淘汰数按优先级累计，不是独立计数：优先级顺序即设定页 `RULE_ROWS` 顺序 —— 市值 -> 流动性 -> 持有人 -> Top10 -> 捆绑 -> 貔貅 -> 24h 成交 -> 源码 -> LP -> 危险权限；**同时踩真阈值的仍记在阈值那一行**（距高回撤排最后）。这个顺序是**归属顺序**，不是运行顺序 —— 流动性 / 成交的下限现在跑在第二半（6.1.8）。实现是 `core/funnel.py` 的 `cumulative_drop_counts` / `DROP_PRIORITY`。 | 各行独立计数，加起来就不等于 `dropped`。 |
| 6.1.13 | 距高回撤：回撤 ≤ `-70%` 淘汰，**三个读数任一枚举跌够即淘汰**，不是三个都要同意。读数三条：① **源高点** `metrics.high_market_cap_usd`（GMGN `history_highest_market_cap`，随 pull 就来，是测量值）；② **库记录高点** `tokens.peak_market_cap_usd`（每轮把源高点回写，upsert 只升不降，所以源沉默的轮次也仍有真高点）；③ **池面高点** `metrics.drawdown_pct`（GeckoTerminal 按池子全历史日线，`ATH_DAYS = 1000`）。噪声闸 `HIGH_SANITY_MULTIPLE = 50`：任何高点超过链市值上限 ×50 视为解析噪声丢弃（三个来源共用）。 | — |
| 6.1.13 | 距高回撤的例外与落点：整源沉默（`drawdown_unread` 未置位）＝ 放行（**首见不算「未知」**）；池面**读失败**（429 / 超时，`fetch_token` 置 `drawdown_unread = True`）＝ 一个没问到的问题、不是答案，记**未判定**而非静默放行。gate **不买**池面读数（一次问二十个会被 GeckoTerminal 打限流），池面由画卡那一遍顺带买（`_picks` 的 `_enrich(only=DEFERRED_ENRICH)` 之后 `check_drawdown` 再判一次，跌够当场淘汰、顺位补下一个，所以卡片数不会因此变少），且**这一遍记 `unknown`**（池面读失败的入选币在报告里显示未判定，不是静默通过）。键 `pipeline.prefilter.drawdown_max_pct`；配套 `geckoterminal.py` 的 `_ath_pools()` 取最深池 ＋ 第二深池（≥ 最深 10%）各读一条日线、取较大高点，`_read()` 对 429 退避重试（5 / 10 / 15s，`RATE_LIMIT_TRIES = 3`）、404 记 `NEGATIVE`，`OHLCV_TTL` 6h → **24h**。 | 反例：要求三读数一致 → 那批 169 个已跌够的币一个没淘汰，ARCAT 漏进报告（12.4）。 |

## 6.2 数据源、安全判定与富集

**分工表**（逐源端点、传输方式与实测结论见该链的 `api/app/sources/<chain>/SOURCES.md`，改源之前先读那份）：

| 用途 | 源 |
| --- | --- |
| 全链扫描（`scan_sources`） | Arc：gmgn / radardex / tolly / geckoterminal；Robinhood：gmgn / geckoterminal / coinbarrel / pez / peeps |
| 安全与源码 | goplus（貔貅、税、危险权限、`dex[]` / `holders[]` / `lp_holders[]`）/ sourcify（源码验证） |
| 补链接 ＋ 校准行情（`registry.ENRICH_ONLY`） | dexscreener：填 `socials` / `websites` / `description`，**并在补齐那一遍领跑市值 / 流动性 / 24h 成交 / 价格 / `price_change_24h_pct`** —— 它是市场口径源，而它没有全链列表，所以只可能赢下逐币那一遍（6.2.1） |
| 挖链找页面（在 `app/search/`，不在 `sources/`） | X 高级搜索（`search/engines/xsearch.py`，cookie 子进程，不吃 token）＋ `identify` 读页面互证；只交出候选链接，tier 由 `identify` 挣 |
| 链上直读 | `sources/onchain.py` 走链自己的 RPC（LP 位置、dev 余额） |

**新增一个源时的三个坑**（都是静默的，编译器不报）：

1. **`FIELD_PRIORITY` 决定一个字段能不能输出**。合并逐字段走那张表，表里没列的源对该字段**永远赢不了**，哪怕它答了 —— 行会以「未判定」通过硬过滤、以「空壳」被淘汰。新源要么进已有字段的表项，要么落进 `DEFAULT_PRIORITY`（链专属源排最后：它们只在自己那条链的配置里出现，所以对别的链不是覆盖，是自己链上的兜底）。
2. **只服务一条链的源不要留在根目录**。RadarDEX / Tolly 在 Robinhood 上返回的是 Arc 的整份数据（实测：`?chain=robinhood` 的 CA 集合与默认调用逐字节相同），放进 `robinhood.yaml` 等于把另一条链的币灌进池子。
3. **链自己的 id 就是通用源要的 slug**。`gmgn` / `geckoterminal` / `dexscreener` 都把 `chain_id` 直接当路径段或 `chainId` 用，所以 `config/chains/<id>.yaml` 的 `id` 必须是那条链在外部站点上的名字；只有 `goplus` / `sourcify` 要另给 EVM chain id（各自的 `CHAIN_IDS`）。

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.2.1 | 扫描源与富集源分开：`scan_sources` 只放能供全链表的那几家；`registry.ENRICH_ONLY` 的源（当前只有 dexscreener）**不进扫描**，只在逐币那一遍被问。它在两处上场：填 `socials` / `websites` / `description`，**并领跑四个市场数与 `price_change_24h_pct`**（`collect.FIELD_PRIORITY` 里排第一）—— 它是市场口径源，而 `fetch_universe` 返回空，所以它只可能赢下逐币那一遍。 | 反例：把 dexscreener 加进 `scan_sources` —— 它没有全链列表，只会在凭证行里挂一个 0。真正的代价在别处：只有被逐币补齐的那批行带市场口径，池子里其余行还是粗筛口径，分位分母因此是混的（10.9）。 |
| 6.2.2 | GoPlus 独占 `pool_list` / `top_holders` / `burn_pct` / `locked_holder_pct` / `contract_holder_pct` / `transfer_tax_pct`；`holders` / `top10_pct` / `buy_tax_pct` / `sell_tax_pct` 以 gmgn 为主、goplus 兜底（一次响应 34 个字段，只用其中一部分是浪费）。写在 `pipeline/collect.py` 的 `FIELD_PRIORITY`。 | 让 gmgn 抢 `pool_list`，它的空列表会把 goplus 的答案挡在外面（6.6.3）。 |
| 6.2.3 | blockscout **不在 `scan_sources`**：它仍在合并优先级链里做兜底，但列表接口只有持有人数、市值与流动性全空，会把候选池灌成四千个。explorer 的 `blockscout_checked` 不走 `http.cached`。 | 「浏览器不认识这个地址」每轮都会重问，这是已知的重复代价。 |
| 6.2.4 | 税率只认 GMGN 安全表：读 `gmgn.ai/api/v1/token_security_evm/{chain}/{ca}` 的 `total_buy_tax` / `total_sell_tax`（缓存 6h）；所以 `FIELD_PRIORITY` 里 `buy_tax_pct` / `sell_tax_pct` **只列 gmgn**，没有表的标的宁可显示「—」。同一张表带 `is_honeypot`，是貔貅判定的第二声（三态另有实现，见 8.1.7）。 | GMGN 排行行的税率是占位 0、goplus 的税率在 Arc 上也全是 0，而 **0 是「有值」、会赢走字段** —— 把 goplus 兜底进来，报告里全部税率的 0 都是假的。 |
| 6.2.5 | LP 判定先链上、答不出才用 GoPlus：`onchain.lp_lock_from_holders()` 读 `lp_holders[]`（每条带 `percent` / `is_contract` / `is_locked`；`percent` 是 LP 供应占比、整表合计 1）—— **多数 LP 在合约里（锁仓合约 / 金库 / 发射台持仓）＝已锁，多数在钱包里＝未锁**；表覆盖不到 50% 的 LP 时不给答案。判定有 `_recorded_lp` 与 `_lp_from` 两条路径，**改口径时两条都要动**；「未判定」的原因取 `LAUNCH_*` 字面值（`LAUNCH_OK` / `LAUNCH_V4` / `LAUNCH_NO_TX` / `LAUNCH_EXPLORER` / `LAUNCH_NODE` / `LAUNCH_NO_TARGET` / `LAUNCH_NO_POOL` / `LAUNCH_BASIS`），实现见 `api/app/sources/onchain.py` 与 `report/cards.py`。 | v4 池没有池合约（头寸是头寸管理器里的 NFT），这就是链上读答不出的原因 —— 要补最后一段只能问头寸 NFT 的 `ownerOf`（头寸管理器地址在 `sources/onchain.py`）。卡片只分三态（8.1.8）。 |
| 6.2.6 | 源码验证是双源取或：`source_verification_mode` = 双源（GoPlus ＋ Sourcify），语义是**任一源确认即算已验证**（`dual` 取 `or`）。 | 写成 `and` 会在 Sourcify 收录很差的 Arc 上一票否决（6.6.4），切「仅 GoPlus」会更宽松。 |
| 6.2.7 | 链上直读走链自己的 RPC：`sources/onchain.py` 读 LP 位置与 dev 余额，走 `chains/*.yaml` 里该链自己的 RPC 端点；一条链只有一个端点、没有备份，记账走同一套 `tally`（7.1.3）。缺 `rpc` / `explorer` 键**直接报错**（`onchain._own_address`），不回落任何默认端点。 | 把链上读接到第三方 explorer API，延迟反而更差（RPC 单次硬地板 2.2–2.5s，见 9.1）。**别再加默认端点**：曾经兜底到 Arc 的节点，链配置漏写 `rpc` 就会拿另一条链的数据、还标着本链的名字。 |
| 6.2.8 | 安全判定判**整个过关池**（`pipeline.gate_limit`，默认 `0` ＝ 全池）：过完硬过滤的行全部送安全判定，读不出来的是「未判定」（既不计通过也不计淘汰）。**这里没有「凑够几张卡就走」的预算** —— 按流动性取前 N 再判，就是把名额先给了最深的那批，而链上排最深的行恰恰是筹码 / 安全规则最想淘汰的那批（Robinhood 2026-09-30：按流动性前 20 个被 17 个 LP 未锁 / 陷阱 / 回撤占满，健康的币连「行不行」都没人问）。真正的预算上限在花钱那一步 —— 链下调研的 `pipeline.research_limit`（6.4.1）。想人为限制判定数仍可填 `gate_limit`（`0` ＝ 全池，负数按 `0` 读）；批数上还想再加一层就用 `pipeline.gate_rounds`（默认 `0` ＝ 不限批）。 | 反例：默认取前 20 判定 —— 深而脏的行吃光名额，浅而干净的行无人裁决（11.25）。 |
| 6.2.9 | 逐币富集：同币多源并发、结果按源顺序合并。`ENRICH_WORKERS = 8`，同一个币的多个源**并发**问，合并顺序仍按源顺序，`_absorb` 结果不随并发顺序变。 | 反例：按「谁先回答」合并，源换了顺序结论就变（6.1.2）。 |
| 6.2.10 | GeckoTerminal 精查延后到入选之后：`DEFERRED_ENRICH = ("geckoterminal",)`。它是唯一能回答「几个池、价格走过的路」的源，也是最贵的一个（逐币三端点、同主机 2.2s 节流、429 固定退避 10s、加线程无效），而它填的全是卡片字段（不进安全判定、不进分数）。所以 gate 阶段跳过它，`picked` 定下来后只对入选的补一遍（`_enrich(..., only=DEFERRED_ENRICH)`），日志单独一行 `sharpen`。第二遍**不要**从批量行重建 `views`（第一遍刚拿到的逐币值会被粗筛行盖掉），正确做法是并进第一遍的 `views` 再 merge；`facts` 与 LP 上链读取在这一遍整段跳过。 | 池面读数由这一遍顺带买（6.1.13）。 |

## 6.3 打分

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.3.1 | 分位分母 ＝ **本轮硬过滤幸存者**（不是全历史池，也不是过完安全门槛的池）：`core/scoring.py` 的 `CandidateSetPercentile`，中点法 `(below + 0.5*ties)/n`；分母用**补齐后的** `item.metrics` 构造（`decide.py` 在 gate 之后建它），为的是让这个币和它比较的池子在同一口径上。 | 代价：**分母因此是混口径的** —— 被补齐的行是 DexScreener 的市场口径，没被补齐的行还是粗筛口径（实测 97 个幸存者里 58 个 gmgn / 34 个 dexscreener / 5 个 tolly，见 10.9）。所以别再把新源塞进 `scan_sources` 想「顺手校准」（6.2.1），也别指望分位对口径免疫。 |
| 6.3.2 | 五个链上因子 ＋ 两个逆向因子：`scoring.FACTOR_FIELDS` = 流动性 / 24h 成交 / 持有人 / Top10 / 捆绑；逆向因子 `REVERSE_FACTORS = {top10_concentration, bundling}`（越小越好，分位反算）。 | 反例：把 Top10 / 捆绑当正向因子，筹码越集中分越高。 |
| 6.3.3 | 综合分 = 链上质量 ＋ 链下调研：`onchain_weight` 默认 55%、`offchain_weight` 默认 45%（`config/chains/*.yaml`，以 `settings_versions` 表为准）；两块各自的五个因子带一套 `*_factor_weights`（出厂各 20%，见 8.2.9），合成时按权重归一，缺项读 0 不进分母。 | 两层权重都在设定页上、落库成设定版本、下一轮生效；两个总权重合计与每套五个因子合计都必须 = 100%，否则 422（`settings.py` 的三条 detail）。 |
| 6.3.4 | 链下调研没落地的标的 = `pending`（冷启动）：本轮调研还没回来的，既不参与排名也不显示 0 分，等它落地后下一轮才有分数（`scoring.score_token` 的 `pending`）。 | 给 0 分的话，一个刚部署、还没来得及调研的币会被排到末尾当成「差」（6.5.11）。 |

## 6.4 链下调研

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.4.1 | 调研是一步，且在记分之前：调研池的每个币都读项目自己的页面（官网 / X / GitHub / TG），一次模型调用先把词条写好、读过的页面进 `lexicon_sources`，再拿这份词条去打分、落库 —— 它在轮次里跑，**没有发布之后的异步动作**。 | 反例：先打分、事后异步补词条，卡片文字与分数就不同源，同一轮出现两个版本的判断（6.2.10、6.3.3）。 |
| 6.4.2 | 每轮全新调研，没有跨轮复用：五项链下分与卡片正文都来自**本轮**的调研结果，词条带 `run_id`；落库前 `lexicon.forget_other_rounds(run_id)` 把其它轮次的词条删掉（连带 `lexicon_sources`）。 | 旧设计的「2 天过期 / 市值波动 40% / 6h 跳过窗口 / 人工确认」**全部取消** —— 只要入选，这一轮就重新写一遍文字、重新打一遍分。 |
| 6.4.3 | 挖链是 X 高级搜索**两问**：`app/search/engines/xsearch.py` 拿 `config/search.env` 里的 cookie 跑，每个标的 CA 走 `Latest`、`$TICKER` 走 `Top`，合并后按「名字贴合 ticker」排序。**搜索不走模型**（不花 token），`SearchService` 按顺序问引擎、第一个有结果就停；没配 cookie 时整层静默失效，卡片只用本轮登记的链接，**不阻塞流程**（9.5.3）。 | 只发 CA 那一问会漏：实测只有 ticker 那一问能捞到某些官方号（地址一问 20 条里一个都没有）。 |
| 6.4.4 | 互证只读前 8 个候选页，`tier` 由页面自己挣：`identify.read_profiles` 只读前 8 个（刷屏账号会把这 8 个名额占满，所以排序决定谁先被读）；**bio 带 CA 才是 own**。站点链接先归一到根域再判。 | 不归一的话，一个 launchpad 的三个深链会被逐个读成 none。 |
| 6.4.5 | 读页面的素材上限：正文合计 **15000 字符**，链接最多 **8 个**；官方 X 时间线另给 **15000 字符**（X 走 `user-posts` 取最近约 20 条，全部带时间戳与互动数）。`READABLE_SOCIALS` **含 TG** —— `t.me/<name>` 的公开预览页带频道名、成员数与最近几条帖子，正是社媒行要的东西。 | 上限压到「够写一段」的量，模型就只能凭事实表写，叙事退化成数字堆砌。 |
| 6.4.6 | 空壳页面交浏览器画，账号页永远不画：静态 lane 不跑 JS，而 Arc 上的发射台官网多是 React 空壳，判定阈值 `identify.SHELL_WORDS = 40`（同批次最低的官网有 94 个词，两边差得很开）。渲染只在**已证明是空壳**时发生，且只有**候选官网**走这条路（`identify.fetch_page`）：X 与 TG 是例外（`identify.NO_RENDER`）—— X 只把 bio 给静态客户端、浏览器看到的是登录墙，TG 预览页画前画后都是 27 个词，所以 X 账号页仍旧 `fetch_html`，浏览器只伺候候选官网。等待用 `DOMContentLoaded + 2.5s`（`networkidle` 在 memespad 上是 21.8s，拦图片 / CSS 反而更慢还会把 SPA 打残），单页实测 2.4–12.0s；`sources/browser.py` 是一个 Chromium、自己一个线程跑一个 event loop（playwright 同步 API 跨线程会崩、标签页上限 6）（6.4.5）。 | 实测 `memespad.com` / `www.arcdex.online` / `usearckit.online` 静态只有 8–10 个词、连 CA 都搜不到，渲染后是 377–827 个词且两个带 CA —— 之前 `usearckit.online` 因此判成 none，卡片上挂的是第三方站 `myarcade.fun`（`r.jina.ai` 已去掉）。 |
| 6.4.7 | 写词条是一个币一次调用，宽度是实测值：每个币一次 `complete_json`（一个币的输出约 35k token 也稳）。并行宽度：找链接 `SEARCH_WORKERS = 15`、写词条 `CH_RESEARCH_WORKERS = 12`（默认）。「一次调用写完所有条目」必然撞天花板，已废弃。 | `SEARCH_WORKERS` 提到 20 则实测 20 个并发有 3 个 429 而且更慢（15 个并发跑 20 个标的零 429）；一个币失败只丢它自己那条，原因进本轮 note（`链下调研未完成：…`），不阻塞整轮（6.5.11）。 |
| 6.4.8 | LLM 客户端走一个 OpenAI 兼容端点：配置在 `config/llm.env`、由 `api/app/config.py` 读 —— **真实环境变量优先**，这个文件**每次调用时重读**，所以改完下一轮生效、不用重启，模板见 `config/llm.env.example`。 | 当前配置 `https://opencode.ai/zen/go/v1` ＋ `deepseek-v4.1-flash`（写作）；有没有写手看 `/api/v1/health` 的 `llm` / `llm_model`。变量见下表。 |

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

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.4.8 | 换 key 的规则（`client._call_payload`）：只有 401 / 403 / 429（key 失效或额度用尽）才换下一个 key，并把换掉的那个晾 `CH_LLM_KEY_COOLDOWN` 秒，所以一个死掉的主 key 一轮只白撞一次；别的 4xx（模型名写错、请求体不合规）换 key 也是一样的错，直接失败；5xx 与网络错误在同一个 key 内按 `CH_LLM_TRIES` 重试。 | **日志里只出现 key 的 sha256 前 8 位指纹，不出现 key 本身**。 |
| 6.4.8 | `CH_LLM_TIMEOUT` 必须高于成功调用的长尾：79 次 research 调用 p90 = 147s、最长成功 296s，180s 会砍掉正常调用、砍掉之后还会重试一次；思考关掉后 research 单次实测 11–17s，300s 只剩兜底作用。 | **它与重试相乘**：180s × 2 曾让一次死掉的调用独占 366s（那一轮 draft 421s）。 |
| 6.4.8 | `CH_LLM_REASONING` 只有 `none` 是真开关：`minimal` / `low` 照样花 680–899 思考 token，`none` 是 0。 | 实测（同 5 个币、同一条生产路径、并发）：`none` 94.9s / 5 个词条全过 / 正文 8711 字符，端点默认 340.1s / 只过 4 个（丢的那个 248s 后回空消息）/ 正文 6199 字符 —— **快 3.6 倍、卡更全、字更多**。代价是约四成草稿漏必填分项（`f_community` 这类），**由 6.5.9 的重问兜住**（那 5 个里触发 2 个、都补回来）；同一批币在没有重问的旧代码上 `none` 只过 3/5，所以这一行和重问是一套、**不能只上一个**。 |
| 6.4.8 | 三个坑（都已踩过）：① **推理模型的思考计入 `max_tokens`** —— 额度给小了返回**空消息**（`finish_reason = length`），不是报错，客户端把空消息当失败，于是额度调小会静默降级成「没有文字」。② **该网点偶发 503** `Endpoint is unavailable`（`gpt-5.6-luna` 直接不可用），所以有上面那条重试规则。③ **一次调用只写一个币的词条**（6.4.7）。 | 另外 hermes 里那两个 deepkey key（`sk-fm7NO…`、`sk-72CtQ…`）实测都是 401，**都不要用**。 |

## 6.5 词条文字

prompt 文件只有一份：`report/prompts/research.md`，`PROMPT_VERSION` 随它升级。**另有第二段提示词内联在代码里**：`research.py` 的 `REPAIR_HEAD`，只在校验没过时发出去（6.5.9），落库记为 `prompt_version = research.md@8+repair`、`purpose = research_repair` —— 后缀表示「research.md ＋ 一段代码里的重问指令」。**一份 prompt 服务所有链**：链名写成占位符 `__CHAIN__`，`ask()` 按 `config.chain_name()` 替换（6.5.12）。

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.5.1 | 篇幅上限写在 prompt 里，代码只在超长时记日志、**不退回**（丢一个词条就没有链下分，比文字长一点贵得多）。上限在 `report/schema.py` 的 `CAPS`；超长走 `log.warning`，`validate_*` 返回值不变，同一个字段记**两条**（`ask` 决定要不要重问、`one` 决定要不要丢这个币，两个判定点都要结果）。 | 上限见下表。 |

| 字段 | 上限 |
| --- | --- |
| `concl` | 500（prompt 要求 200–500，代码下限 160） |
| `prod_text` / `substance_text` / `team_text` / `narr_text` / `social` | 各 200 |
| `badge` | 16 |

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.5.2 | 涉及源码 / 权限 / LP 的句子必须**一字不改**地引用事实表 `【安全】` 行的原文（例：`源码 已验证 ｜ 危险权限 无危险权限 ｜ LP 已锁`）。 | 反例：卡片曾一边显示「LP 已锁」一边在词条里写「三项全部未判定」。 |
| 6.5.3 | 链接统一成「说明 ＋ 可点击词」：`官网 <a href="...">solonpad.fun</a>`（prompt 要求），`report/segments.py` 把 anchor / markdown / 裸 URL 都变成 link 段，前端 `Rich.tsx` 渲染成 `target="_blank"` 的 `<a>`。 | **全链路没有 `dangerouslySetInnerHTML`** —— 不要让模型文本直接进 HTML。 |
| 6.5.4 | 页脚列所有入口，正文不放链接：页脚来自 `cards.links()` —— 交易页 `GMGN ｜ Debot ｜ DexScreener`，然后 `X ｜ TG ｜ GitHub`（取本轮指标里的社媒）和**第一个**官网，全是文字下划线链接、**没有打开图标**；正文（含 `social`）不重复放这些链接。 | 反例：做成一排七个外链图标，基本就只剩图标了。 |
| 6.5.5 | 三条纪律：① `concl` 与 `narr_text` **严禁重复**（确实没有新内容就留空 `""`）；② 提到外部信息必须附**实读到**的原文 URL；③ `social` 只写判断、用时间线的互动率与发帖节奏说话，**不放链接、不拿粉丝数当卖点**，第三方讨论只当「有多少人在谈」的计数。 | 新项目粉丝便宜，证明不了任何事。 |
| 6.5.6 | 结论的写法：`concl` 200–500 字（代码下限 160），prompt 定五段 —— 定位约 110 / 实质约 100 / 亮点约 130 / 风险约 110 / 收尾约 40 字，**不加小标题**；第一句必须是身份句（链 ＋ 赛道 ＋ 定位）且**叙事先行**，风险段写最致命的 1–2 条、要事实性。**不写观点句**（「本批唯一…」「第一梯队」一律不写，判断留给读者）、**不写数字段**（市值 / 流动性 / 持有人 / 量能 / 筹码不罗列，表格与卡片上都有）。 | 卡片**不为叙事单开一块** —— 叙事落在结论里，`narr_text` 退为补充位（定位 / 机制 / 竞争位），通常留空（8.1.6）。 |
| 6.5.7 | 写作输入必须带页面正文：输入就是「读到的页面 ＋ 事实表」，页面读不到时那个币就**没有词条**，而不是让模型凭记忆编 —— prompt 里没有「没读到就照事实表写」这种口径。 | 反例：给模型一句「如无资料请据常理推断」，报告里就会出现不存在的官网与产品。 |
| 6.5.8 | 调研必须带安全块：`research._latest_metrics` 一并读 `safety_json`，`_facts_line(ca, symbol, metrics, safety)` 交给 `facts.block`。 | 不带的话，管线里最宽的那张事实表恰好看不到安全块，词条会永远写「安全块三项全部未判定」。 |
| 6.5.9 | 先重问一次，再降级：`ask` 把上一次的 JSON 和校验器原话（`REPAIR_HEAD`）一起发回去，要一份**完整重写**；第二次还不过才按降级处理（6.5.11）—— 漏写一个分项是笔误，不该因此丢一张卡。 | 不重问的话，思考关掉后约四成草稿漏必填分项，`none` 那一档会直接损失卡片（6.4.8 的实测）。 |
| 6.5.10 | 「读不到」和「没东西」分开：① **空壳页面等于没读** —— 静态 HTML 正文不足 `MIN_CHARS`（80）时 `_read` 返回 `None`，而失败的页面不落库；解法是两处读页面走同一道门（`identify.fetch_page`）：静态两条 lane 先走、正文不足 `SHELL_WORDS = 40` 个词就交给 Chromium 重抓、**两份取词多的**（6.4.6）。② **TG 要读**（`READABLE_SOCIALS` 含 TG）。 | 表面「读了 2 个链接」实际只剩一个；把「页面空」当成「项目没有官网」，模型看到的世界里这个项目就凭空少一个入口。 |
| 6.5.11 | 降级 = 该币无词条：`research.research_entries` 对读不到页面 / 答不出契约的币**不写词条**，该币没有链下分（`scoring.score_token` 的 `pending`）、不进排名 —— **不会用「确定性拼装的正文」冒充一份调研**。失败原因进本轮 note（`链下调研未完成：…`），**不阻塞整轮**。 | 反例：拿确定性模板拼一段文字顶上，卡片上有字、却没有调研，读者无从分辨（6.3.4）。 |
| 6.5.12 | 链名分两个：`label` 是**雷达**的名字（侧栏、配置管理抽屉），`name` 才是**链**的名字（`config.chain_name()`，yaml 没写就回落 `label`）。写进文字的三处都用后者：写作 prompt 的 `__CHAIN__`（`research.py` 的 `CHAIN_TOKEN`）、搜索词（`search_links(..., chain_name=)` → `discover` 的 `build_query`）、设定页 `f_narrative` 的口径行（`forms._offchain_labels`）。 | 反例（2026-09-30）：Robinhood 链的卡片在「叙事与官方关系」里写「与 Arc 无公开关系」—— 一份 prompt 写死了 Arc，模型只能照着答；设定页也印着「离开 Arc 跑不起来 = 5」。 |
## 6.6 取值与合并

这些规矩都是被具体的错纠过之后定下的，**别再犯**。

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.6.1 | 判据是「谁的值更可信」，不是「谁先回答」：同名的**标量**取谁的值由 `collect.py` 的 `DEFAULT_PRIORITY` 决定，字段级的例外写在 `FIELD_PRIORITY`（源侧分工见 6.2.1 – 6.2.3）。 | 反例：按「谁先回答」取，源换了顺序结论就变。 |
| 6.6.2 | 0 是答案，不是缺失：`decide._absorb` 用 `is not None`（空列表也是答案）；GMGN 的 `X or Y` 用 `_first(X, Y)`；`creator_created_count` 的 `_positive` 用 `_num`（dev 发币数 0 是发现）。 | 反例：用 `if value:` 判空，0 被当成没答，dev 发币数 0（重要发现）就被丢掉。 |
| 6.6.3 | 集合类型的空值算「没答」：`collector.merge` 里 `[]` 不赢走字段（与 `price_series` / `socials` / `websites` 一直以来的写法一致）。 | 不挡的话，GMGN 的空 `pool_list` 会把 goplus 的答案挡在外面。 |
| 6.6.4 | `source_verdict` 的 `dual` 取 `or` —— 任一源确认即通过。 | 写成 `and` 会在 Sourcify 收录很差的 Arc 上一票否决（6.2.6）。 |
| 6.6.5 | 覆盖方向是 `overlay(universe_bulk, per_token_merge)`：逐币富集值盖在批量视图上，**别写反**；`overlay` **不复制 `provenance`**，所以 `provenance` 要单独合并。 | — |
| 6.6.6 | 卡片读数取「先有值的那一份」：同一批读数库里存两份（`metrics` 批量榜单、`safety` 逐币判定），`cards._either(metrics, safety, key)` 逐字段取。 | 硬过滤与安检读的是 `metrics`，所以取错只是显示错、不是判断错 —— 但显示错同样要修。 |
| 6.6.7 | 税率只认 GMGN 安全表（见 6.2.4）；`dex[].pool_fee` 在 v4 上不可读，**池子清单不印费率**。 | 印上卡片那一列全是「—」。 |
| 6.6.8 | 事实表只有一份实现：`report/facts.py` 的 `lines()` / `block()` / `safety_line()` 由**卡片与词条两处共用**，缺值打印「—」（「五个源都没这个数」本身就是事实）。 | 两边各拼一份，同一轮里两处对同一个字段的取值就不一样（6.5.8 的「永远写未判定」就是这么来的）。 |
| 6.6.9 | 链接逐币从 DexScreener 读（`sources/dexscreener.py`：填 `socials` / `websites` / `description`，**并在 `FIELD_PRIORITY` 里领跑四个市场数与 `price_change_24h_pct`**，见 6.2.1）；它不在 `sources:` 里，而是 `registry.ENRICH_ONLY`，由 `Engine._enrich` 在逐币那一遍问 —— 位置在写词条之前，所以链接是写作输入的一部分，而 X 搜索挖到的候选链接是另一条来源（6.4.3 / 6.4.4）。取值只认 `baseToken.address` 命中的那一对，否则若答案里只有一条链才接受；社媒的 handle / 裸域名由 `base.social_url` / `base.web_url` 补主机与 scheme（RadarDEX / Tolly / GMGN 共用）。 | 反例：另一条链的 X 句柄写到这张卡上 —— 比空着更糟。 |

## 6.7 读数与漏斗

**四个数（扫描 / 淘汰 / 候选 / 入选）必须一次数出来**，口径只有一个实现。

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 6.7.1 | 四个数一次从 items 的 decision 数出来（`core/funnel.py` 的 `summarize()`，`RoundResult.funnel_summary`，`write.py` 照抄）。 | 于是 `scanned = dropped + candidates` 是**恒等式**，不再是四个各自来源的数字碰巧排在一起（四个数各自统计就会加起来对不上 —— 读者第一眼就会去加）。 |
| 6.7.2 | 落选池标题的「候选」**含入选**（否则「候选 − 入选」不等于表里的行数）；设定页那句「漏斗」文案同理用 `universe − candidates`。 | — |
| 6.7.3 | `runs.dropped` 是总淘汰，份额单独列：面板「硬过滤」那一行的份额写进 `run_summary.dropped_hard`（前端**不靠减法猜**），由 `filter_dropped` 算（按优先级归属，含富集之后才被硬过滤规则淘汰的标的）。 | 前端用「总淘汰 − 其他」倒算，规则一改就错位。 |
| 6.7.4 | 落选池收**全部候选**：有分的按分数排，没分的排在其后并写明理由（`未建档（本轮不参与排名）` / `数据缺失，本轮不参与排名`）。 | 只收有分的，就会出现「候选 78」而表里只有 20 行。 |
| 6.7.5 | 退出项的名字从 `tokens`（CA 主档）取 —— 退出项被画出来时那个币已经不在本轮报告里，只有主档还记得它叫什么。 | 退回 `ca[:10]` 就是给读者一串没有意义的地址片段。 |
| 6.7.6 | 这些数前端一个都不重算，全部来自 `/api/v1`（2.3.4）。 | 前端自己算一遍就会与后端口径漂移。 |

# 7. 数据与状态

## 7.1 缓存与记账

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 7.1.1 | 「对方说没有」也是答案：生产者用 `http.NEGATIVE` 标记「对方答了，答案是空」，`cached` 会缓存它（读回来仍是 `None`，调用方无感），真正的 `None`（超时、5xx）照旧不缓存。 | 不缓存否定答案，Arc 上多数币在 GeckoTerminal 没有池，等于每轮重问、每次 3 次重试 ＋ 2.2s 节流。 |
| 7.1.2 | 失败退避只在失败路径上生效：传输层默认 `sleep` 0.8、GT 逐币端点 1.0，成功路径的节流没动；Sourcify 的 404 是答案不是失败，所以 `tries=1`。 | 成功路径也退避，每轮白等几十秒。 |
| 7.1.3 | 记账要覆盖最贵的那台主机：`http.tally()` 是公开的记账入口，`onchain.rpc_checked` 按同一套计数（ok / retry / fail ＋ 耗时）记账，所以链节点会出现在每轮的主机行里。 | 漏记链节点，每轮最慢的那台就看不见。 |
| 7.1.4 | 日志是唯一的耗时真相：`app/logging_setup.py` 在 `python -m app` 启动时给 `crypto-hunter` 命名空间挂 stderr handler（`CH_LOG_LEVEL` 可调，默认 INFO，`propagate = False` 不污染 uvicorn）；秒表由 `run_chain` 持有、按 `run(clock=...)` 传入，引擎只填不打印（`owns_clock`），所以写词条与落库的耗时也在内。 | 不挂 handler，`logging.lastResort` 只写 WARNING 及以上，`progress.Timer` 算出的阶段耗时会被静默丢弃。 |
| 7.1.5 | 不要拿单轮数字下结论：节点延迟轮间 2.2–4s 波动，`eth_getLogs` 的调用次数也轮间不可比（9.1.4）。 | 见 12.20。 |
| 7.2.1 · 7.2.2 | 数据模型共 **11 张表**，以 `api/app/store/schema.sql` 为准：`chains` · `settings_versions`（不可变，一次改动一行 JSON）· `tokens`（CA 主档，含 `peak_market_cap_usd`）· `runs` · `run_items`（每币进出原因）· `snapshots`（不可变，每轮每币）· `run_summary`（含 `dropped_hard`）· `reports`（已发布的卡片文字，冻结）· `lexicon`（**本轮的链下调研**：五项分 ＋ 卡片文字，带 `run_id`）· `lexicon_sources`（这一轮调研读过的页面）· `llm_calls`（模型 / 耗时 / 成本 / prompt 版本）。加列走 `db.init_db` 的 `_ADDED_COLUMNS`（配 `SCHEMA_VERSION`），老库靠它补齐并回填。 | **别手改 `.db`** —— 老库不会自动长出这一列。 |
| 7.2.3 | 三种表不许改写：`settings_versions`（不可变，改设定 = 追加一行）、`snapshots`（每轮每币一写）、`reports`（落库即冻结）。 | 就地 UPDATE 已发布报告，历史轮次与当时的卡片就对不上。 |

# 8. 界面

## 8.1 报告卡

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.1.1 | 每轮张数 = `per_round`（默认 5），在「本轮总览」里横向一排。**不要求每张高度对齐**，但折叠状态下**所有卡片等高** —— `结论` 固定高度（10 行，`--verdict-lines`），放不下显示「…」、hover 出全文（`.verdict-pop`）。 | 折叠下高度不一，一眼扫读的排版就散了（约 12 张要能同时放下，允许超出一部分）。 |
| 8.1.2 | 「本轮总览」左侧一个按钮，一次开合全部卡片的两段折叠内容。做法是**不接管 `details`** —— 换 key ＋ 给新默认值（`key={quality-${tick}}` ＋ `open={openAll}`），让五张卡的两段 `<details>` 重挂一遍，各卡自己的开合用不着变成页面状态。 | 展开时结论显示完整、不用 hover；收起时回到固定高度、hover 看全文。 |
| 8.1.3 | 卡片宽度不随展开变化：栅格 `.grid` 是 `repeat(auto-fill,minmax(345px,1fr))`。 | 列数只由容器宽度决定。 |
| 8.1.4 | 前三名用金银铜背景区分（`--podium-1/2/3` 与 `--podium-1/2/3-bg`）。 | 只给名次数字变色，横排里看不出哪三张是前三。 |
| 8.1.5 | 「链上质量」与「链下调研」两块都**默认折叠**（同一套 `<details>`，各卡自己开合，一键开合见 8.1.2）。 | 默认展开就把卡片撑高，「结论在前、数据一键展开」这套重排就白做了。 |
| 8.1.6 | 卡片头部与身份行：头部是 `名次 · 标的 · 新进/续榜`（左）＋ `综合分`（右）；下面一条评分条把 `链上质量` / `链下调研` 画成两段（**不带 ×55% 这类占比文案**，两块靠两端对齐）；再一行是 CA（截断）＋ **复制 icon**（点击后显示「已复制」，与 CA 同字号）＋ 池内排名（`流动性 · 成交 · 持有人`）；行情行是 6d 折线 ＋ 市值。 | 给分项挂「说明性文字」就会换行（卡片窄）。 |
| 8.1.7 | 貔貅是判定不是数字：读 GoPlus 的 `is_honeypot`，输出「是 / 否 / —」三态带**色框**（`cards.honeypot_tone()` → `MetricOut.tone`；是＝`--danger-border`、否＝`--ok-border`、—＝`--warn-border`，直角、复用安全区 pill 的配方），CSS 必须写成 `.kv .kv-v.hb-*` 才压得住缺值行的灰字（两层的写法会静默失效）。 | 陷阱占比由「陷阱」chip 承担（`honeypot_pct`＝GMGN 的 `top_entrapment_trader_percentage`）—— 两者不是一个数，别把同一个值挂在两个名字下。 |
| 8.1.8 | LP 只分三态：已锁定 / 未锁定 / 未判定。显示「已锁」时**不跟括号解释**（标签本身就是答案）；未锁与未判定保留括号里的原因、多个原因用「·」分隔（理由自带括号时会套娃）。 | 反例：已锁还写「（LP 在合约里）」就是多一行解释（6.2.5）。 |
| 8.1.9 | 「链上质量」表**不印**开盘与总供应（快照里仍存 `open_at` / `total_supply`）、不印洗量（`wash_pct` 已从 `TokenMetrics` / `METRIC_KEYS` 删）、不印机器人 / 新钱包 / 私人金库 / 优质持有人 / 信号数（采集与快照保留，换链若真有数再显示）；**筹码分布与池子清单也撤了**（payload 里仍带 `safety.pools` / `safety.holders`，供归档与 API）。 | GMGN 在 Arc 上对这些一律回 0，一整行 0.0% 是噪声 —— 0.0% 的一整行印上去，有价值的那几行就被噪声淹没。 |
| 8.1.10 | 退出 chip 要说清是哪一条：不是「跌出硬过滤或排名」这种含糊话，detail 五选一（硬过滤的具体项 / 排名多少被挤出 / …）。实现在 `decide.py` 的 `_changes` —— 上下文（`items` / `pool_rank` / `per_round` / 生效的 `prefilter` / `safety`）在 `_round` 里本来就够得着，递进去即可。 | 只说「退出」，读者要自己猜哪条规则把它挤掉了。 |
| 8.1.11 | 「一轮那一条」（`.bhead`）＝ 日期步进 ＋ `扫描 → 淘汰 → 候选 → 入选` ＋ `已判定 / 未判定` ＋ 本期变化 chips（新进 / 退出），**一行到底**；`.bhead{margin:0 16px}` 与卡片同列。退出项显示**币的名称**不是 CA；日期步进的 label 也在这条里（`2026-09-28 17:00 +08` 与漏斗同一个 10.5px `--muted`），它不是标题；整条读数**同一字号与颜色**（10.5px `--muted`，不打粗、不放大）。 | 三块分割线画到边框上，整行就像被切成三个盒子。 |
| 8.1.12 | 日期控件属于报告页：与首张卡片对齐，外侧矩形控件与它之间留左边距。 | 放到全局顶栏，设定页上也会出现一个改了没用的控件。 |
| 8.1.13 | 展示层的链接一律「说明 ＋ 可点击词」（见 6.5.3），点开跳 `target="_blank"`（`web/src/components/Rich.tsx`）。 | 直接把 URL 印在卡片上就又长又不可点。 |

## 8.2 设定页

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.2.1 | 保存走 `settings_versions`（不可变，之后以表为准），出厂默认在 `config/chains/*.yaml`。**链与链不共享设定，同一条链的多套设定之间也不共享**。一次 PUT 是一张 dotted-path 的 patch，落到两层以上的组里要**深合并**（`settings._deep_merge`）—— 同一个组里改两个键，浅合并的 `{**a, **b}` 会把先改的那个整组覆盖掉。 | 被拒的保存要写成操作者能照着做的一句话：中文、带当前值、把 key 翻成页面上的行名（`forms.field_labels`，从页面自己的 `build` 派生，区间行读作 市值下限 / 市值上限），例如「链下调研五项权重合计要等于 100%，当前 110%（产品完成度 30% ｜ …）」；前端不翻译，只把 `detail` 印出来（`client.ts` 的 `errorText`，只有 5xx 才补 HTTP 码）。反例：做一份「全局设定」，一条链调松另一条就跟着变。 |
| 8.2.2 | 一条链一个 yaml 文件：文件里 `configs` 是这条链的一套套设定（各含 `schedule` / `pipeline` / `report` / `alerts` / `sources` 五段），链自己的事实（`explorer` / `rpc`）写在文件顶层、不进 `configs`。内部名字是 `arc:wide` 这种复合 id，落库是两列（`chain_id` ＋ `config_id`）。`default` 带 `locked`（出厂 label 叫「默认配置」，页面可改名 —— 锁的是条目不是那行字），任何链都留着、删不掉；其余各带 `enabled`，关掉就不排轮。 | — |
| 8.2.2 | **每条链同一时刻只跑一套**（`schedule.tick` 的 `busy_chains` 闸门，读的是本进程在跑的轮次，9.3.4）。 | 几套设定抢同一批源、同一批限流、同一个节点，并排跑只会让第一套更慢。 |
| 8.2.2 | 隔离落在哪：`settings_versions` 按 `(chain_id, config_id)` 各排一条版本线（版本号仍是链级的，一条链的历史读起来还是一个顺序），`runs` 带 `config_id`，`reports` 跟 `run_id` 分库，`lexicon` / `lexicon_sources` 的主键含 `config_id` —— 同一个 CA 被两套设定各判一次是**两条**词条，不是一条被覆盖；`previous_picks` 只认同一套设定的上一轮。不隔离的：`tokens` 仍以 CA 为主键、`peak_market_cap_usd` 仍是链级事实（「任何源报过的最高市值」与哪套在看它无关，共用是对的），`first_seen_run` 因此指向某一套的轮次，这是有意的。 | 反例：两套共用一份词条 → 后跑的那套把先跑那套卡片上的词条删掉（`forget_other_rounds` 当时一条 chain_id 过滤都没有）。 |
| 8.2.3 | 名字与 id 分开：`config_id` 是 `c` ＋ 6 位 base36（像 `c7k2ma`），建的时候随机生成，此后**永不改、永不复用、不上页面**；页面上写的一律是 `label`（≤20 字，链内唯一）。四个写手都在 `chains.py`（`POST` / `PATCH` / `DELETE` `/chains/{id}/configs[/{cid}]`）：这是改一个文件、不是保存一个值，所以不在 `settings.py`；`mode:"new"` 从 `default` 抄五段、`mode:"copy"` 从指定那套抄，**都不带报告库**（新的一套还没挣到东西），命名 `配置 N`（N 取最小空位，从 2 起）/ `<源> 副本`，规则只写在后端一处。 | 反例：把 `config_id` 当 label 用，改名就要动五个主键和每条 run 的 key，等于把整份报告库迁移一遍来换一个读法；让前端自己算「配置 N」则命名规则有两处。 |
| 8.2.3 | 新建的一律 `enabled: false` —— 加一套设定不该悄悄把链上正在跑的那套换掉；`enabled` 由保存路径写：`PUT /settings?config_id=` 除了存 patch 还调 `config.set_active_config()` 把那一套设为生效（按钮就叫**保存并生效**）。 | **每条链最多一个 `true`**：多过一个是数据坏了，`_keep_one_live()` 只留一个、把话写进 `ChainOut.warning`，链照常能读 —— 报错会让整条链打不开，而操作者才是能决定留哪个的人。 |
| 8.2.3 | 拒绝分三个码，各写给要动手的那个人：**400** 名字空或链内重名、**404** 没有这套、**409** 现在不能动（`default`、正在生效的那套、或有一轮在跑）；删除前要把 label **原样打一遍**（后端折空白后比对），对不上就 409。 | 反例：点一下就能过的「二次确认」不是确认。 |
| 8.2.3 | 删除连带清空这套的全部痕迹：`runs` → `reports` / `run_items` / `snapshots` / `run_summary`、`lexicon` / `lexicon_sources` / `settings_versions`，以及 `data/raw/<run_id>/`。**`tokens` 与 `llm_calls` 不动**。`config.forget_library()` 一个事务做完。 | CA 和「谁花过多少钱」是链级事实，留着才对账。 |
| 8.2.3 | 文件是**往返改写**不是重写：`ruamel.yaml` 的 `typ="rt"`（保注释、保引号、保 CRLF，`indent(mapping=2, sequence=4, offset=2)`，`width` 拉满不折行），临时文件 ＋ `os.replace` 落盘，全程一把 `threading.RLock`；`RoundTripRepresenter` 上必须注册 `None` 的表示器。 | 不注册 `None`，`holders_max: null` 会印成空值、每个文件都多出一行假 diff —— 注册之后 `arc.yaml` / `bsc.yaml` 往返**逐字节相同**。 |
| 8.2.3 | 页面上是两个**互不相干**的变量：顶栏里选中的那套决定**读哪份报告库**，设置页保存的那套才是**链上生效**的那套；两者不同是允许的（先去另一套看一眼再切过去）。切换器在顶栏这一排**最左边**，次序 `配置切换器 → 配置管理 → 保存并生效 → 立即运行`（两个「设定集」控件排前面，因为它们是后面两个按钮作用的**对象**）；它穿的是与旁边按钮同一套 `.btn.sec` 外壳：一个盒子、一个该套的标记（`Layers`）、名字、chevron。 | 它**不是**原生 `<select>`：原生控件的展开列表由系统画，深色页面上会得到一张白底蓝条的菜单而样式表伸不进去 —— 所以是按钮 ＋ 自绘 `role="listbox"`，顺手把「哪一套在跑」画成行里的 `生效` 标签和 `--ok` 圆点，点外面或 Escape 收起。**rail 只列链，不再列配置**。 |
| 8.2.4 | **参数设置页头整个撤掉**：链名和「正在编辑哪一套」都在顶栏，页头那个标题只是把页签「设定」再说一遍，「说明」也只是重复下面各块的话，`params-head` 的样式同批删掉。 | 代价是 `SettingsFormOut.note_detail` / `note_bar` 暂时没有读者，数据仍在 API 里。 |
| 8.2.4 | 解释折进「说明」icon（`GroupOut.detail` / `SettingsFormOut.note_detail`），判据是「这句是这一轮的**读数**，还是关于字段本身的规定」：读数留屏幕（本轮入选均值），规定进 icon；口径文案留在 `forms.py`，**前端不认识任何一条文案**（2.3.3）。 | 照这条搬进 icon 的有四项读数：**漏斗一行**、**逐行淘汰数**（`_drop_reading()` 汇总成 `本轮淘汰：…`）、**本轮违规**（`safety_warn`）、**扫描源每个台子贡献多少**（`_scan_reading()`，四个复选框从此只有开关没有数字）—— 行右侧挂一列数字只是在重复「这是规则」，而这些组本来就是判据不是旋钮。`GroupOut.note` 仍是「必须留在屏幕上的组尾读数」那个槽位（`.pset`，8.2.5），但七组现在都没内容（连漏斗也搬进 icon 了），所以页面上一个 `.pset` 都不画。 |
| 8.2.4 | 未保存计数（`待改 N 项`）是前端 draft 状态、后端不可能知道，所以不作为 `detail` 的一行，而是 `NoteHelp` 的 `lead` 参数（popover 第一行，`--warn-fg` 与脏状态同色）。 | 组头不再有独立的 `待改` chip。 |
| 8.2.4 | 块头只剩「图标 ＋ 标题」：`评分口径` 上那两个 `占 55%` / `占 45%` 占比 chip 与 `运行` 的 `不进分` chip 一并撤掉，占比改为两块 `detail` 的第一行（`链上质量占综合分 55%。` / `链下调研占综合分 45%。`）。`链上质量` 一行的三块排成 `评分口径` / `硬过滤` / `安全门槛`（另一个 `评分口径` 在 `链下调研` 行，各讲自己那半），三列宽度地板**随块走**（`app.css` 里 `data-blk="onchain"` 的轨道按这个顺序排），不是随位置钉死。 | 反例：标题下面挂一行解释，一屏里就全是灰字、读者找不到真正的读数。 |
| 8.2.5 | 块内等高（含分两栏）：`.blk-cols{align-items:stretch}` 花掉栅格已经算好的行高，一行里所有框下边齐平；空白从框外搬进框内，所以组尾读数（`.pset`）用 `margin-top:auto` 顶到框底。每块里最高的那一组**分两栏**：切片不是对半（`rows.slice(0, cut)`），切点落在语义边界上，阅读顺序「左栏读完再读右栏」，判据是「没有东西必须折行」而不是「这行短」；宽度不够时挤压其他块的列宽，不换行。 | 框高不齐就是一行里参差错落，读者会以为排版坏了。 |
| 8.2.6 | 读数一律靠右：没有淘汰格的分组也是「label 左、读数右」（`margin-left:auto`），不留例外。`推送与依赖` 的读数**允许折行** —— 它是最窄的一栏、值本身就是答案，长值绝不许把轨道撑出横向滚动条。 | 反例：用 `GMGN · Rad…` 这种截断当读数 —— 那不是读数。 |
| 8.2.7 | 复选框是直角硬线条：原生控件内部不吃 `border-radius:0`（圆角蓝块是 Chrome 画的 → 自绘），**与标题同一行、同一列起点**（`.prow.wide .field{flex:1 0 auto;max-width:100%}`，行本来就是 `flex-wrap:wrap`，缩不缩由行自己判断）；对勾是内联 SVG data-URL，浅色单独一条 `background-image`（自定义属性进不去 data-URL）。 | 反例：留原生的圆角蓝勾，就与全站直角冲突。 |
| 8.2.8 | 区间字段：`市值` 与 `持有人` 都是区间，`持有人` 的上限与下限共用同一条规则（见 6.1.7、10.4），所以淘汰计数、漏斗行、设定页那一行都不用改。设定页这边只多一条 —— 空格子必须能存回去：`config.coerce_like` ＋ `_infer`（空 → `None`，`2000` → `int`；已存 2000 再清空，`None` 要原样活过类型转换）。 | 反例：把上限写成 `0` 就是「一个都不留」。 |
| 8.2.9 | 链下调研块：五项（产品完成度 / 项目实质与亮点 / 团队与 dev 可信度 / 叙事与官方关系 / 社区活跃度）各 0–5，按 `scoring.offchain_factor_weights` 合成 `offchain_weight`（默认 45%）。这五行与链上那半边**同形**：都是百分比输入框、出厂各 20%（合 100），改一个就要把合计调回 100、否则 422，校验与 `onchain_factor_weights` 对称；每项判 0–5 的锚点与「数据来源：每轮全新调研」那句折进 `评分口径` 的说明 icon（锚点文案仍只写在 `forms.py` 的 `OFFCHAIN_LABELS` 里）。 | 反例：把五项做成只读说明，权重这类口径就只能靠改代码（10.8）；缺键当 0 权重会把没这个键的老快照的币全砍掉（要按等权合成 `scoring.offchain_weights`）；锚点留在行上，六行灰字把这一栏撑高，而这五条读一次就够。 |

## 8.3 侧栏与顶栏

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.3.1 | 左栏只渲染 `enabled` 的链：未接的整行不出现（不是变灰），链名旁不挂计数（入选数在「一轮那一条」里）；宽度 **260px**。链名取 yaml 的 `label`（`Arc 雷达` / `Robinhood 雷达`，同一个字串也是「配置管理」抽屉的标题），侧栏的 `text-transform:uppercase` 只改拉丁字母；**`label` 是雷达的名字，`name` 才是链自己的名字**（6.5.12）。 | 把没接的链渲染成灰行，就是每次都提醒读者「这里缺东西」。 |
| 8.3.2 | 报告页那一行：「读数（扫描 / 淘汰 / 候选 / 入选）」放在本期变化**上方**，「本轮总览」在**导出左侧**；三个区域的分割线不画到边框上（8.1.11）。 | — |
| 8.3.3 | 链自己的标记由 `Rail.tsx` 的 `chainIdMark()` 按 id 分发，一件 artwork 一个组件（`ArcMark` / `RobinhoodMark`，都取自 web3icons 的 `networks/mono`）；它是单路径**填充**图标（线重画会把记号画没），用 `currentColor` ＋ prop 尺寸（侧栏 14，与 12.5px 那一档一致），样式钩子是通用的 `.chain-mark`，深浅主题自动适配。 | 分叉逻辑写死在侧栏组件里，加链时就要改侧栏。 |
| 8.3.4 | 顶栏右端 = 当前页面的动作，同一时刻只存在一份：报告页 `本轮总览 › ｜ 导出 ｜ 推送 TG`，设定页 `配置切换器 ｜ 配置管理 ｜ 保存并生效 ｜ 立即运行`（占的是「本轮总览 / 导出 / 推送」的位置；管「哪一套设定」的两个写手排前面，见 8.2.3）。两页共用的 `运行结果` 不算在这两份里，它一直在这一排的最左端（8.3.7）。不可用的动作**灰显不可点**、`title` 写明原因；点下去会打开抽屉 / 面板的那个末尾带 chevron（`本轮总览 ›`、`配置管理 ›`）—— 那是这一排里「会离开本页」的唯一记号。 | 两页的动作同时列出来，读者就分不清哪个属于当前页。 |
| 8.3.5 | 中止按钮要**两次按压**（第一下变 `确认中止`，4 秒后自解除 —— 一次误触不能留下「下一轮上来第二下就停」的按钮）；请求在飞时换成 `正在中止`（转圈、不可点），图标 `Square`、颜色 `currentColor`。 | 一次按压就中止误触代价太大；图标用 `Pause` 与「正在中止」语义不符（9.2.6）。 |
| 8.3.6 | 主题切换在侧栏下方（黑 / 白两态，见 8.6.1）。 | 放到顶栏会与「当前页面的动作」抢位置。 |
| 8.3.7 | 动作行**最左端**是「运行结果」开关（`TopBar.tsx`）：它折的是整条顶栏**下面**那层面板（`RunPanel`），属于这一轮（和 `默认配置` / `展开卡片` 一样），不属于你正站着的那个 tab，所以排在动作行**第一个**、在 `默认配置` 左边；开着时 chevron 朝上、收起时朝下，`title` 写清下一步；这是**唯一**的折叠处 —— 面板自己没有关闭按钮，所以 9.2.8「停止的一轮把页面交回去」搭在它的**收起**那一半上（展开不挪人）；库里一轮都还没有时**灰显不可点**（与 8.3.4 同一条规矩）。 | 塞回 tab 组旁边，读者会以为它换的是右边那片内容 —— 它折的是整页下面那条。 |
| 8.3.8 | 刷新要落在原来那一页：链 / 配置 / tab 三个选择写 `ch.chain` / `ch.config` / `ch.view`（`prefs.ts`，与 `ch.theme` 同前缀），读法就是 `useState` 的初值，没有 `beforeunload`；记着的链已不在 `enabled` 列表里就回落到第一条 `enabled`（8.3.1）。报告**轮次下标不存** —— 它由开页那一次列表回答（9.2.8），存下来会和那份回答打架。 | 反例：把轮次下标一起存了 —— 下一轮跑完后刷新会翻开一个旧批次，而不是最新那轮。 |

## 8.4 字体、字号、字重、颜色

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.4.1 | 字体全站只用 sans：`--mono` 与 `--serif` 都是 `var(--sans)`（`tokens.css`），所以任何地方再写 `var(--mono)` 都是 no-op —— 想让 CA 恢复等宽，改这一行即可。等宽原本承担的对齐交给 `body{font-variant-numeric:tabular-nums}`（等宽是为了数字列对齐，不是换字族）；IBM Plex Mono 的 `@font-face` 与 woff2 已全删。 | 正文 sans ＋ 数字 mono 的混排会让一屏里出现两套字形。 |
| 8.4.2 | 字号只有 8 档：`9 / 9.5 / 10.5 / 11.5 / 12.5 / 15 / 19 / 21`（`9` 只有 `.dsec-badge`）。按钮与表单控件 11.5（`.btn` / `.note-help-btn` / `.step` / `.panel-h .x` / `.ok-btn` / `.frow input` / `.prow input` 同档）；图标按钮 9.5（`.ca-copy`）；表格正文 10.5（表头与脚注 9.5）；块标题三层 —— 面板 / 大块 12.5、子组 11.5、卡片内块 10.5，行标签 11.5，顶栏 `h1` 15。 | 随手写 `13px` 就是字号表之外多出一个值，之后没人敢改。 |
| 8.4.3 | 字重只用 400 / 500 / 600，`b` / `strong` / `h1`–`h6` 一律 600。 | CSS 不写就落回浏览器默认 700，会出现「同为小标题、一个字更黑」。 |
| 8.4.4 | 颜色只有一条来源 `tokens.css`，页面里不写字面 hex：文字色用 `--fg` / `--fg-2` / `--muted` / `--muted-2`；`--accent` 只做填充与边框，**文字用 `--accent-fg`**；状态文字用 `--ok-fg` / `--warn-fg` / `--danger-fg`（`--ok` / `--warn` / `--danger` 给填充与边框）。 | 组件里写 `#c00`，浅色主题下就是看不清的深红。 |
| 8.4.5 | 直角是全局的（`*{border-radius:0!important}`），新控件不要再引入圆角；原生控件内部不吃 `border-radius`（8.2.7）。 | — |
| 8.4.6 | 浮层用卡片那一套色：`.verdict-pop` / `.note-help-pop` 底 `--surface-warm`、字 `--fg`、边 `--border`，比所在那层底浅一档；hover 里文字仍是默认文字色（不要变蓝）、**横向永不出滚动条**（`white-space:normal`，长值折行）。 | 出现横向滚动条，就是某一行的长值把弹层撑破了。 |

## 8.5 图标与 Logo

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.5.1 | 图标只有一套：`lucide-react`（只进用到的）＋ 组件 `web/src/components/Icon.tsx` ＋ 一条规则 `.ico{display:block;stroke-width:1.5;stroke-linecap:square;stroke-linejoin:miter}`。**几何在样式表里否掉 lucide 的默认**（24 格、描边 2、圆角端点与连接）—— `stroke-*` 是继承属性、样式表声明赢过表现属性，所以后加的图标不可能漏掉，调用点也不用重复传 prop。 | 直接在组件里用 lucide 原组件就会混进圆角端点与 2px 描边。 |
| 8.5.2 | 尺寸三档、颜色一律 `currentColor`：不新开色值，自动适配主题，**icon 大小跟随旁边的文字**。 | 比旁边文字大一圈会把行高撑开。 |
| 8.5.3 | Logo 是 `web/src/components/Mark.tsx` 的雷达扫掠（方框 ＋ 一道扫掠 ＋ 一个回波点），用 `createLucideIcon` 声明以便走 `Icon`（`.ico` 那条规则与 `currentColor` 自动套上）。侧栏里它与 `CRYPTO HUNTER` 同行、大小适配（**下方不留横线**）；标签图标只用同一个记号、不铺底色、跟主题走。 | 给 Logo 单独铺一块底色，浅色主题下那块底色比周围还深。 |

## 8.6 主题（深浅两态）

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 8.6.1 | 两套 token 都在 `tokens.css`（`:root` 深、`[data-theme="light"]` 浅），持久化只写 `ch.theme` 一个键；`index.html` 里一段内联脚本抢在首屏前设好 `data-theme`（`try/catch` 包住 `localStorage`）。 | 只在 React 里设主题，深色用户每次刷新会先闪一帧白。 |
| 8.6.2 | 浅色不是反色：链接 / 状态文字色、四种 `--*-bg` 淡底、金银铜、`--shadow` 都按浅底重取，状态文字色要按**它能落到的每一个底**算对比度（≥18px 按 3:1，其余 4.5:1）。 | 直接 `invert()` 会把图表与照片一起反掉。 |
| 8.6.3 | 浅色下 `--bg` 是页面底、`--surface` 是面板底，**右区与左栏整体互换**：左栏（菜单、分组与行、输入框与下拉、运行面板及头脚、复制 CA 芯片）跟 `--surface`，**报告卡片也重新提出来跟 `--surface`**（`proto.css` 里 `--surface` 有几十处引用，卡片只是其中之一），没有一处写死颜色。 | 写反了就是右侧一片灰、视觉重心跑到侧栏上。 |
| 8.6.4 | 运行面板的边框用 `--border`（与它正下方的分组同一个 token）。 | 用 `--fg` 画边框，浅色下就是一只纯黑盒子 —— 它是全屏唯一用正文墨色画边框的地方。 |
| 8.6.5 | 运行面板跑完**不收起**：六行步骤一直可见，失败 / 已中止 / 跑完三种终态一视同仁；面板自己**不带关闭按钮**，折叠只有顶栏那个 `运行结果` 开关一处（8.3.7）；每行右列是那一阶段自己的实测墙钟（`RunPanel` 的 `took()` 把 `batch.phase_timings` 里属于该行的 phase 相加，`STAGE_PHASES` 定义归类，例如「生成报告」= `draft` ＋ `write`、「硬过滤」= `prefilter` ＋ `topup` ＋ `gate`）。 | 早期版本跑完自动收起，等于把「这一轮时间花在哪」写出来又立刻藏掉。 |
| 8.6.6 | `tokens.css` 里 `button,input,select,textarea{font-family:inherit}` 是必需的兜底。 | UA 样式给控件直接设了字体族，不兜底输入框里就会冒出一个不是全站字族的字体。 |
| 8.6.7 | 已知残留：深色状态边框（貔貅三态框 / pill / chip）是 2.29 / 2.37 / 1.63:1，低于非文字元素 3:1 的建议线，**没有为了对齐浅色去改它**（见 10.6）。 | 悄悄改深色 token 去凑 3:1 会让三块「有值」的底变得抢眼。 |

# 9. 运行与验证

## 9.1 读路径

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 9.1.1 | 单次上链请求有硬地板 2.2–2.5s，与查什么无关（`eth_getCode` 与 `eth_call` 同量级）；并发几乎免费（1 / 4 / 8 / 12 并发 = 2.16 / 2.37 / 2.88 / 3.56s）；批量省（两次请求单发 4.61s、打包成一个 JSON-RPC 批量 3.54s）。**一条链只有一个 RPC 端点，没有备份。** | 靠加并发把链上段从 30s 压到 5s 做不到，硬地板在（数字见 12.7）。 |
| 9.1.2 | 提速只做「把本该同时问的一次问完」：`rpc_batch(chain, calls)`（回包按 id 映射回顺序；非 list 回包 = 整批重试，重试耗尽逐项 `None`；同一套 `tally` 记账）、`nft_balances(chain, managers, who)`、`contracts_of(chain, addresses)`（批量 `eth_getCode` 判别 EOA）、`_pool_logs` 的窗口并发（`LOG_WINDOW_WORKERS = 4`，窗口互不相交、`pmap` 保序）、`decide.py` 的 `ENRICH_WORKERS = 8`（6.2.9）。 | 并发不保序会回包错位，所以按 id 映射。 |
| 9.1.3 | 效果如实报：RPC 请求数 40 → 30 / 轮（唯一干净可比的数字，≈ −25%），`gate` 36–44s → 29s；等待时间基本没改善（次数降 25%，但单次延迟轮间能从 2.2s 飘到 4s，叠上 retry 就吃回去了）。阶段耗时日志在 `pipeline/progress.py`（12.6）。 | 反例：把 −25% 的请求数说成「快了三成」。 |
| 9.1.4 | `eth_getLogs` 的调用次数轮间不可比：`_logs_in` 在节点拒绝窗口宽度时对半递归，拒几个窗口由节点侧定（7.1.5）。 | 看整轮的 `ok=` 总数，别用单次 A/B 的调用次数下结论。 |
| 9.1.5 | 30 次 / 轮里没有可去的重复调用（探针证明一轮内没有重复读同一个 CA）；要再压只能减少「哪些读是必需的」，例如把 `lock_facts` 的部分读挪到入选后的 5 个标的上、而不是过关池里每个候选都读（10.7）。 | **未擅自改，等拍板。** |

## 9.2 中止一轮

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 9.2.1 | 口径是**只在进程内存里立一面旗**（`api/app/cancel.py`）：`POST /runs/{id}/cancel` 立刻返回 —— 一轮跑在 `schedule.py` 的单线程 worker 上，HTTP 处理器够不到那个线程，所以不进数据库；轮次自己在下一个检查点读旗、抛 `RoundCancelled`。 | 旗按 run id 存、轮次结束就清 —— 写进数据库的话，重启后上一轮的停止请求还在，下一轮会继承。 |
| 9.2.2 | 一轮没有回滚可做，这才是它简单的原因：落库只有最后一笔事务（`store_round` ＋ `store_reports`），停在哪就是停在哪 —— 已问出去的源、已判过的标的留在日志与 `run_summary` 里，**报告一张也不写**。 | 搞一套「回滚」，复杂度全花在不需要的地方。 |
| 9.2.3 | 检查点只用轮次本来就有的边界，**没为中止新增计时器或线程**：`collect.py` 两个源之间；`decide.py` 每个阶段边界、`_gate` 的批次循环头与**每个标的**、picks 那次兜底 enrich 之前；`research.py` 每个币的词条之间（一个币要么有词条要么没有，停在两个币之间不会留半条）；`llm/client.py` 一次调用的前 / 后与**读响应体的每一块之间**。 | 加一个每秒轮询的计时器只会多一个线程、还让每轮变慢。 |
| 9.2.4 | 在途的模型答案可以放弃，但**不能退化成 `LLMUnavailable`**：`client.stream` 按块查旗，命中就关连接抛 `RoundCancelled`；关键是 `complete()` 里单开 `except cancel.RoundCancelled: raise`。 | 退化的话，一轮会用「没写词条」的样子把报告发出去。 |
| 9.2.5 | 排队中的那一轮是唯一例外：没人开始跑就读不到旗，而 `schedule.recover` 会把停在 `pending` 的轮次再交给 worker —— 所以这一种由路由直接落库（`run.stop_run`），`run_chain` 对已是终态的轮次直接返回。 | 只立旗的话，队里那一轮永远不会停。 |
| 9.2.6 | 响应时间是秒到二十秒的量级：拉取段**秒级**；补齐链上数据与安全判定是最慢的一段 —— 补 60 行 37s、判 60 行 94s（8 并发，每行 ≈ 1.5s；实测 Robinhood 2026-09-30），因为判定默认判**整个过关池**（6.2.8）；写作段在下一次响应块到达时（单位是秒）。 | 不要承诺「点一下立刻停」。 |
| 9.2.7 | 故意留的窗口：写库之后不再有检查点 —— 停止请求若正好在最后一笔事务提交之后到达，这一轮照常发布。 | 在事务里插检查点，报告就会半写半不写；已发出的报告不该被改写成「已中止」。 |
| 9.2.8 | 前端：中止后与跑完同一处理。`已中止` 的面板标题是 `已中止 · 本轮`，步骤列表照常显示（已结束的阶段有耗时，没走到的画 `—`）；顶栏开关的**收起那一半**退回**最近一轮有卡的批次**（`App.tsx` 的 `closePanel`：最近一轮里第一个 `selected > 0`），**展开那一半永不挪动读者**；刷新同样退回那一轮（`App.tsx` 开页的第一次回答）；没有卡片结果时不留白页。 | 刷新后跳回那个没有结果的批次就是这么来的。 |
| 9.2.9 | 新状态 `cancelled`：不是故障就不该记成 `failed`，`states.TERMINAL` 含它（测试 `test_cancel.py` / `test_llm.py` / `test_states.py`）。 | 记成 `failed` 会让报表里「失败率」虚高。 |

## 9.3 状态机

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 9.3.1 | 状态链 `pending → collected → scored → selected → written → researched` | 任何在跑的状态都可以跳到 `cancelled` / `failed`（`api/app/pipeline/states.py`）。在 `written` 之后再排一个异步阶段，报告已发布却还会被改写。 |
| 9.3.2 | 「一轮结束了」= 到了终态 | `TERMINAL = {researched, cancelled, failed}` 决定 `finished_at`、页面停表；`written` 之后一步就是终态（`_finish()` 推 `written → researched`，没有推送出口要等，所以 `researched` 既是「文字已落库」也是「本轮结束」）。把 `written` 也当终态，推送那一步没走完就停表。 |
| 9.3.3 | 展示口径的锚点 | 文字落库一次（`reports.payload_json`），落库之后不再改写；落库那笔事务之前，任何阶段失败都可以重跑。卡住的一轮靠**状态机上的重试与补发**恢复，不另起看门狗脚本 —— 另写一个 watchdog 扫库改状态，就是两个写者抢同一行。 |
| 9.3.4 | **「正在跑」由执行器说了算，不翻 `runs` 表**：`schedule._live`（run id → 链 ＋ 配置）在 `submit` 时写入、future 完成时删掉；`busy_chains` / `live_configs` 只读它。「任何非终态的行都算在跑」是有毒的：进程死在半路会永久留一条非终态行，于是每条链永远有一轮在跑，ticker 再也不发轮 —— 2026-09-30 实测，两条链都 busy，11 小时零个排期轮。 | 生产是一个进程一个端口（`main.py`），本进程的轮次就是全部真相；要上多 worker 就得换成带心跳的租约。 |
| 9.3.5 | 开机收尾（`recover()`）：`pending` 重交，**其余非终态一律收尾**（`close_abandoned`）—— 存过 `reports` 的收成 `researched`（它确实写完了），没存过的收成 `failed` 并写明「进程重启，本轮已中断」。 | 不收尾，页面就永远显示「正在运行」并一直走表。收尾只在开机跑一次、在任何 worker 之前，所以不违反 9.3.3。状态机不认识的旧状态（`sent`）没有合法迁移，直接写库收尾 —— `states.can` 管的是在跑的轮次，不认识的状态不可能在跑。 |

## 9.4 工作方式（和这个用户合作）

| 编号 | 规则 | 说明 |
| --- | --- | --- |
| 9.4.1 | 他说「我们先讨论」时，只出判断和方案，**不动代码** | 没让改的别改 —— 把「先讨论」当成「讨论完直接实现」就是踩这条。 |
| 9.4.2 | 一次只动该动的 | 不顺手重构、不修无关的 bug（可以在汇报里提一句，由他决定）—— 顺手删掉「看起来多余」的一段，它可能是某个口径的兜底。 |
| 9.4.3 | 不 `git commit`、不建分支，除非他明确要求 | 也不要自动提交一版「阶段性成果」。 |
| 9.4.4 | 改动贴现有风格 | 直角（`border-radius:0!important` 全局已定）、颜色只从 `tokens.css` 取、文案口径跟 `settings/forms` 走（2.3.3）；带圆角的新卡片就是反例。 |
| 9.4.5 | 汇报短，先结论后数据 | 先说结论，再给实测数字与 `文件:行`；UI 改动附截图。他对 UI 细节极其挑剔，一处一处改、改完复述「改了什么 ＋ 实测多少」；别按时间顺序写成流水账。 |
| 9.4.6 | 用中文沟通 | 代码注释与标识符按现有习惯（注释英文、文案中文）；报告正文里混英文句子是反例。 |

## 9.5 还没接的部分（别当成 bug）

| 编号 | 事项 | 说明 |
| --- | --- | --- |
| 9.5.1 · 9.6.4 | TG 推送 | `api/app/out/` 是占位模块（`out.configured()` 返回 `False`），一轮跑到那步写「未配置（跳过推送，报告已落库）」并正常收尾，**不阻塞**；顶栏「推送 TG」与「导出」都带 `disabled`、按不动 —— 导出**已经接上下载**（`web/src/App.tsx` 的 `onExport`），只是按钮还锁着，推送完全没接。推送是落库之后的可选动作，将来接出口改的就是 `configured()`，流程不动；把「未配置」当成失败，整轮会卡在「正在运行」。 |
| 9.5.2 | 一键重算 | `POST /settings/recompute` 返回 501，因为一轮的 universe 没落库；前端以为它可用就会点了没反应。 |
| 9.5.3 | 搜索没配 cookie | `config/search.env` 空着时 X 搜索整层静默失效（`discover` 返回空），卡片只用本轮登记的链接，**不阻塞流程**（6.4.3）。把「没配 cookie」当报错，没有链接的币也进不了报告。 |
| 9.5.4 · 9.5.5 | 两条「看起来没接、其实是有意的」 | 链下调研没回来的标的本轮不参与排名（不是 0 分，见 6.3.4 / 6.5.11），`holders_max` 默认 `null` 就是不设上限（见 6.1.7 / 10.4）。 |

## 9.6 进度与完成态

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 9.6.1 | 进度按段额度填 | 进度条只有五段（`progress.py` 的 `PHASES`，与 2.4 那九个时间线键是两套粒度），且五段不等长 —— 额度是两条链实测墙钟的加权占比（12.27），不均分（按段均分会先躺二十分钟再跳一格）。每段占一段额度、段内按**实际完成的工作量**填充。额度按**阶段走的顺序**排（`pull` → `topup` → `gate` → `draft` → `score`），所以柱子本来就只会向前走。改阶段就得回来改下面那张表。 |
| 9.6.2 | 进度只增不减 | `_set_progress` 落库用 `MAX(progress, ?)` —— 段内额度是估的，回跳比走得慢更伤信任；`percent` 单调，`progress_label` 仍如实跟着阶段变。`runs.progress` / `runs.progress_label` 存最新值，前端 `RunPanel` 顶部显示 `NN% · 阶段名`；测试 `test_the_bar_never_walks_backwards` 守这一条。 |
| 9.6.3 | `sharpen` 与 `write` 不占额度、只记日志 | 为一两秒的阶段切一条进度带，只会让已有的五段更窄。阶段耗时每轮打一行 `round <id> phase timings: ...` 进日志；**数据库只留结论、不留耗时，日志是唯一能复核这些额度的地方** —— 给 `write` 也切一段，就会卡在 99% 等落库。 |

| 进度段 | 额度 | 段内单位 |
| --- | --- | --- |
| `pull` | 0–19 | 每个扫描源拉完一次 |
| `topup` | 19–30 | 每补一个候选的链上数据 |
| `gate` | 30–51 | 每判完一个候选 |
| `draft` | 51–96 | 每调研并写完一个币的词条 |
| `score` | 96–100 | 一次 |

## 9.7 数值显示

| 编号 | 规则 | 说明 / 反例 |
| --- | --- | --- |
| 9.7.1 | 分数只印整数 | 库里和接口里都带一位小数（`report/cards.py` 的 `_round`，打分条要用这个小数画长度），**页面印出来的一律四舍五入到整数**，且只有一个出口 —— `web/src/format.ts` 的 `whole()`：卡片的 `综合分` / `链上质量` / `链下调研`、本轮总览落选池的 `评分` 列、导出 markdown 的头部；后端只剩设定页那句实测均值（`settings.py` 的 `_fmt`）。**四舍五入是半进位**（70.5 读 71）：Python 的 `round()` 是半偶进位、会读成 70，两边不许混用，所以 Python 侧写 `math.floor(x + 0.5)`。`toFixed(1)` 是反例 —— 70.9 与 71 是同一个分数的两种写法，一屏里出现两种就不像同一套数。 |

# 10. 待决（都没擅自改）

**想动这里面的任何一条，先问用户。**

| 编号 | 待决 | 现状与可走的路 |
| --- | --- | --- |
| 10.1 | 硬过滤的松紧 | 现值见 6.1.5 – 6.1.7（比参考实现紧，它的下限是 0）。两头一起收窄是为了同时挡住「微市值 ＋ 零数据」和「涨上去的老盘」；代价是少掉两头的新面孔 —— 松紧随时可在设定页改，所以这不算错，只是没拍板。 |
| 10.2 | 同一行快照里 `market_cap_usd` 与 `price_usd × 供应` 对不上 | 两个端点各缓存、TTL 不同步，一行拼了两个时刻的数。影响只在「距高回撤」的读数（判定逻辑不受影响，6.1.13）：①源高点与②库记录高点都以 `market_cap_usd` 当 `now`，刻度不自洽会让跌幅偏深或偏浅，也让设定页「未判定」偏高。三条路 —— 改用 `price_usd` 当序列 / 把市值那列的 TTL 与价格对齐 / 先修自洽再谈阈值。 |
| 10.3 | `num` ＋ `enabled_key` 行的 `开启` 标签折成两行 | `.chk` 被 `flex:0 1 auto` 压扁到 29×31px，与复选框长相无关。三条路 —— `.chk{white-space:nowrap}` / `开启` 缩成 `开` / 该列不可收缩。 |
| 10.4 | 持有人上限默认留空（＝ 不设上限，6.1.7） | 给一个数就等于凭空开始砍币，而「市值 1–20 万的币持有人能到多少」在 Arc 上没实测过。 |
| 10.5 | worker 线程卡住（进程还活着）时，那一轮和「正在跑」长得一样 | 「进程死在半路」这一半已修（9.3.4 / 9.3.5）；剩下的是进程活着、线程卡在某个不返回的网络上，现在没有任何超时能发现它。要修得给轮次加心跳（`_live` 里带时间戳），代价是每个阶段都得写一次。 |
| 10.6 | 深色主题的状态边框对比度不达标（2.29 / 2.37 / 1.63:1，8.6.7） | 要达标就把 `--ok-border` / `--warn-border` / `--danger-border` 各提一档。 |
| 10.7 | 链上读路径要再压，只能减少「哪些读是必需的」 | 30 次 / 轮里没有可去的重复调用（9.1.5）。 |
| 10.8 | 链下调研五项的边界 | 第 2 项（项目实质与亮点）与第 4 项（叙事与官方关系）的界线、粗筛条件、`per_round` 取值、判定细则、链下调研块的其他参数（8.2.9）。**权重已定**，不在待决里：五项各有百分比旋钮、出厂等权 20%。 |
| 10.9 | 分位分母是混口径的：只有被逐币补齐的那批行带市场口径（DexScreener），池子里其余行还是粗筛口径 | 补齐不再受 `gate_limit × ENRICH_POOL_MULTIPLE` 截断（默认补整个过关池），但**只问缺筹码字段的行** —— 粗筛行里筹码已齐的行不会再问一遍 DexScreener，分位分母仍是混的（实测 97 个幸存者的口径分布：gmgn 58 / dexscreener 34 / tolly 5，12.26）。两端流动性中位差 1.11 倍、最大 10 倍（12.23），所以同一个分位里既有市场口径也有粗筛口径，流动性分位会随「这行补没补」漂移（实测中位 0.041、p90 0.32、最大 0.73，12.24）。三条路 —— ① 缺筹码的行也补一遍 DexScreener 行情（`MARKET_ENRICH` 把 gmgn 也带上，全池走会把 GMGN 那半边也拉到全池，要只买行情得给 `_top_up` 单开一条只带 dexscreener 的路）；② 按口径切开各算各的分位（分母变小，可比性反而更差）；③ 认了它并写进文档（现状）。**动它要重跑一轮对比，别顺手改。** |

# 11. 附录：为什么这么定

每条规则都是被某个具体的错纠出来的。这里只记「曾经怎么错」，**现行口径看第三列指到的数字地址**。

| 编号 | 曾经的错 | 现在写在哪 |
| --- | --- | --- |
| 11.1 | 淘汰数按各行独立计数，加起来不等于 `dropped` | 6.1.12 |
| 11.2 | 距高回撤要求三个读数一致 → 169 个已跌够的币一个没淘汰，ARCAT 漏进报告 | 6.1.13 ｜ 12.4 |
| 11.3 | 库记录高点从不回写 → 那一半永远只是下界，「只报未判定、从不动手」 | 6.1.13 |
| 11.4 | gate 曾买池面读数 → 被 GeckoTerminal 打限流，gate 54s 涨到 242s | 6.1.13 ｜ 6.2.10 ｜ 12.5 |
| 11.5 | 硬过滤曾是一遍 → 未知等于放行，上千个发射台自报的行直接进池 | 6.1.14 |
| 11.6 | 补齐曾用全量 `fetch_token`（四个端点）→ 29 行 53.1s | 6.1.14 ｜ 12.3 |
| 11.7 | 短名单排序键曾用成交额 / `min(流动性, 成交额)` → 刷量的浅池把深池挤掉 | 6.1.11 |
| 11.8 | 词条曾跨轮复用（2 天过期 / 市值波动 40% / 6h 跳过窗口 / 人工确认） | 6.4.2 |
| 11.9 | 「一次调用写完所有条目」已废弃 —— 必然撞天花板 | 6.4.7 |
| 11.10 | 词条看不到安全块 → 永远写「安全块三项全部未判定」 | 6.5.8 |
| 11.11 | 卡片曾一边显示「LP 已锁」一边在词条里写「三项全部未判定」 | 6.5.2 |
| 11.12 | 运行面板跑完自动收起 → 把「这一轮时间花在哪」写出来又立刻藏掉 | 8.6.5 |
| 11.13 | 空壳官网静态抓只判出 none → `usearckit.online` 的卡片上挂了第三方站 `myarcade.fun` | 6.4.6 ｜ 12.11 |
| 11.14 | 池面读数读失败时静默放行（而不是记未判定） | 6.1.13 |
| 11.15 | 税率曾用排行榜行 / GoPlus 的占位 0 → 报告里全部税率都是假的 | 6.2.4 ｜ 6.6.7 |
| 11.16 | `source_verdict` 的 `dual` 曾写 `and` → 在 Sourcify 收录很差的 Arc 上一票否决 | 6.6.4 |
| 11.17 | 曾用单次 A/B 的 `eth_getLogs` 调用次数下结论 → 波动全来自节点 | 9.1.4 |
| 11.18 | 推理模型的思考计入 `max_tokens`，额度给小了返回**空消息**（不是报错）→ 客户端把空消息当失败，静默降级成「没有文字」 | 6.4.8 |
| 11.19 | `CH_LLM_TIMEOUT` 180s × `CH_LLM_TRIES` 2 → 一次死掉的调用独占 366s（那一轮 draft 421s） | 6.4.8 |
| 11.20 | 第三方渲染 `r.jina.ai` 与 `networkidle` 等待策略都试过并放弃 | 6.4.6 ｜ 12.12 |
| 11.21 | 设定 patch 在两层嵌套组上做浅合并 → 同一次保存改两个权重只留下最后一个 | 8.2.1 |
| 11.22 | DexScreener 的四个市场数在 `FIELD_PRIORITY` 里一个都没列 → 一轮又一轮地花钱问、问完丢掉（17,573 张快照的 `provenance` 里它只赢过 `socials` / `websites`） | 6.1.3 ｜ 6.2.1 ｜ 12.23 |
| 11.23 | GMGN 榜单的 `volume` / `price_change_percent` 跟着查询窗口走，下游一律当 24h 记 → 5m 榜的 $47 被当成「一天成交 $47」淘汰（CORTEX 真实 24h $14,549） | 6.1.9 ｜ 12.22 |
| 11.24 | 流动性 / 24h 成交的下限跑在不含逐币读数的第一半 → 判的是没校准的读数（Tolly 深度可以差 20 倍、GMGN 的成交是别的窗口） | 6.1.3 ｜ 6.1.8 |
| 11.25 | 安全判定曾只判按流动性取的前 20 个候选 → 深而脏的行吃光名额，健康币从未被问（Robinhood 2026-09-30 一轮：判过的 20 个里 17 个是 LP 未锁 / 陷阱 / 回撤淘汰，dotswarm 按流动性第 29 名、一张卡没出） | 6.2.8 ｜ 12.27 |
| 11.26 | 进度五段的额度是照「gate 里还在买池面读数」那会儿的墙钟定的（`gate` 拿走 14–82） → 判全池、写作又变长之后，`gate` 只占整轮 18%、`draft` 占 60%，柱子会在 86% 一停五分钟 | 9.6.1 ｜ 12.27 |

# 12. 附录：实测台账

正文里的每个数字都在这里有一行；**改口径时把旧数字一并改掉**，别留两套。

| 编号 | 实测 | 现在写在哪 |
| --- | --- | --- |
| 12.1 | RadarDEX 的市值口径与 GMGN / GeckoTerminal 不是一回事（拿 `sort=mcap` 当全链排名会错） | 6.1.1 |
| 12.2 | 全链口径：GeckoTerminal 分页全量 ≈ 3249 个池/币，RadarDEX 一次拉全量覆盖 ≈ 4145；Arc 上 85% 的币停在发行时的 5000–9000 U 初始 FDV（这就是市值下限压到 1 万的依据） | 6.1.1 ｜ 6.1.5 ｜ 6.1.6 |
| 12.3 | 补齐 29 行：全量 `fetch_token` 53.1s、轻量 `fetch_chips` 13.6s；worker 开 8 / 16 / 24 都是 13.6s（已到每主机节流下限，加并发无用） | 6.1.14 |
| 12.4 | 169 个已跌够的币一个没淘汰；ARCAT：库内高点 $266K、GMGN 源高点 $5.05M、现市值 $113.8K → −97.7% 直接淘汰 | 6.1.13 |
| 12.5 | gate 一次问二十个候选被 GeckoTerminal 打限流：`ok=24 fail=18 retry=78`，gate 54s → 242s | 6.1.13 ｜ 6.2.10 |
| 12.6 | RPC 请求数 40 → 30 / 轮（≈ −25%）；`gate` 36–44s → 29s | 9.1.3 |
| 12.7 | 单次上链请求 2.2–2.5s；并发 1 / 4 / 8 / 12 = 2.16 / 2.37 / 2.88 / 3.56s；同样两次请求，单发 4.61s vs 打包成 JSON-RPC 批量 3.54s | 9.1.1 |
| 12.8 | 79 次 research 调用 p90 = 147s、最长成功 296s | 6.4.8 |
| 12.9 | 思考 `none`：94.9s / 5 个词条全过 / 正文 8711 字符；端点默认：340.1s / 只过 4 个 / 正文 6199 字符。旧代码（无重问）`none` 只过 3/5 | 6.4.8 |
| 12.10 | 找链接并发：15 个并发跑 20 个标的零 429；20 个并发有 3 个 429 而且更慢 | 6.4.7 |
| 12.11 | 空壳页面：`memespad.com` / `www.arcdex.online` / `usearckit.online` 静态只有 8–10 个词、连 CA 都搜不到，渲染后是 377–827 个词且两个带 CA | 6.4.6 |
| 12.12 | 浏览器单页 2.4–12.0s；`networkidle` 在 memespad 上是 21.8s | 6.4.6 |
| 12.13 | `SHELL_WORDS = 40`；同一批次里次低的官网有 94 个词（两边差得很开） | 6.4.6 |
| 12.14 | 补齐一次 45.5s（改轻量后约 14s）；候选池里持有人 < 100 的行 16 → 0 | 6.1.14 |
| 12.15 | Tolly 全族自报深度是虚的：5.52 倍（所以 DexScreener 同批必问） | 6.1.14 |
| 12.16 | 池子 80 个候选里混着持有人 2 / 3 / 4 的行 | 6.1.14 |
| 12.17 | 判完 163 个标的要 20 分钟（按 GeckoTerminal 逐币 2.2s 节流、加线程无效算 —— 这是 gate 不买池面读数、把它推到入选后（6.2.10）的依据） | 6.1.13 ｜ 6.2.10 |
| 12.18 | GeckoTerminal 逐币端点 2.2s/次节流；GMGN 0.45s/主机节流 | 6.2.8 ｜ 6.2.9 |
| 12.19 | 思考关掉后 research 单次 11–17s（300s 只剩兜底作用） | 6.4.8 |
| 12.20 | 节点延迟轮间 2.2–4s 波动，`eth_getLogs` 的调用次数也轮间不可比 | 7.1.5 ｜ 9.1.4 |
| 12.21 | 一轮有九个「阶段」键（耗时日志与运行面板用）与五个进度段，两套名字很容易被当成一套 | 2.4 ｜ 9.6.1 |
| 12.22 | GMGN 排行接口 `/rank/arc/swaps/{period}` 只返回一个 `volume`，跟着窗口走：同一个 CORTEX 读到 47.58（5m）/ 59.56（1h）/ 9386.81（6h），两个独立源给的 24h 是 14,549；40 个币跨窗口比对：`volume`（131 倍）、`price_change_percent`（符号都反）、`swaps` / `buys` / `sells` 是窗口量，`market_cap` / `liquidity` / `price` / `holder_count` 只差采样抖动（0.01%） | 6.1.9 ｜ 11.23 |
| 12.23 | 同一批 Arc 币三个源的口径比：市值 DexScreener / GMGN 中位 0.98（n=24，0.92–1.03），Tolly / GMGN 中位 1.00 但 4.7% 的行差 2 倍以上（n=85，最多 10 倍）；流动性 DexScreener / GMGN 中位 1.11（n=24，1.00–10.23），DexScreener / Tolly 中位 1.89（n=42，0.09–9.99）—— Tolly 的偏差没方向：发射台行上高 5.52 倍（12.15），排行行上低约一半 | 6.1.3 ｜ 6.3.1 |
| 12.24 | 97 个幸存者的流动性分位，同池对比「改口径前 / 后」：中位差 0.041、p90 0.32、最大 0.73（43 行差过 0.05）—— 这一改会整体重排池子 | 6.3.1 ｜ 10.9 |
| 12.25 | 一轮 165 个池子行里 106 行需要逐币补：`gate_limit: 0` 时 106 行全进补齐；旧的 `gate_limit: 20` 只有前 60 行进（20 × `ENRICH_POOL_MULTIPLE` 3）。DexScreener 逐币读 0.55s/次（24 次 13.3s，已是节流下限） | 6.1.9 ｜ 10.9 |
| 12.26 | 补齐后的 97 个幸存者，流动性口径分布：gmgn 58 / dexscreener 34 / tolly 5；成交口径：gmgn 41 / dexscreener 35 / tolly 16 / 空 5 | 6.3.1 ｜ 10.9 |
| 12.27 | 进度五段额度（9.6.1）的依据 —— 四条轮（Robinhood 523s / 598s，Arc 341s / 333s）按墙钟加权：pull 19.0% / topup 10.7% / gate 20.3% / draft 46.5%（逐轮百分比均值 pull 18.6 / topup 12.2 / gate 22.3 / draft 43.7），取整成 19 / 11 / 21 / 45。四条轮的 `gate.judged` 分别 60 / 51 / 67 / 65，`unjudged` 全是 0（判完整池） | 6.2.8 ｜ 9.2.6 ｜ 9.6.1 |
