"""Domain types: the contract between pipeline stages and the HTTP layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class DropReason(str, Enum):
    MARKET_CAP = "market_cap"
    LIQUIDITY = "liquidity"
    VOLUME_24H = "volume_24h"
    HOLDERS = "holders"
    TOP10 = "top10"
    BUNDLING = "bundling"
    HONEYPOT = "honeypot"
    #: Entrapment share above this chain's ceiling. Its own reason because the settings
    #: page shows it as its own row, separate from the GMGN honeypot flag.
    ENTRAPMENT = "entrapment"
    #: 距高回撤: this far below the highest market cap this project has seen for the
    #: token, and this far below the highest price its main pool traded at over the last
    #: ten days. Its own reason because the settings page shows it as its own row.
    MARKET_CAP_DRAWDOWN = "market_cap_drawdown"
    SOURCE_UNVERIFIED = "source_unverified"
    LP_UNLOCKED = "lp_unlocked"
    DANGEROUS_PERM = "dangerous_perm"
    MISSING_DATA = "missing_data"


class RunStatus(str, Enum):
    """Ordered stages. `researched` is the end: a stored report is never rewritten."""

    PENDING = "pending"
    COLLECTED = "collected"
    SCORED = "scored"
    SELECTED = "selected"
    WRITTEN = "written"
    RESEARCHED = "researched"
    #: Stopped by the operator while it was running. Terminal, and its own state rather
    #: than `failed`: nothing broke, and a page that calls a deliberate stop a failure
    #: a page that teaches the reader to ignore failures.
    CANCELLED = "cancelled"
    FAILED = "failed"


class TokenMetrics(BaseModel):
    """Pool/market level facts for one token in one round.

    Every field is optional on purpose: a missing value must surface as "not judged"
    rather than as a zero. `provenance` maps a field name to the source that supplied it.

    `price_series` is the 6-day price journey the card sparkline draws, oldest first.
    """

    ca: str
    symbol: str | None = None
    #: The project own blurb, off its listing page. No scanner on Arc carries one, so it
    #: arrives - when it arrives at all - from the per-token link read; the writer is the
    #: only reader that wants it, and "no source had a blurb" is itself worth printing.
    description: str | None = None
    market_cap_usd: float | None = None
    #: The highest market cap a *source* has on record for this token - GMGN's
    #: `history_highest_market_cap`. 距高回撤 reads it beside the pool own history and the
    #: high this project sampled: three readings of one number, and the deepest of them wins.
    high_market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    turnovers: float | None = None
    holders: int | None = None
    top10_pct: float | None = None
    bundling_pct: float | None = None
    honeypot_pct: float | None = None
    honeypot_note: str | None = None
    honeypot_detected: bool | None = None
    sniper_pct: float | None = None
    insider_pct: float | None = None
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None
    pools: int | None = None
    pool_volume_usd: float | None = None   # the round's combined 24h pool volume
    main_pool: str | None = None
    main_pool_share_pct: float | None = None
    price_usd: float | None = None
    price_series: list[float] = Field(default_factory=list)
    price_ath_usd: float | None = None
    drawdown_pct: float | None = None
    #: True only when the pool history was asked for and could not be read - a throttle or a
    #: timeout. The drawdown rule then answers 未判定 instead of reading that silence as a pass.
    drawdown_unread: bool | None = None
    price_change_24h_pct: float | None = None
    launchpad: str | None = None
    #: Who deployed it - what the LP question is asked about, so it must survive the merge.
    creator: str | None = None
    #: How often the X account was renamed. Cheaper to carry than to re-ask GMGN later.
    renames: int | None = None
    socials: dict[str, str] = Field(default_factory=dict)
    websites: list[str] = Field(default_factory=list)
    dex_pair: str | None = None
    gmgn_url: str | None = None
    debot_url: str | None = None
    dexscreener_url: str | None = None
    liquidity_rank: int | None = None
    volume_rank: int | None = None
    holders_rank: int | None = None
    transfer_tax_pct: float | None = None
    #: GoPlus's pool breakdown (`dex[]`), one entry per pool the token trades in:
    #: `{name, kind, liquidity_usd, fee_pct, pair, manager}`. The pool list the chain page
    #: shows, and where a v4 pool id comes from.
    pool_list: list[dict] = Field(default_factory=list)
    #: GoPlus's holder table (`holders[]`), summarised. Shares of supply sitting in a burn
    #: address, in a holder the feed marks locked, and in contracts (pool, locker, bridge).
    burn_pct: float | None = None
    locked_holder_pct: float | None = None
    contract_holder_pct: float | None = None
    #: The largest holders, for the card's 筹码 table: `{address, pct, contract, locked}`.
    top_holders: list[dict] = Field(default_factory=list)
    #: The LP holders, summarised the same way (`{address, pct, contract, locked}`). This is
    #: where the LP lock question is answered when the chain read cannot see the position -
    #: a v4 pool has no pool contract to ask.
    lp_holders: list[dict] = Field(default_factory=list)
    #: The anti-bot reads GMGN sells and we were leaving on the table.
    bot_degen_pct: float | None = None
    fresh_wallet_pct: float | None = None
    private_vault_pct: float | None = None
    bluechip_holder_pct: float | None = None
    degen_call_count: int | None = None
    signal_count: int | None = None
    #: When trading opened (GMGN's `open_timestamp`), and the supply figures.
    open_at: datetime | None = None
    total_supply: float | None = None
    circulating_supply: float | None = None
    max_supply: float | None = None
    created_at: datetime | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class SafetyFacts(BaseModel):
    """Cross-source safety evidence. None means "not established", never "safe"."""

    source_verified: bool | None = None
    source_verified_by: list[str] = Field(default_factory=list)
    #: The verifiers that answered at all, whether with a match or with "no record". The
    #: difference between the two is the difference between a no and a nobody-asked, and
    #: `source_verification_mode` reads it (core/filters.py).
    source_checked_by: list[str] = Field(default_factory=list)
    source_match: str | None = None
    #: The verified contract's name, when a verifier published one.
    source_name: str | None = None
    #: Deployer address. Comes from whichever source knows it; the LP question needs it.
    creator: str | None = None
    lp_locked: bool | None = None
    lp_note: str | None = None
    #: Which source answered 已锁: `gmgn` is the answer of record (its own security
    #: sheet carries `lock_summary`), and a value that came from there is not overwritten
    #: by the chain read, which cannot see a v4 position at all.
    lp_source: str | None = None
    dangerous_perms: list[str] | None = None
    dev_holding_pct: float | None = None
    entrapment_pct: float | None = None
    insider_pct: float | None = None
    sniper_pct: float | None = None
    bundling_pct: float | None = None
    top10_pct: float | None = None
    holders: int | None = None
    dev_created_count: int | None = None
    #: What the owner and the deployer still hold (GoPlus's `owner_percent` /
    #: `creator_percent`), a complement to `dev_holding_pct` read off the chain.
    owner_pct: float | None = None
    creator_pct: float | None = None
    #: Every remaining GoPlus flag, keyed by the feed's own name (`is_whitelisted`,
    #: `external_call`, `selfdestruct`, ...) and kept as answered. A dict rather than a
    #: dozen fields because nothing scores them; the card lists the ones that are set.
    flags: dict[str, bool] = Field(default_factory=dict)


class FilterVerdict(BaseModel):
    passed: bool
    failed: list[DropReason] = Field(default_factory=list)
    unknown: list[DropReason] = Field(default_factory=list)


class FactorScore(BaseModel):
    factor: str
    raw: float | None = None
    score: float = 0.0
    weight: float = 0.0
    note: str | None = None


class ScoreBreakdown(BaseModel):
    onchain: float | None = None
    product: float | None = None
    total: float | None = None
    factors: list[FactorScore] = Field(default_factory=list)
    pending_review: bool = False
    notes: list[str] = Field(default_factory=list)


class FunnelSummary(BaseModel):
    scanned: int = 0
    dropped: int = 0
    candidates: int = 0
    selected: int = 0
