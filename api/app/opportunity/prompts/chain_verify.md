# 任务：判断一个名字是不是公开区块链，以及它的类型

输入是若干项目（`key` / `name`）以及各自的 `pages`（官网、文档或搜索结果，带 `url` 和
`text`）。这些名字不在 Chainlist / CoinGecko / DefiLlama 里，需要你只依据页面原文判断。

**不要凭印象补全。** 页面没有说它是区块链，就判 `unverified`。

## 类型

- `l1`：自己的共识层与区块，独立 L1；
- `l2`：依附于另一条链的 rollup / sidechain；
- `appchain`：Cosmos / Polkadot 这类应用链（有独立链，但属于某个生态）；
- `testnet`：明确只提供测试网；
- `unverified`：页面不足以判断。

## 公开性

`is_public = true` 需要页面明确写出：主网 / 测试网、公开 RPC、区块浏览器、公开可用。
只是“即将上线”“规划中”“内部测试”不足以判为公开。

## 输出（严格 JSON）

输入有几个 `key`，`items` 就有几条；`key` 原样抄回。

```json
{
  "items": [
    {
      "key": "example",
      "type": "l1",
      "is_public": true,
      "evidence_url": "https://example.com/docs",
      "quote": "Example is a Layer 1 blockchain ...",
      "confidence": "high"
    }
  ]
}
```

- `evidence_url` 必须是输入里出现过的 URL 之一；
- `quote` 必须是那一页的原文短句，不能改写；
- 判断不了就 `type = "unverified"`、`is_public = false`、`evidence_url` 和 `quote` 留空。
