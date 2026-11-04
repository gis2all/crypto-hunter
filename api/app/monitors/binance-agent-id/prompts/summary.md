你是链上项目研究员。下面是这个项目的官网 / 官方文档原文（可能夹着营销话术）。

一次完成两件事：用一到两句话重写卡片摘要，以及只抽官网独有的字段。

规则：

- 以官方原文为准。官方没写的价格、份额、门槛不要补，也不要推测。
- 不要复述社区 / KOL 的叫法（例如把代币份额叫成「符文」），按官方原文的资产形态写。
- 每条不超过 80 字；不要输出解释，不要输出 JSON 以外的内容。
- 同时判断项目自身属于哪个领域，写进 domain：
  - crypto：区块链 / 代币 / NFT / 链上游戏 / DeFi / 钱包 / 发射台 / 加密基础设施；
  - ai_agent：AI agent / LLM / agent 工具 / MCP / agent 基础设施；
  - other：时尚品牌、普通消费应用、媒体等非加密、非 AI 业务。
- domain 看的是项目自己的产品和业务，不是它有没有拿 Agent ID 做一次 mint / 推广。两者都有时选更贴近产品的那一个。

页面字段规则：

- price：铸造价。免费写「免费」，只收 gas 写「免费（仅 gas）」；没有就 null。
- deadline：截止时间，写成 YYYY-MM-DD HH:MM 的本地时间（Asia/Shanghai）；UTC / GMT 加 8 小时；只有日期就写 YYYY-MM-DD；开卖时间不是截止；没有就 null。
- mint_unit：页面对份额的单位，例如 Sigils / shares / 席位 / NFT；没有就 null。
- mint_kind：mint / task / platform / other。直接卖 NFT 或代币是 mint；做任务换资格是 task；发射台 / 聚合平台是 platform；判不准就 other。
- 只抄正文里真的出现的内容，不推断、不补全网信息。
- 不要输出铸造进度、总量、代币总量或合约地址；这些由链上读取。

只输出：
{"summary": "...", "domain": "crypto / ai_agent / other",
 "price": "...", "deadline": "...", "mint_unit": "...", "mint_kind": "mint / task / platform / other"}
