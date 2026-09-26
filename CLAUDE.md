# Crypto Hunter — 项目说明（agent 接手文档）

> **这是项目的唯一真相来源。** 定位、怎么跑、契约、规矩、坑都在这里。视觉的真相只有一处：`web/src/styles/`（tokens → proto → app），没有别的设计源，旧原型已删。
> **维护规则**：改动只要碰了**分层 / 契约 / 运行方式 / 数据模型 / 视觉规则**，就回来改这里。
> **写法**：只写**现行**的规则、键名与实现位置，不记改动史。改了口径就**改写**那一条，不要追加「某天起改成…」——过程与当时的实测数字属于提交信息，不属于这里。

## 目录

- **一、项目真相（必读）**：1.1 这是什么 · 1.2 一轮怎么跑 · 1.3 技术栈与运行 · 1.4 目录地图 · 1.5 契约与红线 · 1.6 数据模型 · 1.7 状态机 · 1.8 数据源 · 1.9 验证 · 1.10 环境与坑 · 1.11 还没接的部分 · 1.12 工作方式
- **二、口径与流水线**：2.1 已定口径（13 条）· 2.2 状态机边界 · 2.3 进度与完成态 · 2.4 写作阶段（LLM）· 2.5 词条文字规则 · 2.6 取值、合并与读数纪律
- **三、界面规则**：3.1 报告卡 · 3.2 设定页 · 3.3 侧栏与顶栏
- **四、视觉系统**：4.1 字体字号字重颜色 · 4.2 主题 · 4.3 图标与 Logo
- **五、运行与性能**：5.1 读路径 · 5.2 中止一轮
- **六、待决**：6.1 需拍板的事

---

# 一、项目真相（必读）

## 1.1 这是什么

每 3 小时，从一条链上的几千个币里挑出 N 个（默认 5，`per_round` 可设），每个附一份两分钟能读完的判断。**研究流水线，不是行情终端。**

不做：交易、仓位建议、买卖点、实时行情、信号指标、多用户、回测引擎。

## 1.2 一轮是怎么跑的（主线）

`pull 拉取扫描源 → 去重 → 硬过滤（按优先级累计淘汰）→ 零数据淘汰 → 安全判定（限量）→ 打分排序（链上质量 55% + 赛道产品 45%）→ 词条与写作（LLM）→ 落库 → 报告卡 → 归档 → 下一轮对比上一轮`

每一步的口径与实现位置在第二、三部分；动手改流水线之前先把 **2.1** 读完。

## 1.3 技术栈与运行方式

| | |
| --- | --- |
| 后端 | Python 3.13 + FastAPI + SQLite(WAL) + httpx（GMGN 要 TLS 指纹，用 curl-cffi），uv 管理，依赖精确 pin |
| 前端 | React + TypeScript(strict) + Vite + TanStack Query，**无 UI 组件库**，样式全部自写 |
| 生产 | **同域单进程**：FastAPI 托管 `web/dist`，只有一个端口 8787 |
| 开发 | Vite dev server 5173，`/api` 代理到 8787 |
| 调度 | 服务内 ticker + `runs` 表唯一约束做幂等，**不用 APScheduler**（避免两套状态；幂等靠 `run_key` 唯一） |
| 图标 | lucide-react，统一走 `web/src/components/Icon.tsx` |
| 出图（未接） | TG 对比表计划用 Playwright 截前端同一张表，一份组件两处用 |

```powershell
scripts/dev.ps1       # 开发：API 8787（后台）+ Vite 5173
scripts/build.ps1     # 生产：npm run build，然后单进程托管 web/dist
scripts/openapi.ps1   # 导出 openapi.json + 重新生成前端类型

cd api; uv run python -m pytest -q    # 后端测试（基线 365 绿）
cd web; npm run build                 # tsc -b + vite build，exit 0 才算过
```

## 1.4 目录地图

| 路径 | 是什么 |
| --- | --- |
| `api/app/api/v1/` | HTTP 层：`chains` / `runs` / `lexicon` / `settings` / `health` |
| `api/app/core/` | 纯逻辑：`filters` / `funnel` / `scoring` / `lexicon` / `models`（**不许有 IO**） |
| `api/app/pipeline/` | 一轮的编排：`collect` / `decide` / `states` / `progress` / `run` / `schedule` / `write` |
| `api/app/report/` | 报告：`facts`（唯一事实表）/ `cards` / `assemble` / `draft` / `brief` / `research` / `prompts` |
| `api/app/sources/` | 一个源一个适配器（13 个）+ `registry` + `http`（缓存与节流）+ `SOURCES.md`（逐源端点、传输、实测结论） |
| `api/app/store/` | SQLite：`schema.sql` + `db.py` |
| `api/tests/` | 365 条，按模块分文件 |
| `web/src/features/` | `report` / `settings` / `run` / `rail` / `overview` |
| `web/src/styles/` | `tokens.css`（两态颜色）→ `proto.css`（布局）→ `app.css`（覆盖与后续规则），**加载顺序即优先级** |
| `config/chains.yaml` | 出厂默认设定（每条链一套）；「保存并生效」写进 `settings_versions` 表，之后以表为准 |
| `config/llm.env` | 写作阶段的端点 / key / 模型（不进版本库；改文件下次调用即生效，不用重启） |
| `data/` | `crypto-hunter.db` + `raw/`（每轮原始响应与 HTTP 缓存） |
## 1.5 契约与红线

- 唯一的跨边界物是 `/api/v1` 的 schema：**Pydantic 是唯一真源** → `scripts/openapi.ps1` → `web/src/api/types.gen.ts`。**不许手写 DTO。**
- 前端不碰数据源与数据库；后端不内嵌 HTML（只托管 `web/dist`）；`api/app/core/` 里不许有 IO。
- 一个源一个适配器，源与源互不感知；同名指标取谁的值由 `pipeline/collect.py` 的优先级链决定。
- 页面上的解释性文案由后端 `settings/form` 给（「说明」icon 的内容），前端不自己造句。
- 分数只在后端算；前端不算分、不补数。
- **已发布的一轮不可改写**：`sent` 之后只能改词条库（见 1.7、2.2）。
- 缺值语义 = 未知（见 2.1 第 4 条）：没有的数是 `None` / 空，不许用 0 或空字符串替代。

## 1.6 数据模型（**11 张表**，以 `api/app/store/schema.sql` 为准）

加列走 `db.init_db` 的 `_ADDED_COLUMNS`（配 `SCHEMA_VERSION`）：老库靠它补齐并回填，别手改 `.db`。

`chains` · `settings_versions`（不可变，一次改动一行 JSON）· `tokens`（CA 主档，含 `peak_market_cap_usd`）· `runs` · `run_items`（每币进出原因）· `snapshots`（不可变，每轮每币）· `run_summary`（含 `dropped_hard`）· `reports`（已发布的卡片文字，冻结）· `lexicon`（词条库，带 `baseline_*` 与 `research_state`）· `lexicon_sources`（重档调研读过的页面）· `llm_calls`（模型 / 耗时 / 成本 / prompt 版本）。

## 1.7 状态机

`pending → collected → scored → selected → written → sent → researched`，任何在跑的状态都可以跳到 `cancelled` / `failed`。

- **`sent` 是硬边界**：之后只能改词条库，不能改已发报告。「先发后研」靠状态保证，不靠 prompt。
- `states.DELIVERED = {sent} ∪ TERMINAL`：自动部分结束、页面停表（`sent` 不是终态，但它是「本轮不再有自动动作」的那一刻）。
- `researched` 由重档调研的 worker 在「本轮卡片全部落定」之后写（`pipeline/run.py` 的 `researched_runs()` 从行里推导，重启后答案不变；`failed` 也算落定）。
- 卡住的一轮靠**状态机上的重试与补发**恢复，不另起看门狗脚本。

## 1.8 数据源

| 用途 | 源 |
| --- | --- |
| 全链扫描（`scan_sources`） | gmgn / radardex / tolly / geckoterminal |
| 安全与源码 | goplus（貔貅判定、税、危险权限、`dex[]` / `holders[]` / `lp_holders[]`）/ sourcify（源码验证） |
| 只补链接（`registry.ENRICH_ONLY`） | dexscreener：只填 `socials` / `websites` / `description`，**不填任何进分的数** |
| 链上直读 | `sources/onchain.py` 走链自己的 RPC（LP 位置、dev 余额） |

源侧五条硬规矩（逐源端点、传输与实测结论见 `api/app/sources/SOURCES.md`）：

- **GoPlus 独占这些字段**：`pool_list` / `top_holders` / `burn_pct` / `locked_holder_pct` / `contract_holder_pct` / `transfer_tax_pct`；`holders` / `top10_pct` / `buy_tax_pct` / `sell_tax_pct` 以 GMGN 为主、GoPlus 兜底。一次响应 34 个字段，只用其中一部分是浪费。
- **`blockscout` 不在 `scan_sources`**（列表接口只有持有人数，市值与流动性全空，会把候选池灌成四千个），但仍留在合并优先级链里。explorer 的 `blockscout_checked` 不走 `http.cached`，所以「浏览器不认识这个地址」每轮都会重问。
- **税率只认 GMGN 的安全表**（`gmgn.ai/api/v1/token_security_evm/{chain}/{ca}` 的 `total_buy_tax` / `total_sell_tax`，缓存 6h）：GMGN 排行行的税率是占位 0、GoPlus 的税率在 Arc 上也全是 0，而 0 是「有值」，会赢走字段 —— 所以 `FIELD_PRIORITY` 里 `buy_tax_pct` / `sell_tax_pct` **只列 gmgn**，没有表的标的宁可显示「—」。同一张表带 `is_honeypot`，作为貔貅判定的第二声。
- **LP 判定先链上、答不出才用 GoPlus**：`onchain.lp_lock_from_holders()` 读 `lp_holders[]`（每条带 `percent` / `is_contract` / `is_locked`，`percent` 是 LP 供应占比、整表合计 1）——**多数 LP 在合约里（锁仓合约 / 金库 / 发射台持仓）＝已锁，多数在钱包里＝未锁**；表覆盖不到 50% 的 LP 时不给答案。链上读答不出才走它（所以发射台路径发行的仍显示链上的理由）。v4 池没有池合约（头寸是头寸管理器里的 NFT），这是链上读答不出的原因；要补最后一段只能问头寸 NFT 的 `ownerOf`（头寸管理器地址在 `sources/onchain.py`）。判定路径是 `_recorded_lp` 与 `_lp_from` 两条，改口径时两条都要动；「未判定」的原因取 `LAUNCH_*` 字面值（`LAUNCH_OK` / `LAUNCH_V4` / `LAUNCH_NO_TX` / `LAUNCH_EXPLORER` / `LAUNCH_NODE` / `LAUNCH_NO_TARGET` / `LAUNCH_NO_POOL` / `LAUNCH_BASIS`）。
- **源码验证是双源取或**：`source_verification_mode` = 双源（GoPlus + Sourcify），语义是**任一源确认即算已验证**（`dual` 取 `or`）；切「仅 GoPlus」会更宽松。

## 1.9 验证（改完必须做）

1. 改了 `.py`：`cd api; uv run python -m pytest -q`，基线 **365 绿**；然后**重启 API**（常驻进程，不重启跑的还是旧代码）。
2. 改了前端：`cd web; npm run build`，exit 0（里面有 `tsc -b`，类型不过就构建不过）。
3. 视觉改动：1726 视口（用户的屏）**深/浅各截一遍**，覆盖报告页（折叠 + 全部展开）、设定页、本轮总览、词条库展开态；坐标与计算值用 `getComputedStyle` 验，不靠肉眼。

## 1.10 环境与坑（Windows）

- **PowerShell 语义**：不要用 `&&`，用 `;` 或 `if`；含 `&` 的 URL 要加引号。
- **写文件一律 UTF-8 无 BOM**（JSON / TSX / MD / YAML 尤其）；不要用 PowerShell here-string 写非 ASCII 内容。
- 终端里中文显示成 `????` 是 GBK 显示问题，**文件没坏**，用 UTF-8 读一遍再下结论。
- playwright 必须给 Chromium 绝对路径：`C:/Users/12620/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe`。
- 服务常驻 `http://127.0.0.1:8787`，托管的是 `web/dist`：前端改完要 build，浏览器要强刷。
- 长命令（build / pytest）把输出重定向到日志再看尾巴，别把整屏刷进上下文。
- **仓库还没有首次提交**（`git status` 里全是 `??`）：删文件没有历史可回滚，动手前想清楚。

## 1.11 还没接的部分（别当成 bug）

- **TG 推送**：`api/app/out/` 是占位模块（`out.configured()` 返回 `False`）。一轮跑到那步写「未配置（跳过推送，报告已落库）」并正常收尾，**不阻塞**；顶栏「推送 TG」与「导出」是 demo 按钮，点了不会真做，`title` 里写了原因。
- **一键重算**（`POST /settings/recompute`）返回 501：一轮的 universe 没落库。
- **词条库**有「待入库 / 待重写 / 重档调研」状态：未入库的标的本轮不参与排名（不是 0 分）。
- **无上限的规则**：`holders_max` 默认 `null`（见 6.1 第 4 条）。

## 1.12 工作方式（和这个用户合作）

- **先讨论再动手**：他说「我们先讨论」时，只出判断和方案，**不动代码**。没让改的别改。
- **一次只动该动的**：不顺手重构、不修无关的 bug（可以在汇报里提一句，由他决定）。
- **不 `git commit`、不建分支**，除非他明确要求。
- **改动贴现有风格**：直角（`border-radius:0 !important` 全局已定）、颜色只从 `tokens.css` 取、文案口径跟 `settings/form` 走。
- **汇报短**：先说结论，再给实测数字与 `文件:行`；UI 改动附截图。他对 UI 细节极其挑剔，一处一处改、改完复述「改了什么 + 实测多少」。
- 用**中文**沟通；代码注释与标识符按现有习惯（注释英文、文案中文）。

---
# 二、口径与流水线

## 2.1 已定口径（13 条，含实现位置）

1. **分位分母 = 本轮硬过滤幸存者**（不是全历史池，也不是过完安全门槛的池子）。实现 `core/scoring.py` 的 `CandidateSetPercentile`（中点法 `(below + 0.5*ties)/n`）；逆向因子 `REVERSE_FACTORS = {top10_concentration, bundling}`（越小越好）。分母用 `universe.metrics` 的**粗筛视图**构造，不用富集后的 `item.metrics` —— 否则同一批里被富集的 12 个会改变分母，同一支币的分数会随分批顺序漂移。
2. **淘汰数 = 按优先级累计**（不是独立计数）。实现 `core/funnel.py` 的 `cumulative_drop_counts()`；优先级顺序即设定页 `RULE_ROWS` 顺序：市值 → 流动性 → 持有人 → Top10 → 捆绑 → 貔貅 → 24h 成交 → 源码 → LP → 危险权限。
3. **新币冷启动**：未入库标的默认 `pending_review`，本轮不参与排名，也不显示 0 分。
4. **缺值语义 = 未知**：整源缺失记 `None`，既不判通过也不判淘汰。
5. **零数据淘汰**（`prefilter.min_scored_fields`，`1`）：五个排序因子（`scoring.FACTOR_FIELDS`：流动性 / 24h 成交 / 持有人 / Top10 / 捆绑率）里**一个数都没有**的标的按 `missing_data` 硬淘汰 —— 它没法打分、没法取池内分位。**只在富集之后判**（`check_all`，不在 `check_prefilter`）：富集前那个空字段的含义是「粗筛没带这个字段」。关掉它：`min_scored_fields: 0` 或缺键。实现 `core/filters.py` 的 `check_scored_data` / `scored_fields`；计数行在设定页「硬过滤」的「零数据淘汰 ≥ N 项」，优先级排最末，所以同时踩真阈值的仍记在阈值上。
6. **安全判定限量**（`pipeline.gate_limit`，`20`）：按 **24h 成交额降序**取前 20 个送安全判定，其余留「未判定」——**既不计通过也不计淘汰**，本轮 note 写明「只判定前 N 个，其余 M 个未判定」。依据是实测：per-token 读取按主机串行节流（GeckoTerminal 2.2s/次），判完 163 个要 20 分钟，加并发无效。两条补充：①**预算是预算，不是少出报告的理由** —— 预算花完若达标候选不足报告张数，继续按批往后判；②最后一批按剩余预算截断（20 就是 20 个请求）。`0` = 判完整个池子；想再加一层上限就用 `pipeline.gate_rounds`。实现 `pipeline/decide.py` 的 `GATE_LIMIT` / `_gate(..., need=)`，测试在 `test_engine.py`。
7. **LLM 降级 = 无文字**：`write.py` 的 `"written" if payload["note"] else "draft"`。文字缺席时报告落 `draft`，卡片正文仍由确定性拼装产出（`report/assemble.py` + `report/cards.py`）。
8. **GeckoTerminal 精查延后到入选之后**（`DEFERRED_ENRICH = ("geckoterminal",)`，`pipeline/decide.py`）：它是唯一能回答「几个池、价格走过的路」的源，也是最贵的一个（逐币三端点、同主机 2.2s 节流、429 固定退避 10s、加线程无效），而它填的全是卡片字段（不进安全判定、不进分数）。所以 gate 阶段跳过它，`picked` 定下来后只对入选的补一遍（`_enrich(..., only=DEFERRED_ENRICH)`），日志单独一行 `sharpen`。第二遍把答案并进第一遍的 `views` 再 merge（**不从批量行重建**，否则第一遍刚拿到的逐币值会被粗筛行盖掉）；`facts` 与 LP 上链读取整段跳过。
9. **词条档案带基准与刷新条件**（`core/lexicon.py` + `report/research.py`）：词条同时是 45% 的赛道产品分**和**卡片正文，所以不能「写一次、永远复用」。每个档案落库时记下它是对着什么写的（`baseline_mcap` / `baseline_holders` / `baseline_at` / `baseline_safety`，`draft.store_entries`），每轮入选用 `lexicon.due()` 判一次。四条规则按序（**刷新条件优先于跳过窗口**）：市值较建档波动 ≥ 40% → 重写；持有人较建档增加 ≥ 40% → 重写（减少不算）；超有效期（默认 2 天）→ 重写；都没命中且在跳过窗口（默认 6h）内 → 复用。四个数在设定页「词条库状态」（`pipeline.lexicon.*`）。重写发生在**评分之前** —— 同一份档案既是分数又是文字，先评分后改字会让结论和分数来自两份档案。
10. **重档调研放在发布之后**：它读项目自己的页面（官网 / X / GitHub / 文档，最多 4 个，正文合计 8000 字符上限），用同一个 JSON 契约重写同一份档案，把读过的页面写进 `lexicon_sources`。它落在 `sent` 之后，异步跑在独立线程（`schedule.py` 每 30 秒看一次、一次 2 个），失败只写 `lexicon.research_state=failed` 与原因，**不阻塞任何流程**；本轮卡片全部落定后轮次才从 `sent` 走到 `researched`。两条路各自记账：跳过窗口防「用同一批事实反复重写」，重档看的是 `research_state`（**每次重写档案都归零**）。人工确认过的（`review_state=confirmed`）两条路都不动，卡片写明「解读写于 X 小时前 · 人工已确认，未自动改写」。反过来，**只要复用了旧档案，卡片就必须带时间标注**（`entry_note` → `reports.payload_json`）——「老文字配新数字」是这套系统里最容易骗到人的一件事。
11. **空壳不进池、预算不凑数、词条库有进有出**（`core/filters.py` + `pipeline/decide.py` + `report/draft.py`）：①**空壳淘汰默认开启**（`require_market_data`，链配置写 `false` 才关）：市值与流动性同时缺失不是「源没回答」，是「没有可答的东西」，不该走「未知 = 通过」；`MISSING_DATA` 在 `PREFILTER_PRIORITY` 排最后。②**判定预算就是预算**（`Engine._gate`）：cap 用完就发布，**少出几张卡好过拿没数的行凑数**；唯一例外是「一个都没通过」——零张卡不是短报告，是对池子的读数，这时才继续往后找。`per_round` 从此只管「从通过者里取前几名」。③**词条库有进有出**（`draft.prune_lexicon`，每轮落库前跑，失败只记日志）：档案留下需满足三者之一 —— 人工确认过（`review_state=confirmed`，永不删）、曾经入选过（`run_items` 有 `selected`）、本轮又被判定过（本轮 `scored` 的 CA，作为 `keep` 传入）。删除连带清掉它的 `lexicon_sources`。
12. **写作输入 = 卡片那份事实表**：事实表只有一份实现 `report/facts.py`（`lines()` / `block()` / `safety_line()`），卡片、词条草稿、本期点评、重档调研四处共用；缺值打印「—」（「五个源都没这个数」本身就是事实）。链接**逐币从 DexScreener 读**（`sources/dexscreener.py`：只填 `socials` / `websites` / `description`，**不填任何进分的数**；它不在 `sources:` 里，而是 `registry.ENRICH_ONLY`，由 `Engine._enrich` 在逐币那一遍问 —— 位置在写词条之前，所以链接是写作输入的一部分）。社媒的 handle / 裸域名由 `base.social_url` / `base.web_url` 补主机与 scheme（RadarDEX / Tolly / GMGN 共用）。取值规则：只认 `baseToken.address` 命中的那一对，否则若答案里只有一条链才接受 —— 另一条链的 X 句柄写到这张卡上比空着更糟。
13. **距高回撤（双确认）**（`pipeline.prefilter.drawdown_max_pct`，写负数，默认 `-70`；前端也叫 `距高回撤`）：距「历史最高市值」与「主池近 10 日最高价」各回撤 70% 以上才算淘汰；有一侧判不出就保持「未判定」，不淘汰。**两个口径读的是两样东西**：历史最高市值不是历史最高价，而是**本项目每轮扫描见过的最高市值**（`tokens.peak_market_cap_usd`，每轮对每个扫描到的币 upsert，不只进候选的），老库由 `_backfill_token_peaks` 从 `snapshots` 回填（那是「在区间内被看到的最高值」，是下界）。**首见不算「未知」**（否则每个新币都会给「未判定」加一个数）。**归因排最后**（`funnel.DROP_PRIORITY`）：同时踩了流动性门槛的仍记在流动性那一行。免费的那一侧（库内历史）先判，它决定要不要去问收费的那一侧（GeckoTerminal 日线，只对「历史已判深且还没有池面读数」的币问）。

## 2.2 状态机边界

`pending → collected → scored → selected → written → sent → researched`，任一态可转 `failed`；`cancelled` / `researched` / `failed` 是终态。

- **`sent` 不是终态**（词条库还能改），但推送出口未接入时它**就是一轮的结束态** —— 所以「一轮结束了」由一个集合表达而不是一个状态：`states.DELIVERED = {sent} | TERMINAL`（它决定 `finished_at`，见 2.3）。
- 展示口径的锚点：`sent` 之前的一切都可能被改写，之后写出的文字是冻结的（落在 `reports.payload_json`）。

## 2.3 进度与完成态

**进度百分比**（`pipeline/progress.py`）：一轮的五个阶段不是等长的（拉源约两分钟、安全判定占四分之三、打分是算术、写作是模型在答），按阶段均分会先躺二十分钟再跳一格。所以每个阶段占一段额度，段内按**实际完成的工作量**填充：

| 阶段 | 额度 | 段内单位 |
| --- | --- | --- |
| `pull` | 0–8 | 每个扫描源拉完一次 |
| `gate` | 8–82 | 每判完一个候选 |
| `score` | 82–86 | 一次 |
| `draft` | 86–96 | 每写完一批词条草稿 |
| `commentary` | 96–100 | 每写完一段点评 |

- 额度是按实测墙钟占比定的：`Timer` 每轮把各阶段真实耗时打成一行 `round <id> phase timings: ...` 进日志（数据库只留结论、不留耗时，日志是唯一能复核这些额度的地方）。`runs.progress` / `runs.progress_label` 存最新值，前端 `RunPanel` 顶部显示 `NN% · 阶段名`。
- **进度只增不减**：`_set_progress` 落库用 `MAX(progress, ?)` —— 段内额度是估的，回跳比走得慢更伤信任。`percent` 单调，`progress_label` 仍如实跟着阶段变。测试 `test_the_bar_never_walks_backwards`。
- `sharpen`（入选后的 GeckoTerminal 补读）与 `write`（落库）**不占额度**，只记日志：为一两秒的阶段切一条进度带只会让另外四段变窄（它们分别停在 86% 与写几百行库）。
- **推送出口未配置 = 一轮的完成，不是卡住**：`out.configured()` 返回 `False` 时 `run.py` 的 `_finish()` 把轮次推到 `sent`，写 `PUSH_STATUS = 未配置（跳过推送，报告已落库）`。将来接出口改的是 `configured()`，流程不动。
## 2.4 写作阶段（LLM）

词条草稿与卡片点评走一个 OpenAI 兼容端点。配置在 `config/llm.env`，由 `api/app/config.py` 读取：**真实环境变量优先**，而这个文件每次调用时重读，所以改完下一轮生效、不用重启。

| 变量 | 作用 |
| --- | --- |
| `CH_LLM_BASE_URL` | 端点，客户端自己拼 `/chat/completions` |
| `CH_LLM_API_KEY` | Bearer |
| `CH_LLM_MODEL` | 模型名 |
| `CH_LLM_TIMEOUT` | 单次超时，默认 180s |
| `CH_LLM_MAX_TOKENS` | **推理模型必填**：思考也吃这个额度（该端点接受 65536，当前 32000） |
| `CH_LLM_DRAFT_BATCH` | 词条草稿每次调用写几条，默认 5 |
| `CH_LLM_EXTRA_HEADERS` | JSON，额外请求头（本端点必须要 `x-opencode-session`） |
| `CH_LLM_TRIES` | 尝试次数，默认 2；只重试 5xx 与网络错误，4xx 直接失败 |

当前配置：`https://opencode.ai/zen/go/v1` + `deepseek-v4.1-flash`。有没有写手，看 `/api/v1/health` 的 `llm` / `llm_model` 两个字段。

**三个坑（都已踩过）**：

1. **推理模型的思考计入 `max_tokens`**，额度给小了返回的是**空消息**（`finish_reason = length`），不是报错 —— 客户端把空消息当失败，于是额度调小会静默降级成「没有文字」。
2. **该网点偶发 503** `Endpoint is unavailable`（`gpt-5.6-luna` / `grok-4.7` 直接不可用），所以有上面那条重试规则。hermes 里那两个 deepkey key（`sk-fm7NO…`、`sk-72CtQ…`）实测都是 401，都不要用。
3. **一次调用写完所有条目必然撞天花板**：`draft_lexicon` 必须分批发（每批 `CH_LLM_DRAFT_BATCH`，默认 5），每批单独校验、单独落库 —— 一批答不出来只丢它自己那几条，原因通过 `notes` 进本轮 note（`词条草稿部分未生成（N 批：…）`）。参考实现没有这个问题：它不自己调 LLM，而是把 prompt 交给 agent，由 agent 决定怎么写、写几轮。**「一次调用写完所有条目」是我们自动化时新引入的约束。**

## 2.5 词条文字规则

三份 prompt（`report/prompts/{lexicon,commentary,research}.md`）共用一套纪律，`PROMPT_VERSION` 一起升级。

**1. 每字段篇幅上限写在 prompt 里，代码只在超长时记一条日志、不退回** —— 删一条草稿要赔上整批标的（`CH_LLM_DRAFT_BATCH` 条一起丢），代价比文字长一点大得多。落在 `report/schema.py` 的 `CAPS` / `PER_TOKEN_CAP`，超长走 `log.warning`，`validate_*` 返回值不变。

| 字段 | 上限 |
| --- | --- |
| `concl` | 500（下限 200） |
| `narr_text` | 150 |
| `prod_text` | 200 |
| `mech_text` | 200 |
| `pros` / `cons` | 各 100 |
| `per_token`（本期点评） | 300 |

**2. 安全只照抄。** 涉及源码 / 权限 / LP 的句子必须一字不改地引用事实表 `【安全】` 行的原文（例：`源码 已验证 ｜ 危险权限 无危险权限 ｜ LP 已锁`）—— 卡片曾一边显示「LP 已锁」一边在点评里写「三项全部未判定」。

**3. 链接统一成「说明 + 可点击词」**（`官网 <a href="...">solonpad.fun</a>`）：prompt 要求；`report/segments.py` 把 anchor / markdown / 裸 URL 都变成 link 段；前端 `Rich.tsx` 渲染成 `target="_blank"` 的 `<a>`，**全链路没有 `dangerouslySetInnerHTML`**。

**4. 社媒块由服务端补链接，页脚只留交易页。** `report/draft.py` 的 `_social_block` 保留写手自己那句，再把这一轮真实解析到、而句子没提到的链接（`metrics.socials` / `websites`）按 `X / TG / 官网` 补在末尾。`report/cards.py` 的 `links()` 只留 `GMGN ｜ Debot ｜ DexScreener`。

**5. 三条纪律（三份 prompt 一致）**：`concl` 与 `narr_text` 严禁重复（确实没有新内容就留空 `""`）；提到外部信息必须附**实读到**的原文 URL；`social` 写成「handle · 粉丝数 · 备注」不带 @；`mech_text` 写回购 / 燃烧 / 分红是否写明与 dev 的买卖行为。

**6. 结论的写法**：`concl` 200–350 字，**只做两件事** —— ①第一句必须是身份句（链 + 赛道 + 定位），主体讲它是什么、在做什么、凭什么；②结尾一条最关键风险。**不写观点句**（「本批唯一…」「第一梯队」一律不写，判断留给读者）、**不写数字段**（市值 / 流动性 / 持有人 / 量能 / 筹码不罗列，表格与卡片上都有）。卡片**不为叙事单开一块**：叙事落在结论里，`narr_text` 退为补充位（定位 / 机制 / 竞争位），通常留空。

**7. 刷新条件多一条：安全判定变化。** `lexicon.baseline_safety` 存「这些字写下来时，安全块是什么」，存成**可读的字**而不是哈希 —— 它要落进 `refresh_reason` 给人看（例：`安全判定变化（LP 未判定→已锁）`）。三种情况**不算变化**（记在这里，免得以后当成漏判）：没有 baseline（回填不到的行留空，空值不触发）；本轮没读到（`safety_signature` 返回空串，源挂了）；本轮三项全是「未判定」（`lexicon.UNDETERMINED`）。反过来「未判定 → 有判定」**要**重写，所以「什么都没答」也必须记成一个值。

**8. 草稿路径没有页面正文。** `lexicon.md` / `commentary.md` 喂的只有事实表（`draft._draft_batch` 的 context 就是 `facts.block`），所以 prompt 不许写「先按链接写」—— 那只会得到「声称我读到了」。这一遍确证不了就写「—」；真去读页面并据此重写是深档（`research`）的活。

**9. 深档必须带安全块。** `research._latest_metrics` 一并读 `safety_json`，`_facts_line(ca, symbol, metrics, safety)` 交给 `facts.block`（测试：`test_the_deep_pass_sheet_carries_the_safety_verdict`）—— 否则管线里最宽的那张事实表恰好看不到安全块，深档写出来的词条永远写「安全块三项全部未判定」。

**10. 重档调研把「读不到」和「没东西」分开。** ①**空壳页面等于没读**：静态 HTML 正文不足 `MIN_CHARS`（80）时 `_read` 会返回 `None`，而失败的页面不落库 —— 表面「读了 2 个链接」，实际只剩一个，在模型看到的世界里这个项目就是没有官网。所以 `_read` 先静态抓、不够长再把**同一个 URL** 交给 `READER_PREFIX = https://r.jina.ai/` 渲染重试；关键是**请求头**：jina 对浏览器 UA 返回 403、对 `curl/8.5.0`（`UA_READER`）返回正文。②**TG 要读**：`READABLE_SOCIALS` 含 TG —— `t.me/<name>` 的公开预览页带频道名、成员数与最近几条帖子，正是社媒行要的东西。③**点评缺对照与去重纪律**：`brief.build` 多给一段「落选池（同一批打分、未入选，供对照）」（`run.near_misses`，与报告页落选表同源，取前 6），prompt 要求点评写「本期变化 + 与上期或池内对比 + 看点隐患 + 下一步跟踪」。
**上限压得怎么样**：`concl` / `prod_text` / `pros` / `cons` 基本压住；**唯一没压住的是 `per_token`**（实测 5 条全在 431–458，超 300 上限 44%）—— 这条 prompt 既要「变化 + 对比 + 看点隐患 + 下一步跟踪」四件事又只给 300 字，写满就是 450 上下。要不要放宽到 400 或砍掉「下一步跟踪」，见 6.1。

## 2.6 取值、合并与读数纪律

**取值**（这些规矩都是被具体的错纠过之后定下的，别再犯）：

- **判据是「谁的值更可信」，不是「谁先回答」**：同名的**标量**取谁的值由 `collect.py` 的 `DEFAULT_PRIORITY` 决定，字段级的例外写在 `FIELD_PRIORITY`（源侧分工见 1.8，例如 `pool_list` / `top_holders` 只有 GoPlus 给）。
- **0 是答案，不是缺失**：`decide._absorb` 用 `is not None`（空列表也是答案）；GMGN 的 `X or Y` 用 `_first(X, Y)`；`creator_created_count` 的 `_positive` 用 `_num`（dev 发币数 0 是发现）。
- **集合类型的空值算「没答」**：`collector.merge` 里 `[]` 不赢走字段（与 `price_series` / `socials` / `websites` 一直以来的写法一致），否则 GMGN 的空 `pool_list` 会把 GoPlus 的答案挡在外面。
- **`source_verdict` 的 `dual` 取 `or`**（任一源确认即通过）—— 写成 `and` 会在 Sourcify 收录很差的 Arc 上一票否决。
- **覆盖方向**：`overlay(universe_bulk, per_token_merge)` —— 逐币富集值盖在批量视图上，别写反（`overlay` 不复制 `provenance`，所以 `provenance` 要单独合并）。
- **卡片读数取「先有值的那一份」**：同一批读数库里存两份（`metrics` 批量榜单、`safety` 逐币判定），`cards._either(metrics, safety, key)` 逐字段取；硬过滤与安检读的是 `metrics`，所以取错只是显示错、不是判断错。
- **税率只认 GMGN 安全表**，见 1.8；`dex[].pool_fee` 在 v4 上不可读，池子清单不印费率。

**缓存与记账**：

- **「对方说没有」也是答案**：生产者用 `http.NEGATIVE` 标记「对方答了，答案是空」，`cached` 会缓存它（读回来仍是 `None`，调用方无感）；真正的 `None`（超时、5xx）照旧不缓存。Arc 上多数币在 GeckoTerminal 没有池，不缓存否定答案等于每轮重问、每次 3 次重试 + 2.2s 节流。
- **失败退避只在失败路径上生效**（传输层默认 `sleep` 0.8、GT 逐币端点 1.0），成功路径的节流没动。Sourcify 的 404 是答案不是失败，所以 `tries=1`。
- **记账要覆盖最贵的那台主机**：`http.tally()` 是公开的记账入口，`onchain.rpc_checked` 按同一套计数（ok / retry / fail + 耗时）记账，所以链节点会出现在每轮的主机行里。
- **日志是唯一的耗时真相**：`app/logging_setup.py` 在 `python -m app` 启动时给 `crypto-hunter` 命名空间挂 stderr handler（`CH_LOG_LEVEL` 可调，默认 INFO，`propagate = False` 不污染 uvicorn）—— 不挂的话 `logging.lastResort` 只写 WARNING 及以上，`progress.Timer` 算出的阶段耗时会被静默丢弃。秒表由 `run_chain` 持有、按 `run(clock=...)` 传入，引擎只填不打印（`owns_clock`），所以点评与落库的耗时也在内。
- **不要拿单轮数字下结论**：节点延迟轮间 2.2–4s 波动，`eth_getLogs` 的调用次数也轮间不可比（见 5.1）。

**读数与漏斗口径（四个数必须一次数出来）**：

- **一次从 items 的 decision 数出来**：`core/funnel.py` 的 `summarize()`（`RoundResult.funnel_summary`，`write.py` 照抄），于是 `scanned = dropped + candidates` 是恒等式，不再是四个各自来源的数字碰巧排在一起。
- **候选含入选**：落选池标题的「候选」必须含入选（否则「候选 − 入选」不等于表里的行数）；设定页那句「漏斗」文案同理用 `universe − candidates`。
- **`runs.dropped` 是总淘汰**；面板「硬过滤」那一行的份额单独写进 `run_summary.dropped_hard`（前端不靠减法猜）。「硬过滤」的份额由 `prefilter_dropped` 算（按优先级归属、含富集之后才被硬过滤规则淘汰的标的）。
- **落选池收全部候选**：有分的按分数排，没分的排在其后并写明理由（`未建档（本轮不参与排名）` / `数据缺失，本轮不参与排名`）。
- **退出项的名字从 `tokens` 取**（CA 主档）：退出项被画出来时那个币已经不在本轮报告里，只有主档还记得它叫什么；不要退回 `ca[:10]`。
- 前端不重算这些数，全部来自 `/api/v1`。

---
# 三、界面规则

## 3.1 报告卡

- 每轮卡片张数 = `per_round`（默认 5），在「本轮总览」里横向一排。**不要求每张高度对齐**，但折叠状态下**所有卡片等高**：`结论` 固定高度（10 行，`--verdict-lines`），放不下显示「…」、hover 出全文（`.verdict-pop`）。「全部展开」时结论显示完整、不再需要 hover；「全部收起」时回到固定高度。
- **一键开合**：「本轮总览」左侧一个按钮，一次开合全部卡片的两段折叠内容。做法是**不接管 `details`** —— 换 key + 给新的默认值（`key={quality-${tick}}` + `open={openAll}`），让五张卡的两段 `<details>` 重挂一遍，各卡自己的开合用不着变成页面状态。
- **卡片宽度不随展开变化**：栅格 `.grid` 是 `repeat(auto-fill,minmax(345px,1fr))`，展开一张卡不会把五张一起挤窄或换列（列数只由容器宽度决定）。
- **123 名顶部用金银铜背景区分**（`--podium-1/2/3` 与 `--podium-1/2/3-bg`）。
- 「链上质量」整块**默认折叠**（`<details>`，与「展开详述」同一套展开语法）：卡片重排成「结论在前、数据一键展开」，五张横排才有一眼可读性。
- 卡片头部：`标的 · 词条` 一行，中间一栏是市值，CA 单列一行带**复制 icon**（点击后显示「已复制」，icon 与 CA 同字号）；页脚是「去哪儿交易」，链接由 `report/cards.py` 的 `links()` 用 `TOKEN_URLS`（`gmgn_url` / `debot_url` / DexScreener）拼：`GMGN ｜ Debot ｜ DexScreener`（名字写死这三个，LLM 上下文里的 `【交易页】` 用同一写法）。
- **貔貅是判定不是数字**：读 GoPlus 的 `is_honeypot`，输出「是 / 否 / —」三态带**色框**（`cards.honeypot_tone()` → `MetricOut.tone`；是＝`--danger-border`、否＝`--ok-border`、—＝`--warn-border`，直角、复用安全区 pill 的配方）。CSS 必须写成 `.kv .kv-v.hb-*` 才压得住缺值行的灰字（两层的写法会静默失效）。陷阱占比由「陷阱」chip 承担（读 `honeypot_pct`，也就是 GMGN 的 `top_entrapment_trader_percentage`）—— 两者不是一个数，不要把同一个值挂在两个名字下。
- **LP 只分三态**：已锁定 / 未锁定 / 未判定。显示「已锁」时**不跟括号解释**（标签本身就是答案）；未锁与未判定保留括号里的原因，多个原因用「·」分隔（理由自己带括号时会套娃）。
- 「链上质量」表**不印**：开盘、总供应（快照里仍存 `open_at` / `total_supply`）；洗量（`wash_pct` 已从 `TokenMetrics` / `METRIC_KEYS` 删除）；机器人 / 新钱包 / 私人金库 / 优质持有人 / 信号数（GMGN 在 Arc 上对这些一律回 0，一整行 0.0% 是噪声；采集与快照保留，换链若真有数再显示）。池子清单不印费率。
- **退出 chip 要说清是哪一条**（不是「跌出硬过滤或排名」这种含糊话）：detail 五选一（硬过滤的具体项 / 排名多少被挤出 / …）。调用点在 `decide._changes`，上下文（`items`、`pool_rank`、`per_round`、生效的 `prefilter` / `safety`）在 `_round` 里本来就够得着 —— 递进去即可。
- **一轮那一条**（`.bhead`）：日期步进 + `扫描 → 淘汰 → 候选 → 入选` + `已判定 / 未判定` + 本期变化 chips（新进 / 退出），**一行到底**，高度压低、区域分割线不与边框相连；`.bhead{margin:0 16px}` 与卡片同列。退出项显示**币的名称**，不是 CA。
- 日期控件属于**报告页**（不是全局）：它在报告页里与首张卡片对齐，外侧矩形控件与日期控件之间留左边距。
- 展示层的链接一律「说明 + 可点击词」，见 2.5 第 3 条；点开跳 `target="_blank"`。

## 3.2 设定页

- **每条链一套设定**（`/settings`）：保存写进 `settings_versions`（不可变），之后以表为准；出厂默认在 `config/chains.yaml`。链与链之间不共享设定。
- **解释折进「说明」icon**：`GroupOut.detail` / `SettingsFormOut.note_detail` / `lexicon_note`。判据是「这句话是这一轮的**读数**，还是关于字段本身的规定」—— 读数留屏幕（漏斗计数、违规提示、本轮入选均值、词条库那句结论），规定进 icon。口径文案留在 `forms.py`，**前端不认识任何一条文案**。
- **块内等高**：`.blk-cols{align-items:stretch}` 花掉栅格已经算好的行高，一行里所有框下边齐平；空白从框外搬进框内，所以组尾读数（`.pset` / `.note-inline`）用 `margin-top:auto` 顶到框底。每块里最高的那一组**分两栏**：切片不是对半（`rows.slice(0, cut)`），切点落在语义边界上，阅读顺序「左栏读完再读右栏」；判据是「没有东西必须折行」而不是「这行短」。宽度不够时挤压其他块的列宽，不换行。
- **读数靠右**：没有淘汰格的分组也是「label 左、读数右」（`margin-left:auto`），不要留成例外。`推送与依赖` 的读数**允许折行** —— 它是最窄的一栏，值本身就是答案（`GMGN · Rad…` 不算读数），长值绝不许把轨道撑出横向滚动条。
- **复选框**：直角硬线条（原生控件内部不吃 `border-radius:0`，圆角蓝块是 Chrome 画的 → 自绘），**与标题同一行、同一列起点**（`.prow.wide .field{flex:1 0 auto;max-width:100%}`，行本来就是 `flex-wrap:wrap`，缩不缩由行自己判断）；对勾是内联 SVG data-URL，浅色单独一条 `background-image`（自定义属性进不去 data-URL）。
- **区间**：`市值` 与 `持有人` 都是区间；上限 `holders_max` 与下限共用「持有人」这一条规则（`_ceiling(...)`），所以淘汰计数、漏斗行、设定页那一行都不用改。**缺键 = 无上限**（老快照没有这个键时不能开始砍币），默认留空见 6.1。空格子必须能存回去：`config.coerce_like` + `_infer`（空 → `None`，`2000` → `int`；已存 2000 再清空，`None` 要原样活过类型转换）。
- **词条库那一行**的表头只留 `+ 词条库` 和右端的 `说明`；popover 三件事：它是什么、它有多大（`rows.length`）、怎么读一行（后端 `lexicon_note`，里面的 45% 从 `_weight(built, "product")` 取，与块头同源）。展开词条库后表格下方那段表例说明同样折进 icon。
- **词条库可折叠**（默认收起），每个标的带**完整 CA** 与可复制的 CA icon。

## 3.3 侧栏与顶栏

- **左栏只渲染 `enabled` 的链**（未接的整行不出现，不是变灰）；链名旁边不挂计数（入选数在「一轮那一条」里）。左栏宽度 **260px**。
- 总览那一行的「读数（扫描 / 淘汰 / 候选 / 入选）」放在本期变化上方；「本轮总览」在导出左侧。
- **链自己的标记**：`Rail.tsx` 的 `chainIdMark()` 按 id 分发；Arc 的 artwork 是单路径**填充**图标（线重画会把记号画没），用 `currentColor` + prop 尺寸（侧栏 14，与 12.5px 那一档一致），深浅主题自动适配。
- **顶栏右端 = 当前页面的动作**，同一时刻只存在一份：报告页 `本轮总览 › ｜ 导出 ｜ 推送 TG`；设定页 `保存并生效 ｜ 立即运行`（在「本轮总览 / 导出 / 推送」的位置）。不可用的动作**灰显不可点**，`title` 写明原因。
- **中止按钮**：两次按压（第一下变 `确认中止`，4 秒后自解除 —— 一次误触不能留下「下一轮上来第二下就停」的按钮），请求在飞时换成 `正在中止`（转圈、不可点），图标 `Pause`，颜色 `currentColor`。
- 主题切换（黑 / 白）在侧栏下方，见 4.2。

# 四、视觉系统

## 4.1 字体、字号、字重、颜色

- **字体全站只用 sans**：`--mono` 与 `--serif` 都是 `var(--sans)`（`tokens.css`），所以任何地方再写 `var(--mono)` 都是 no-op —— 想让 CA 恢复等宽，改这一行即可。等宽原本承担的对齐交给 `body{font-variant-numeric:tabular-nums}`（等宽是为了让数字列对齐，不是为了换字族）。IBM Plex Mono 的 `@font-face` 与 woff2 已全部删掉。
- **字号只有 7 档**：`9.5 / 10.5 / 11.5 / 12.5 / 15 / 19 / 21`。按钮与表单控件 11.5（`.btn` / `.ghost` / `.step` / `.panel-h .x` / `.ok-btn` / `.frow input` / `.prow input` / `.prow .sel` 同档）；图标按钮 9.5（`.ca-copy` / `.note-help-ico`）；表格正文 10.5（表头与脚注 9.5）；块标题三层 —— 面板 / 大块 12.5、子组 11.5、卡片内块 10.5，行标签 11.5，`参数设置` 15。
- **字重只用 400 / 500 / 600**：`b` / `strong` / `h1`–`h6` 一律 600 —— CSS 没写就落回浏览器默认 700，会出现「同为小标题、一个字更黑」。
- **颜色只有一条来源 `tokens.css`**：页面里不写字面 hex。文字色用 `--fg` / `--fg-2` / `--muted` / `--muted-2`；`--accent` 只做填充与边框，**文字用 `--accent-fg`**；状态文字用 `--ok-fg` / `--warn-fg` / `--danger-fg`（`--ok` / `--warn` / `--danger` 给填充与边框）。
- **直角是全局的**（`*{border-radius:0!important}`），新控件不要再引入圆角。
- 浮层（`.verdict-pop` / `.note-help-pop`）用卡片那一套色（底 `--surface-warm`、字 `--fg`、边 `--border`），比所在那层底浅一档；**横向永不出滚动条**（`white-space:normal`，长值折行）。

## 4.2 主题（深 / 浅两态）

- 两套 token 都在 `tokens.css`：`:root` 深色、`[data-theme="light"]` 浅色。持久化只写 `ch.theme` 一个键（`web/src/theme.ts`）；`index.html` 里一段内联脚本抢在首屏前设好 `data-theme`（否则深色用户每次刷新先看到一帧白），脚本用 `try/catch` 包住 `localStorage`。
- **浅色不是反色**：链接 / 状态文字色、四种 `--*-bg` 淡底、金银铜、`--shadow` 都要按浅底重取；状态文字色要按**它能落到的每一个底**算对比度（`--surface-warm`、`--row-cur`）。要求：字号 ≥18px 按 3:1，其余 4.5:1。
- 浅色下 `--bg` 是页面底（白）、`--surface` 是面板底：左栏与菜单、分组与行、输入框与下拉、运行面板及其头脚、词条库表、复制 CA 芯片、**报告卡片**全部自动跟着走（`--surface` 在 `proto.css` 里有几十处引用，卡片只是其中之一），没有一处写死颜色。
- **运行面板的边框用 `--border`**（与它正下方的分组同一个 token），不用正文墨色 —— 用 `--fg` 在浅色下就是一只纯黑盒子。它是全屏唯一用正文墨色画边框的地方。
- **表单控件不吃继承**：`tokens.css` 里 `button,input,select,textarea{font-family:inherit}` 兜底（UA 样式会给控件直接设字体族，明确写了族名的规则靠优先级照样赢）。
- **已知残留**：深色主题的状态边框（貔貅三态框 / pill / chip）是 2.29 / 2.37 / 1.63:1，低于非文字元素 3:1 的建议线，没有为了对齐浅色去改它（见 6.1）。

## 4.3 图标与 Logo

- 图标只有一套：`lucide-react`（只进用到的）+ 一个组件 `web/src/components/Icon.tsx` + 一条规则 `.ico{display:block;stroke-width:1.5;stroke-linecap:square;stroke-linejoin:miter}`。**几何在样式表里否掉 lucide 的默认**（24 格、描边 2、圆角端点与连接）—— `stroke-*` 是继承属性、样式表声明赢过表现属性，所以后加的图标不可能忘记，调用点也不用重复传 prop。
- 尺寸三档，颜色一律 `currentColor`（不新开色值，自动适配主题）；icon 大小跟随旁边的文字大小。
- **Logo**（`web/src/components/Mark.tsx`）：雷达扫掠（方框 + 一道扫掠 + 一个回波点），用 `createLucideIcon` 声明以便走 `Icon`（`.ico` 那条规则与 `currentColor` 自动套上）。侧栏里它与 `CRYPTO HUNTER` 同行、大小适配；标签图标只用同一个记号、不铺底色、跟着主题走。

# 五、运行与性能

## 5.1 读路径

- **硬地板**：单次上链请求 2.2–2.5s，与查什么无关（`eth_getCode` 与 `eth_call` 同量级）；并发几乎免费（1 / 4 / 8 / 12 并发 = 2.16 / 2.37 / 2.88 / 3.56s）；批量确实省 —— 同样两次请求，单发 4.61s、打包成一个 JSON-RPC 批量 3.54s。一条链只有一个 RPC 端点，没有可换的备份。
- 所以提速只做「把本该同时问的一次问完」：`sources/onchain.py` 的 `rpc_batch(chain, calls)`（回包按 id 映射回顺序；非 list 回包 = 整批重试，重试耗尽逐项 `None`；用 `get` 同一套 `tally` 记账）、`nft_balances(chain, managers, who)`、`contracts_of(chain, addresses)`（批量 `eth_getCode` 判别 EOA）；`_pool_logs` 窗口并发（`LOG_WINDOW_WORKERS = 4`，窗口互不相交、`pmap` 保序）；`pipeline/decide.py` 的 `ENRICH_WORKERS = 8`，同一个币的多个源**并发**问（合并顺序仍按源顺序，`_absorb` 结果不变）。
- 效果：RPC 请求数 40 → 30 / 轮（这是唯一干净可比的数字，≈ −25%），`gate` 36–44s → 29s。等待时间基本没改善（次数降了 25%，但单次延迟轮间能从 2.2s 飘到 4s，再叠 retry 就吃回去了）——**别把它当战果**。
- **`eth_getLogs` 的调用次数轮间不可比**：`_logs_in` 在节点拒绝窗口宽度时对半递归，拒几个窗口由节点侧决定。**结论看整轮的 `ok=` 总数，别用单次 A/B 的调用次数下结论。**
- **还没解决**：30 次 / 轮里没有可去的重复调用（探针证明一轮内没有重复读同一个 CA），要再压只能减少「哪些读是必需的」（例如把 `lock_facts` 的部分读挪到入选后的 5 个标的上，而不是 20 个候选都读）。**未擅自改，等拍板。**

## 5.2 中止一轮

- **口径**：`POST /runs/{id}/cancel` 只在**进程内存里立一面旗**（`api/app/cancel.py`）就立刻返回 —— 一轮跑在 `schedule.py` 的单线程 worker 上，HTTP 处理器够不到那个线程，所以不进数据库。轮次自己在下一个检查点读旗、抛 `RoundCancelled`。旗按 run id 存、轮次结束清掉，所以下一轮绝不会继承上一轮的停止请求。
- **一轮没有回滚可做，这才是它能这么简单的原因**：落库只有最后一笔事务（`store_round` + `store_reports`），停在哪就是停在哪 —— 已经问出去的源、已经判过的标的留在日志与 `run_summary` 里，**报告一张也不写**。
- **检查点只用轮次本来就有的边界**（没有为中止新增计时器或线程）：`collect.py` 两个源之间；`decide.py` 每个阶段边界与 `_gate` 的批次循环头与**每个标的**、picks 那次兜底 enrich 之前；`draft.py` 词条草稿的**每一批之间**（批是先攒后写的，所以停在这里是「一条都没写」）；`llm/client.py` 一次调用的前 / 后与**读响应体的每一块之间**。
- **在途的模型答案可以被放弃**（`client.stream` 按块查旗，命中就关连接抛 `RoundCancelled`）；关键是**它不能退化成 `LLMUnavailable`** —— 否则一轮会用「没写点评」的样子把报告发出去，所以 `complete()` 里单开 `except cancel.RoundCancelled: raise`。
- **排队中的那一轮是唯一的例外**：没人开始跑就读不到旗，而 `schedule.recover` 会把停在 `pending` 的轮次再交给 worker 一次 —— 所以这一种由路由直接落库（`run.stop_run`），并且 `run_chain` 对已经是终态的轮次直接返回。
- **响应时间是秒到二十秒的量级**：拉取段按中止是秒级；安全判定中中止约 20 秒（`enrich_limit` 默认 20 个标的 × 8 并发；最慢的一档，因为「一批」是轮次原有的最小单位，再往下拆只会让每一轮都变慢）；写作段在下一次响应块到达时（单位是秒）。
- **故意留下的窗口**：写库之后不再有检查点 —— 停止请求若恰好在最后一笔事务提交之后到达，这一轮照常发布（已经发出去的报告不该被改写成「已中止」）。
- **前端**：`已中止` 的面板是折起来的（与跑完那一轮同一处理），标题 `已中止 · 本轮`，不自动收起；按 `收起` **或刷新**，报告页都退回**最近一轮有卡的批次**（`App.tsx`：最近一轮里第一个 `selected > 0` 或还没停的批次；开页只答一次，之后位置归翻页器）；没有卡片结果时不留白页。
- **新状态 `cancelled`**（不是故障，就不该记成 `failed`），`states.TERMINAL` 含它；测试在 `tests/test_cancel.py` + `tests/test_llm.py` + `tests/test_states.py`。

# 六、待决

## 6.1 需拍板的事（都没擅自改）

1. **硬过滤的松紧**：`market_cap_min_usd` = $50,000 是自定值（参考实现是 0）。代价是 GMGN 的 121 个去重标的里有 63 个市值 < $50K 被这条先淘汰，而 Top10 / 捆绑偏集中的 12 个标的一半落在里面 —— 这条下限顺手替掉了参考的集中度规则。降回 0 更接近参考，代价是候选里多一批微市值（含零数据）标的。
2. **同一行快照里 `market_cap_usd` 与 `price_usd × 供应` 对不上**（两个端点各自缓存、TTL 不同步，同一行拼了两个时刻的数），而「距高回撤」正好建在市值这一列：峰值滞后只让规则更保守，「现在市值」偏高会让回撤偏浅、规则更不容易触发。三条路 —— 用 `price_usd` 当序列 / 把市值那列的 TTL 与价格对齐 / 先修自洽再谈阈值。
3. **`num` + `enabled_key` 行的 `开启` 标签被折成两行**（`.chk` 被 `flex:0 1 auto` 压扁到 29×31px，与复选框长什么样无关）。三条路 —— `.chk{white-space:nowrap}` / 把 `开启` 缩成 `开` / 这一列不再可收缩。
4. **持有人的上限默认留空**（= 不设上限）：给它一个数就等于凭空开始砍币，而 Arc 上「市值 1–20 万的币持有人能到多少」没有实测过。
5. **`per_token`（本期点评）上限 300 字压不住**（实测 5 条全在 431–458）：放宽到 400，或者砍掉「下一步跟踪」。
6. **进程死在 `collected` / `scored` 中途时，那一轮和「正在跑」长得一样**（目前只恢复 `pending` 的轮）。要修得给轮次加心跳时间戳。
7. **深色主题的状态边框对比度不达标**（2.29 / 2.37 / 1.63:1，见 4.2）：要达标就把 `--ok-border` / `--warn-border` / `--danger-border` 各提一档，一行一个。
8. **2.5 第 9 条 bug 生效期写下的词条文字仍是错的**（11 行里只有 4 行会因判定变化被改写）。要立刻修就把那几行 `lexicon.research_state` 置回 `queued` 重跑深档（`research.queue` 会跳过 `done`）；注意已发布卡片的文字是冻结的，重写只影响下一轮。