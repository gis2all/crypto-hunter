"""The monitors this build knows, and what each one is.

A monitor is a question the reader wants answered again and again - "which projects take a
Binance Agent ID right now" - not a single project. Its queries, prompt and card fields live
here; the cadence and the query text can be overridden per machine in `config/monitors/`.

Adding a monitor is three pieces: an entry here, a prompt file beside this module, and a card
component in `web/src/features/monitors/`. That is the cost the reader accepted when choosing
one bespoke page per monitor (2026-10-29).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Monitor:
    id: str
    name: str
    tagline: str
    #: One X advanced search per entry; the slices come from the window below.
    queries: tuple[str, ...]
    #: System prompt, read from `monitors/prompts/<prompt>` at call time.
    prompt: str
    #: Stamped on every card so a prompt change is traceable.
    version: str
    #: Which card fields this monitor's page prints, in order.
    fields: tuple[str, ...]
    window_hours: int = 24
    slice_hours: int = 4
    #: The gate's floor. `rules.judge` scores the post; anything under this never reaches the model.
    min_score: int = 30
    #: How many gated posts one model call may see. The monitor is a sweep, not a census.
    max_posts: int = 100
    #: Defaults the settings page starts from; `config/monitors/<id>.yaml` overrides them.
    interval_hours: int = 8
    start_hour: int = 1
    start_minute: int = 15
    notes: tuple[str, ...] = field(default_factory=tuple)


BINANCE_AGENT_ID = Monitor(
    id="binance-agent-id",
    name="Binance Agent ID",
    tagline="接受 Agent ID 参与的铸造、白名单、任务与积分",
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
    notes=(
        "只收「用 Agent ID 作为参与资格」的项目；Binance 官方公告本身不建卡。",
        "价格 / 供应 / 截止只有材料写了才填，没写就是未判定。",
    ),
)


MONITORS: dict[str, Monitor] = {monitor.id: monitor for monitor in (BINANCE_AGENT_ID,)}


def get(monitor_id: str) -> Monitor | None:
    return MONITORS.get(monitor_id)
