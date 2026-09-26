# Crypto Hunter

按链、按周期的代币筛选 + 报告工厂：每 3 小时从一条链的几千个币里挑出 N 个（默认 5），每个出一份两分钟能读完的报告。

**研究流水线，不是行情终端。** 不做交易、不做信号、不追实时、不做多用户、不做回测。

一轮：`拉扫描源 → 去重 → 硬过滤 → 零数据淘汰 → 安全判定（限量）→ 打分排序（链上质量 55% + 赛道产品 45%）→ 词条与写作（LLM）→ 落库 → 出卡 → 归档 → 下一轮对比上一轮`

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

### 写手（可选）

`config/llm.env`（不入库，本机自己放）。**不配也能跑**：词条与点评会缺席，报告落 `draft`，卡片正文仍是确定性拼装出来的那份。

```
CH_LLM_BASE_URL=...
CH_LLM_API_KEY=...
CH_LLM_MODEL=...
CH_LLM_MAX_TOKENS=32000   # 推理模型的思考也吃这个额度，给小了会返回空消息（不是报错）
```

### 本机快捷方式

`scripts/` 下的三个脚本**不在仓库里**（`.gitignore` 忽略了它）：`dev.ps1`（后端后台 + Vite）、`build.ps1`（build + 起服务）、`openapi.ps1`（导出 schema + 重生成前端类型）。上面那几条原生命令与它们等价。

## 目录

| 路径 | 说明 |
| --- | --- |
| `api/` | FastAPI + SQLite(WAL)，uv 管理。`app/` 下：`api/v1`（HTTP）/ `core`（纯逻辑，**不许有 IO**）/ `pipeline`（一轮的编排）/ `report`（报告与 prompt）/ `sources`（一个源一个适配器）/ `store`（schema） |
| `web/` | React 19 + TS(strict) + Vite + TanStack Query，无 UI 组件库。`src/styles/` 是视觉的唯一真相（tokens → proto → app） |
| `config/` | `chains.yaml` 出厂默认设定（每条链一套，保存后以 `settings_versions` 表为准）；`llm.env` 本机私密配置 |
| `data/` | `crypto-hunter.db` + `raw/`（每轮原始响应与 HTTP 缓存）。不入库 |
| `api/app/sources/SOURCES.md` | 逐源端点、传输方式、实测结论（数据源口径） |

## 测试

```powershell
cd api; uv run python -m pytest     # 基线 365 绿
cd web; npm run build               # 类型不过就构建不过
```

## 数据源

| 用途 | 源 |
| --- | --- |
| 全链扫描 | GMGN / RadarDEX / Tolly / GeckoTerminal |
| 安全与源码 | GoPlus（貔貅判定、税、危险权限、LP 持有人表）/ Sourcify（源码验证） |
| 只补链接 | DexScreener（只填社媒 / 官网 / 描述，**不填任何进分的数**） |
| 链上直读 | 链自己的 RPC（LP 位置、dev 余额） |

## 现状与限制

- 只接了 **Arc**：左栏只渲染 `config/chains.yaml` 里启用的链。
- **TG 推送与导出没接**：占位按钮。一轮跑到那步会写「未配置（跳过推送，报告已落库）」并正常收尾，不阻塞。
- 一键重算返回 501（一轮的 universe 没落库）。
- 每轮默认 5 张卡，设定页可改；「距高回撤」确保同一个币不会连着上榜。
- 报告是研究材料，不是投资建议。

## 细节在哪

口径、契约红线、状态机、数据模型、视觉规则、环境坑与待决，全部在 **`CLAUDE.md`**（唯一真相来源）。这里不重复抄 —— 两份说法迟早会漂移。
