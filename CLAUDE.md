# 1. 项目定位

## 产品定义

每条链按生效配置的周期自动跑一轮，从数千个代币里挑出 N 个，各附一份两分钟内可读完的判断。本项目是研究流水线，不是行情终端。交易、仓位建议、买卖点、实时行情、信号指标、多用户、回测引擎均不在范围内。

# 2. 技术栈

后端 Python 3.13、FastAPI、SQLite(WAL)、httpx（GMGN 需要 TLS 指纹，走 curl-cffi），由 uv 管理；前端 React、TypeScript(strict)、Vite、TanStack Query，不使用 UI 组件库，样式全部自写。

部署为同域单进程：FastAPI 托管 `web/dist`，仅一个端口 8787。排程由服务内 ticker 与 `runs` 表的唯一约束共同保证幂等（`run_key`），不使用 APScheduler。

# 3. 运行与验证

## 启动与构建

```powershell
cd api; uv run python -m app          # 后端 127.0.0.1:8787，同时托管 web/dist
cd web; npm install; npm run dev      # 前端 5173，/api 代理到 8787
cd web; npm run build                 # 生产：tsc -b 与 vite build，随后强制刷新浏览器
```

接口类型：Pydantic 是唯一真源 —— 先执行 `cd api; uv run python -m app.export_openapi`，再执行 `cd web; npm run types`；不得手写 DTO。

## 改动后的验证

- 修改 `.py` 后：`cd api; uv run python -m pytest -q` 必须全绿（不记条数），随后重启 API —— 它是常驻进程，不重启运行的仍是旧代码。
- 修改前端后：`cd web; npm run build`，退出码为 0（内含 `tsc -b`）。
- 修改 `prompts/*.md` 后：执行 `pytest tests/test_prompts.py`，prompt 内的 JSON 示例需与 `NewTokenEntry` 一致。
- 修改数据源解析后：执行 `pytest tests/test_<source>.py`，并清理对应前缀缓存。
- 长命令（build 与 pytest）应将输出重定向到日志后查看末尾，避免把整屏输出写入上下文。

# 4. 一轮流水线

拉取扫描源、去重、硬过滤第一半（市值区间与空壳淘汰）、逐币补齐链上数据（持有人、Top10、捆绑、24h 成交与市场口径行情）、硬过滤第二半与安全判定与距高回撤、链下调研与写作、打分排序、选取前 N、落库出卡、归档、下一轮对比上一轮。

| 阶段（时间线键） | 做什么 | 实现 |
| --- | --- | --- |
| `pull` | 每个扫描源拉一次全量 | `pipeline/collect.py` |
| `dedupe` | 按 CA 去重，逐字段按优先级合并 | `collect.py` |
| `prefilter` | 硬过滤第一半：粗筛行就能定的规则 | `core/filters.py` |
| `topup` | 逐币补齐持有人 / Top10 / 捆绑 / 24h 成交，并带回市场口径行情 | `decide.Engine._top_up` |
| `gate` | 硬过滤第二半 ＋ 安全判定与距高回撤，判完整个过关池 | `check_prefilter_enriched` ＋ `decide.Engine._gate` |
| `draft` | 链下调研与写作：过关池按流动性取前 `research_limit` 个 | `report/research.py` |
| `score` | 链上分位 ＋ 链下块 ＝ 综合分 | `core/scoring.py` |
| `sharpen` | 选出前 `per_round`，入选后补读 GeckoTerminal | `decide.Engine._picks` |
| `write` | 落库（一笔事务） | `pipeline/write.py` |

顺序依据：不产生费用的规则先跑（一张粗筛行即可判定），需要逐币读数的规则必须排在补齐之后 —— 未判定永不淘汰，因此先付费再裁决；安全判定又必须排在调研之前，否则调研名额会被深而质量差的行占满。

三种粒度不可混用：这九个键是耗时日志与运行面板的键；运行面板折成六格；进度条只用五段。新增或修改阶段需三处同时改动，细节见 `api/app/pipeline/README.md`。

# 5. 代码结构

- `api/app/api/v1/`：唯一 HTTP 层（`chains`、`runs`、`lexicon`、`settings`、`favorites`、`lookup`、`opportunity`、`system`、`health`），页面文案在 `forms.py`。见该目录 `README.md`。
- `api/app/pipeline/`：一轮的编排（`collect`、`decide`、`states`、`progress`、`run`、`schedule`、`write`）。见 `README.md`。
- `api/app/core/`：纯逻辑（`filters`、`funnel`、`scoring`、`models`、`parse`、`caliber`），不得有 IO；「0 是否算读数」只由 `parse.py` 与 `sources/base.py` 决定。
- `api/app/sources/`：一个源一个适配器，跨链的放根目录，只服务一条链的放 `<chain>/`。见 `README.md` 与该链 `SOURCES.md`。
- `api/app/report/`：事实表（全仓唯一一份）、卡片、装配、词条、调研编排、提示词。见 `README.md`。
- `api/app/search/`：挖链（识别、发现、抓取、引擎）。见 `README.md`。
- `api/app/opportunity/`：新机会（外部活动与新项目的发现）：watchlist、扫描 job、1–5 可参与机会（无动作的卡标「仅观察」）、整批分组 / 溯源 / 主题 / 抽取、链注册表自动核验（`chainverify` / `chain_registry`）、晋升门槛、读者自己的归档与删除。与一轮的编排无关，不写 `runs`。见 `README.md`。
- `api/app/store/`：SQLite（`schema.sql` 与 `db.py`）。见 `README.md`。
- `api/app/llm/`：写手客户端（key 轮换与冷却、两种请求形状、记账）；`list_models` 与 `probe_keys` 只服务系统设置页的两个按钮。
- `api/app/config.py`：链文件的读与写（路径常量、系统接线、配置的增删改、存档版本、patch 变形）。
- `api/app/system_settings.py`：系统设置页那张表，读写 `config/system.yaml` 并写入 `os.environ`。
- `api/app/lookup.py`：CA 搜索的全部工作，封装为一个可轮询的 job；不在一轮的编排内，也不访问 `runs`。
- `api/app/out/`：TG 推送的占位模块，`configured()` 返回 `False`。
- `web/src/features/`：一个页面一个目录；`web/src/components/` 存放通用件；`web/src/styles/` 是视觉的唯一出处。见两份 `README.md`。
- `config/`：`chains/*.example.yaml` 是出厂模板（跟仓库），同名的 `*.yaml` 是本机那份（首次读取时从模板播种，不入库）；`system.yaml` 是本机私密接线（不入库）；`opportunity.example.yaml` 是新机会那条流水线十个旋钮的模板，本机那份 `opportunity.yaml` 由设定页保存时创建，只写被改过的键，删掉即回默认。当前四条链：Arc / Robinhood / Solana / BSC；左栏只渲染 `enabled` 的那条。
- `data/`：`crypto-hunter.db` 与 `raw/`（每轮原始响应与 HTTP 缓存），不入库。

# 6. 术语

- 扫描、淘汰、候选、入选：一轮的四个数（`scanned`、`dropped`、`candidates`、`selected`），候选包含入选，四个数一次数出，`scanned = dropped + candidates` 是恒等式。
- 未判定：表示未问到，不等于不合格 —— 既不计入通过也不计入淘汰。
- 判定池：通过硬过滤、送入安全判定的那一批，默认全部判定（`gate_limit: 0`，负数按 0 处理）。
- 调研池：过关池中按流动性取前 `research_limit` 个、本轮实际写入词条的那一批（0 表示全部）。
- 链上质量与链下调研：综合分的两半，默认权重 55% 与 45%。
- 词条：一个代币在本轮的链下调研结果（`lexicon` 表一行）；本轮未完成调研的标的为 `pending`，不参与排名，也不显示 0 分。
- 距高回撤：距历史最高点的跌幅。
- 貔貅：可买不可卖；它与陷阱占比（GMGN 的 `top_entrapment_trader_percentage`）不是同一个数。
- 发射台：launchpad，新币多数由其发行，每条链各有自己的发射台。
- 主池：流动性最大的池。
- `0`：是答案而非缺失 —— 判空用 `is not None` 而非 `if value:`；集合类型的 `[]` 视为「未答」，不赢走字段。
- 收藏与搜索记录：同一张 `favorites` 行（主键 `(chain_id, ca)`），区别仅在于 `origin` 与 `kept`；链名、配置、轮次只是随行存储的值，不发生任何级联。
- 市场口径：一个数的最终读法以交易市场为准（DexScreener 的 `liquidity.usd` 与 `h24`），而非榜单或发射台自报的数值。

# 7. 变更入口

| 变更目标 | 读 | 动 |
| --- | --- | --- |
| 新增或修改一个数据源 | `api/app/sources/README.md` 与该链 `SOURCES.md` | 通用源 `api/app/sources/`，链专属源 `api/app/sources/<chain>/`；新源必须同时进入 `registry.ALL`、`FIELD_PRIORITY` 与 `DEFAULT_PRIORITY` |
| 调整硬过滤默认值 | `api/app/pipeline/README.md` · 硬过滤 | `config/chains/*.example.yaml`；本机那份是同名 `*.yaml`，保存后以 `settings_versions` 为准 |
| 修改评分权重或因子 | `api/app/pipeline/README.md` · 打分 | `core/scoring.py` 与 `config/chains/*.example.yaml` |
| 修改卡片展示内容 | `web/src/features/README.md` · 报告卡 | `api/app/report/cards.py` |
| 修改卡片文案或结论写法 | `api/app/report/README.md` · 词条文字 | `api/app/report/prompts/research.md` 与 `report/schema.py` |
| 修改设定页字段或说明文案 | `api/app/api/v1/README.md` · 设定页 | `api/app/api/v1/forms.py` 与 `web/src/features/settings/` |
| 新增或删除一套配置 | `api/app/api/v1/README.md` · 设定页 | `api/app/api/v1/chains.py`、`config/chains/*.yaml` 与 `settings/ConfigManager.tsx` |
| 修改视觉 | `web/src/styles/README.md` | `web/src/styles/tokens.css`（先改此处，再动 `proto.css` 与 `app.css`） |
| 修改一轮的阶段或顺序 | 本文件第 4 节（一轮流水线）与 `api/app/pipeline/README.md` | `api/app/pipeline/decide.py`、`progress.py` 与运行面板的 `STAGE_PHASES` |
| 新增或修改数据表 | `api/app/store/README.md` | `api/app/store/schema.sql` 与 `store/db.py` |
| 修改收藏页 | `web/src/features/README.md` · 收藏页 | `api/app/api/v1/favorites.py`、`report/assemble.py` 与 `web/src/features/favorites/` |
| 修改 CA 搜索 | `web/src/features/README.md` · CA 搜索 | `api/app/lookup.py`、`api/app/api/v1/lookup.py` 与 `web/src/features/lookup/` |
| 修改新机会（watchlist、扫描、分组与抽取、溯源、链核验、主题、晋升门槛、用户状态、页面与设定） | `api/app/opportunity/README.md` | `api/app/opportunity/`、`api/app/api/v1/opportunity.py` 与 `web/src/features/opportunity/`；watchlist 种子在 `opportunity/seeds/watchlist.yaml`，链注册表种子/同步在 `chainverify.py`，旋钮在 `opportunity/settings.py` 的 `FIELDS` 与 `config/opportunity.example.yaml` |
| 修改本机接线（LLM、X 搜索、日志） | `api/app/api/v1/README.md` · 系统设置（本机接线） | `config/system.yaml`、`api/app/system_settings.py` 与 `web/src/features/system/` |
| 第 8 节（边界与待决）中的待决事项 | 先与用户确认 | 第 8 节 |

# 8. 边界与待决

尚未接入的部分（不是缺陷）：

- TG 推送：`api/app/out/` 是占位模块，一轮执行到该步时写入「未配置（跳过推送，报告已落库）」并正常收尾；顶栏「推送 TG」与「导出」均带 `disabled`。
- 一键重算：`POST /settings/recompute` 返回 501。
- 搜索未配置 cookie：X 搜索整层静默失效，卡片只使用本轮登记的链接，不阻塞流程。
- 两项「看似未接入、实为有意如此」：链下调研未完成的标的本轮不参与排名（不是 0 分）；`holders_max` 默认 `null` 即不设上限。

待决事项（改动前须先与用户确认）：

- 硬过滤的松紧；同一快照内 `market_cap_usd` 与 `price_usd × 供应` 对不上；设定页 `开启` 标签折行；持有人上限默认留空。
- worker 线程卡住（进程仍存活）时，该轮与「正在跑」状态无法区分；深色主题的状态边框对比度不达标。
- 链上读路径已无可去除的重复调用；链下调研五项的边界；分位分母为混口径（补齐过的行是市场口径，其余仍是粗筛口径），改动需要重跑一轮对比。

# 9. 设计取舍

- 分层不成环：前端不接触数据源与数据库；后端不内嵌 HTML（只托管 `web/dist`）；`api/app/core/` 内不得有 IO。
- 一个源一个适配器，源与源互不感知：同名指标的取值由 `pipeline/collect.py` 的 `DEFAULT_PRIORITY` 与 `FIELD_PRIORITY` 决定，与调用顺序无关。
- 文案只在后端：页面上的解释性文案由 `api/app/api/v1/forms.py` 提供，前端不识别任何一条。例外是一轮的「口径」三行位于 `core/caliber.py`：落库与页面都需输出同一句，两边各写一份即产生漂移。
- 分数只在后端计算：前端不计分、不补数，页面上的每个数字都来自 `/api/v1`。
- 一轮只写一次：卡片文字在落库时即为终稿，没有任何阶段回头改写已发布的报告。
- 缺值语义为未知：缺失的数是 `None` 或空，不得用 0 或空字符串替代。
- 缺值只会是「未知」，不会变成放行：把未判定当作通过，上千条发射台自报的行会直接进池；反之把「对方回答没有」当作失败，Arc 上多数代币每轮都需重新查询。
- 一个静默的空开关比缺少一个旋钮更糟：设定页上无人读取的开关，勾选与否结果完全相同，会让用户误认为它已生效：要么接通行为，要么删除该动作。
- 事实只写一处：同一件事在两处各写一份，两边就会漂移；落地即冻结的三张表（`settings_versions`、`snapshots`、`reports`）就是这条规则的机器化实现。
- 先付费再裁决：需要读取单个代币才能确定的数，判决只能在补齐之后作出；反之，粗筛行即可确定的数不应等待付费环节。
- 排序键应选择「各来源答案一致」的那个：以未校准的读数作主键时，改动一个阈值就会整体重排池子。
- 不可观测即不可复核：日志是唯一可复核阶段耗时与进度的出处（`GET /api/v1/system/log?tail=` 读 `log/api-run.err.log` 尾部，`GET /api/v1/system/logs?since=` 读进程内最近 500 条），数据库只保留结论。

# 10. 文档约定

- 每个大模块一份 `README.md`：`api/app/sources/`、`api/app/pipeline/`、`api/app/report/`、`api/app/search/`、`api/app/opportunity/`、`api/app/store/`、`api/app/api/v1/`、`web/src/features/`、`web/src/styles/`。
- 链级数据源细节在 `api/app/sources/<chain>/SOURCES.md`；提示词在 `api/app/report/prompts/research.md` 与 `api/app/opportunity/prompts/`。
- 规则不编号：定位一条规则用模块文档的小节名，`rg` 搜小节名或规则里的关键词即可；跨文档引用写「见 `<模块>/README.md` · 小节名」，同文档内写「见本文件 · 小节名」。
- 模块引用用短名：`sources/` 到 `api/v1/` 在 `api/app/` 下，`features/` 与 `styles/` 在 `web/src/` 下。
- 维护范围：分层、契约、运行方式、数据模型、视觉规则发生变化时更新本文件；改动代码的那一次同步修改对应模块文档；口径变化时直接改写原条目，不追加「某天起改为…」。
- 唯一出处：视觉为 `web/src/styles/`，数据模型为 `api/app/store/schema.sql`，出厂设定为 `config/chains/*.example.yaml`。

# 11. 注意事项

- 不使用 `&&`，改用 `;` 或 `if`；含 `&` 的 URL 需要加引号。
- 所有写入文件的操作用无 BOM 的 UTF-8；不得用 PowerShell here-string 写非 ASCII 内容，那些字符会被写成 `?`。
- 终端把中文显示成 `????` 属于显示问题，文件本身没有损坏：应以 UTF-8 读取确认后再下结论。
- 保存设定会用 `os.replace` 换掉 `config/chains/<chain>.yaml`；目标文件正被别的进程读取时抛出 `PermissionError [WinError 5]`，因此全套测试中偶发一条失败（`test_a_text_knob_can_still_be_cleared`），单独运行时通过、重跑也通过 —— 属于文件抢占，不是代码缺陷。
- 服务常驻 `127.0.0.1:8787` 并托管 `web/dist`：前端改动后需要 build 并强制刷新浏览器。
