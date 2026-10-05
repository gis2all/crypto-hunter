# Crypto Hunter

按链、按周期的代币筛选 + 报告工厂：每 3 小时从一条链的几千个币里挑出 N 个（默认 5），每个出一份两分钟能读完的报告。

**研究流水线，不是行情终端。** 不做交易、不做信号、不追实时、不做多用户、不做回测。

一轮：`拉扫描源 → 去重 → 硬过滤（不花钱那半：市值区间 ＋ 空壳淘汰）→ 补齐链上数据（逐币补持有人 / Top10 / 捆绑 / 24h 成交 ＋ 市场口径行情）→ 硬过滤（花钱那半：流动性 / 24h 成交下限 ＋ 筹码 ＋ 零数据）→ 安全判定与距高回撤（判完整池）→ 链下调研与写作（LLM：X 搜索挖链接 + 读页面 + 写词条；过关池按流动性取前 N）→ 打分排序（链上质量 55% + 链下调研 45%）→ 选前 N → 落库出卡 → 归档 → 下一轮对比上一轮`

## 快速开始

前置：Python 3.13（`>=3.13,<3.14`）+ uv，Node 20+（Vite 6）+ npm。

```powershell
# 后端：127.0.0.1:8787。生产也只有一个进程 —— 它同时托管 web/dist。
uv run --project api python -m app

# 前端（开发）：5173，/api 代理到 8787
cd web; npm install; npm run dev
```

- 生产：`cd web; npm run build`（内部是 `tsc -b` + `vite build`），再起上面那条后端命令。前端改完要重新 build 并强刷浏览器。
- 改过 `.py` 之后要重启后端才生效（常驻进程，不重启跑的还是旧代码）。
- 接口类型：`cd api; uv run python -m app.export_openapi` → `cd web; npm run types`。**Pydantic 是唯一真源，不许手写 DTO。**
- 一条链不止一套设定：设置页顶栏最左的两个控件管这件事 —— 配置下拉选**读**哪一套，`配置管理` 负责新建 / 复制 / 改名 / 删除；它们右边的 `保存并生效` 才会把它变成该链**真正在跑**的那套（同一条链同一时刻只有一套生效）。删一套会连它的报告库一起删掉，默认那套删不掉。

### 写手与搜索（可选）

一个 `config/system.yaml`（写手 ＋ X 搜索登录态 ＋ 日志等级），在左栏最下面的 **系统设置** 里改，也可以直接编这份文件。**改完下次调用生效**（日志等级当场生效），不用重启。这份文件不入库、本机自己放；模板见 `config/system.yaml.example`。网关那一段有三件省事的：**预设**一键填地址、**获取模型**把端点回的模型列出来直接选（也可以手填）、**测试连接**对每一把 key 各打一次最小调用，看它到底答不答得上话 —— 401/429 这类拒绝会把网关的原话印出来。X 会话那一行也有个**测试连接**：只读一次你自己的账号，确认这串 cookie 还是登录态（不搜关键词，不花搜索额度）。

**不配也能跑**：写手缺席则链下调研缺席 —— 判定池里的币没有词条、不进排名（本轮 note 会写明）；搜索缺席只让卡片少一批外部材料。两条路都不阻塞流程。

```
llm:
  base_url: https://...              # OpenAI 兼容网关
  api_key: sk-main,sk-standby        # 逗号分隔可写多个，第一个是主 key，后面备用
  model: ...
  reasoning: none                    # 思考预算，省时省钱的大头；不填=端点默认（高档思考）
  max_tokens: 32000                  # 推理模型的思考也吃这个额度，给小了会返回空消息（不是报错）
research_workers: 12                 # 一次并行调研几个币
x:
  auth_token: ...                    # 一个已登录 x.com 会话的 cookie（用小号）
  ct0: ...
log_level: INFO                      # DEBUG / INFO / WARNING / ERROR
```

全部变量见 `config/system.yaml.example`（页面上就是左栏最下面的「系统设置」）。某个 key 返回 401/403/429（失效或额度用尽）会自动换下一个，并把换掉的那个晾 `key_cooldown` 秒（默认 600，0 关闭）。

### 本机快捷方式

`scripts/` 下的三个脚本**不在仓库里**（`.gitignore` 忽略了它）：`dev.ps1`（后端后台 + Vite）、`build.ps1`（build + 起服务）、`openapi.ps1`（导出 schema + 重生成前端类型）。上面那几条原生命令与它们等价。

## 目录

| 路径 | 说明 |
| --- | --- |
| `api/` | FastAPI + SQLite(WAL)，uv 管理。`app/` 下：`api/v1`（HTTP）/ `core`（纯逻辑，**不许有 IO**）/ `pipeline`（一轮的编排）/ `report`（报告与 prompt）/ `search`（挖链：X 搜索与判别）/ `sources`（一个源一个适配器）/ `store`（schema） |
| `web/` | React 19 + TS(strict) + Vite + TanStack Query，无 UI 组件库。`src/styles/` 是视觉的唯一真相（tokens → proto → app） |
| `config/` | `chains/*.example.yaml` 出厂默认设定模板（一条链一个文件，文件里的 `configs` 是一套套设定，各有一套报告库，保存后以 `settings_versions` 表为准；页面上可增删改名，只有一套生效）；同名的 `chains/*.yaml` 是本机那份（首次读时从模板播种，页面的写入都落它，不入库）；`system.yaml` 本机私密接线（LLM 网关 / X 会话 / 日志等级；配 `.example` 模板，页面上是「系统设置」） |
| `data/` | `crypto-hunter.db` + `raw/`（每轮原始响应与 HTTP 缓存）。不入库 |
| `api/app/sources/<链>/SOURCES.md` | 逐源端点、传输方式、实测结论（数据源口径），含 X 搜索挖链层。链专属源放 `sources/<链>/`，通用源在 `sources/` 根目录；目前只有 `arc/` 那份（Robinhood 待补） |

## 测试

```powershell
cd api; uv run python -m pytest     # 必须全绿（0 failed），不记条数
cd web; npm run build               # 类型不过就构建不过
```

## 数据源

| 用途 | 源 |
| --- | --- |
| 全链扫描 | Arc：GMGN / RadarDEX / Tolly；Robinhood：GMGN / CoinBarrel / PEZ / Peeps；GeckoTerminal 两链都算 |
| 安全与源码 | GoPlus（貔貅判定、税、危险权限、LP 持有人表）/ Sourcify（源码验证） |
| 市场口径与链接 | DexScreener（`market_cap_usd` / `liquidity_usd` / `volume_24h_usd` / `price_usd` 的第一优先源，同时补社媒 / 官网 / 描述） |
| 挖链找页面 | X 高级搜索（`app/search/`，一次 subprocess 不吃 token）+ 读页面互证 |
| 链上直读 | 链自己的 RPC（LP 位置、dev 余额） |

## 现状与限制

- 已接 **Arc** 与 **Robinhood**（`bsc.yaml` 出厂是关闭的）：左栏只渲染 `config/chains/*.yaml` 里 `enabled: true` 的链。
- **导出与 TG 推送都按不动**：两个按钮都带 `disabled`（`web/src/features/report/TopBar.tsx`）。导出已经把下载接上了（`web/src/App.tsx` 的 `onExport`），只是按钮还锁着；TG 推送完全没接，一轮跑到那步会写「未配置（跳过推送，报告已落库）」并正常收尾，不阻塞。
- 一键重算返回 501（一轮的 universe 没落库）。
- 每轮默认 5 张卡，设定页可改；「距高回撤」读三个高点估计（源高点 / 库记录高点 / 池面全历史），任一跌够 70% 就不再上榜。
- 报告是研究材料，不是投资建议。

## 细节在哪

口径、契约红线、状态机、数据模型、视觉规则、环境坑与待决，全部在 **`CLAUDE.md`**（唯一真相来源）。这里不重复抄 —— 两份说法迟早会漂移。
