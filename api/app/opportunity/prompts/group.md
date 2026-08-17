# 任务：把一批 X 帖子归并成机会候选

输入每条一行：

```text
[p1] @handle | score=105 | 帖子正文
```

规则：

- 每个 id 必须且只能出现在 `groups`、`irrelevant`、`uncertain` 三个桶之一。
 - 输出前逐条核对：三个桶里的 id 合起来必须正好等于输入的 N 条，不能只输出一部分。
- 同一项目的多条帖子合并成一个 group；不同项目不要合并。
- group 只写项目名、候选官方 handle、候选 URL、kind 和 ids；不写摘要、理由、步骤。
- kind 只能是 `activity` 或 `project`：帖子里有可执行动作（测试网、任务、空投、Mint、白名单等）用 `activity`；主要是在介绍新项目、协议或工具、没有具体动作用 `project`。
- handle / url 只是候选，不验证官网；没有就留空。
- 不确定的帖子放 `uncertain`，不要猜。
- 只输出 JSON，不要 Markdown，不要解释。

输出形状：

```json
{
  "groups": [
    {
      "name": "Spout Finance",
      "handle": "spoutfi",
      "urls": ["https://spout.finance"],
      "kind": "activity",
      "ids": ["p1", "p7"]
    }
  ],
  "irrelevant": ["p3"],
  "uncertain": ["p5"]
}
```
