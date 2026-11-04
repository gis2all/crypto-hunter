"""The Binance Agent ID monitor: which AI / crypto projects use or support an Agent ID."""

from __future__ import annotations

from ..registry import Monitor

MONITOR = Monitor(
    id="binance-agent-id",
    name="Binance Agent ID",
    tagline="与 Agent ID 生态有真实关系的铸造、白名单、任务与集成",
    queries=(
        # 实体词组只留真正不同的 token：X 不分大小写、短语按连续 token 匹配，所以
        # "Binance Agent ID" / "币安 Agent ID" / ".ai Agent ID" 都被 "agent id" 覆盖；
        # "agent-id" 的连字符是分隔符，等价于同一个短语。Agentic Wallet 是另一种说法，
        # 单独列出（2026-10-30, user）。
        '("agent id" OR agentid OR agent_id OR #agentid'
        ' OR "agentic wallet" OR "agentic wallets")'
        ' (mint OR minting OR minted OR whitelist OR allowlist OR presale OR "public sale"'
        ' OR 铸造 OR 白名单 OR 预售 OR 公售)'
        ' -is:retweet -is:reply -giveaway -lottery -casino -gambling -sports -entertainment'
        ' -游戏 -派对 -AMA',
        '("agent id" OR agentid OR agent_id OR #agentid'
        ' OR "agentic wallet" OR "agentic wallets")'
        ' (quest OR quests OR points OR airdrop OR reward OR "check in" OR checkin'
        ' OR 积分 OR 空投 OR 任务 OR 测试网 OR 白名单任务)'
        ' -is:retweet -is:reply -giveaway -lottery -casino -gambling -sports -entertainment'
        ' -游戏 -派对 -AMA',
    ),
    prompt="binance-agent-id.md",
    version="monitor-agentid-v1",
    fields=("chain", "status", "price", "supply", "deadline"),
    min_score=35,
    notes=(
        "只收 AI / Agent / 加密领域中确实使用或支持 Agent ID 的项目；Binance 官方公告本身不建卡。",
        "价格 / 供应 / 截止只有材料写了才填，没写就是未判定。",
    ),
)
