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
    #: A row the pull carried as an address and nothing else: neither a market cap nor a
    #: reserve. Its own reason, not MISSING_DATA: the panel prints the two under different
    #: rows (空壳币 / 零数据淘汰), and this one is decided in the free half - before the
    #: five factors the other one counts exist.
    EMPTY_SHELL = "empty_shell"
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


def ca_key(ca: object) -> str:
    """One token identity, spelled the one way that makes two references the same token.

    An EVM address is hex, and hex is case-insensitive: folding it to lower case gives the
    same address, and that happens to be the spelling every EVM source answers with and the
    one every key, table and lexicon in this app has always held. Everything else is 32 bytes
    of base58 - Solana, and anything else shaped like it - and base58 is case-*sensitive*:
    folding it does not produce a sloppier spelling of the same address, it produces a
    different address, which the feed answers with "unknown" instead of with a correction.
    Left unfolded on purpose, so the two chains need one rule rather than two.
    """
    text = str(ca or "").strip()
    return text.lower() if text[:2].lower() == "0x" else text


#: The address GeckoTerminal and DexScreener both spell a chain's own coin with - no token is
#: behind it. GeckoTerminal answers it as the base token of a native pool it cannot resolve,
#: and DexScreener answers it with the native pairs of every chain at once.
ZERO_ADDRESS = "0x" + "0" * 40


def is_placeholder_ca(ca: object) -> bool:
    """True for a chain's own coin written as an address, which is not a token.

    Read as one it is the worst row a round can carry: its market cap is a blank, so every
    market-cap floor waves it through unjudged, and its liquidity is the whole chain's, so it
    heads any ranking it enters.
    """
    return ca_key(ca) == ZERO_ADDRESS


class TokenMetrics(BaseModel):
    """Pool/market level facts for one token in one round.

    Every field is optional on purpose: a missing value must surface as "not judged"
    rather than as a zero. `provenance` maps a field name to the source that supplied it.

    `price_series` is the 6-day price journey the card sparkline draws, oldest first.
    """

    ca: str
    symbol: str | None = None
    #: The token own square mark, as a URL. A link rather than the bytes: a card is copied
    #: around and archived, and an image would be one more thing to store, expire and clean
    #: up, while the picture itself is something the browser already knows how to fetch. Empty
    #: means no source had one, and the card draws its empty slot rather than nothing.
    logo_url: str | None = None
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


def share_site(row: TokenMetrics | dict | None, url: str) -> None:
    """Put the site a search just proved at the head of one row's websites.

    The card prints exactly one site, and until this ran it printed only what the sources
    listed - a token whose site the search learned off the project's own page had no 官网
    line on it at all. Prepend rather than append: the sites a union of launchpad rows
    produces arrive in whatever order the sources were read, and the page that was actually
    read and tied back to the token is the one worth printing.

    `row` is a TokenMetrics in the collect pass and a plain dict on the paths that never
    built one - a CA search, and the research pass filling in the row it was handed - so both
    are written. `search` joins the provenance list because it is one of the sources this
    union field is now built from, which is what keeps "where did this link come from"
    answerable.
    """
    url = str(url or "").strip()
    if row is None or not url:
        return
    if isinstance(row, dict):
        sites = [str(site) for site in (row.get("websites") or [])]
        provenance = dict(row.get("provenance") or {})
    else:
        sites = [str(site) for site in (row.websites or [])]
        provenance = dict(row.provenance or {})
    sites = [url] + [site for site in sites if site and site != url]
    names = [name for name in str(provenance.get("websites") or "").split(",") if name]
    if "search" not in names:
        names.append("search")
    provenance["websites"] = ",".join(names)
    if isinstance(row, dict):
        row["websites"] = sites
        row["provenance"] = provenance
    else:
        row.websites = sites
        row.provenance = provenance


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
