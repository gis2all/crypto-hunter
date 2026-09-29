"""Step 1: pull the chain and merge what each source knows.

The merge is field-wise, not source-wise. Sources disagree - one is a minute stale, one
counts a pool the other has not indexed - and picking a whole row from one source would
make every number on a card inherit that source's blind spots. Per field, the first
source in `FIELD_PRIORITY` that has a value wins, and the winner is recorded in
`provenance`, so "where did this market cap come from" is answerable after the fact.

Nothing here decides anything. A source that fails is recorded as failed and the round
continues: one dead feed must not empty the funnel, it must show up as a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import cancel
from ..core.models import TokenMetrics
from ..sources.base import SourceReport
from ..sources.http import archive

#: Which source is believed first, per field. GMGN sees live swap flow and is the
#: authority on the numbers people trade against; GeckoTerminal is the only source that
#: can enumerate the chain; RadarDEX fills the rest and is the only one that ships a
#: deployer address at scale.
#:
#: `dexscreener` leads the four market numbers although it is a per-token read, because
#: it is the caliber every market rule is supposed to run on. Its liquidity sums every
#: pool the token holds on this chain, where the launchpad feeds run 5.52x high (measured
#: 2026-09-29 on Tolly rows: 54,783.59 against a real 9,930.26); its h24 volume is a
#: 24-hour figure, where GMGN's rank row answers in the window it happened to rank in. It
#: has no bulk sweep (`fetch_universe` returns nothing), so it can only ever win the
#: enrich merge - the pull never has an opinion for it to override.
FIELD_PRIORITY: dict[str, tuple[str, ...]] = {
    "symbol": ("gmgn", "radardex", "tolly", "geckoterminal", "blockscout"),
    "gmgn_url": ("gmgn", "geckoterminal", "radardex"),
    "debot_url": ("gmgn", "geckoterminal", "radardex"),
    "market_cap_usd": ("dexscreener", "gmgn", "geckoterminal", "radardex", "tolly",
                       "blockscout"),
    # The token own high. Only GMGN reports one (its `history_highest_market_cap`), and it
    # is not interchangeable with the pool history below it: GeckoTerminal answers the same
    # question in a price, and no merge can convert one into the other. Listed explicitly so
    # a future source cannot quietly outrank the one that measured it.
    "high_market_cap_usd": ("gmgn",),
    "liquidity_usd": ("dexscreener", "gmgn", "geckoterminal", "radardex", "tolly",
                      "blockscout"),
    "volume_24h_usd": ("dexscreener", "gmgn", "geckoterminal", "radardex", "tolly",
                       "blockscout"),
    "price_usd": ("dexscreener", "gmgn", "radardex", "tolly", "geckoterminal",
                  "blockscout"),
    # GMGN answers this with the *ranking window's* change, not the 24-hour one:
    # `price_change_percent` read -0.22% on the 5m board and +0.35% on the 24h board for
    # the same token (measured 2026-09-29). The adapter gates it on the window, and the
    # two sources that ship a real `change24h` sit behind it. Listed explicitly so the
    # default order cannot let a wrong-window reading win by accident.
    "price_change_24h_pct": ("gmgn", "radardex", "tolly", "geckoterminal", "blockscout"),
    "holders": ("gmgn", "radardex", "blockscout", "goplus"),
    "top10_pct": ("gmgn", "radardex", "goplus"),
    "bundling_pct": ("gmgn", "radardex"),
    "sniper_pct": ("gmgn", "radardex"),
    "insider_pct": ("gmgn", "radardex"),
    "honeypot_pct": ("gmgn",),
    "honeypot_detected": ("goplus", "gmgn"),
    # Only GMGN measures these: GoPlus answers "0" for every Arc token it is asked
    # about (measured 24/24 on 2026-09-25), so listing it as a fallback would turn its
    # placeholder into the card's number. A token GMGN has no sheet for shows "—".
    "buy_tax_pct": ("gmgn",),
    "sell_tax_pct": ("gmgn",),
    # The chain reads only GoPlus answers: its pool breakdown, its largest-holder table and
    # the shares that table implies, and the transfer tax no other feed reports. Listed
    # explicitly so a future source cannot quietly outrank the one that measured them.
    "transfer_tax_pct": ("goplus",),
    "pool_list": ("goplus",),
    "top_holders": ("goplus",),
    "lp_holders": ("goplus",),
    "burn_pct": ("goplus",),
    "locked_holder_pct": ("goplus",),
    "contract_holder_pct": ("goplus",),
    # The anti-bot reads, from GMGN's per-token statistics.
    "bot_degen_pct": ("gmgn",),
    "fresh_wallet_pct": ("gmgn",),
    "private_vault_pct": ("gmgn",),
    "bluechip_holder_pct": ("gmgn",),
    "degen_call_count": ("gmgn",),
    "signal_count": ("gmgn",),
    "open_at": ("gmgn",),
    "total_supply": ("gmgn", "goplus"),
    "circulating_supply": ("gmgn", "goplus"),
    "max_supply": ("gmgn", "goplus"),
    "launchpad": ("gmgn", "radardex", "tolly", "blockscout"),
    "creator": ("gmgn", "radardex", "tolly"),
    "renames": ("gmgn",),
    "dex_pair": ("geckoterminal",),
    # Only the pool history answers these two, and it is the only reading that can fail to -
    # see `filters.check_drawdown`.
    "drawdown_pct": ("geckoterminal",),
    "drawdown_unread": ("geckoterminal",),
    "main_pool": ("geckoterminal", "tolly"),
    "pools": ("geckoterminal", "radardex"),
    "price_series": ("geckoterminal",),
    "created_at": ("radardex", "tolly", "geckoterminal"),
    # The project’s own blurb. Only the per-token link read has one (see
    # `sources/dexscreener.py`), so an empty 项目自述 line means no source carried it -
    # which is worth printing rather than leaving out.
    "description": ("dexscreener",),
}
DEFAULT_PRIORITY = ("gmgn", "radardex", "tolly", "geckoterminal", "blockscout", "goplus", "sourcify",
                    "dexscreener")

#: Per-token pages on the trackers a card links to. One place, so a URL shape cannot drift
#: between the sources that write it. DexScreener answers a token at the same path shape it
#: answers a pool (`/chain/address`), which is why this one is the CA and not the pool id the
#: pair feed carries - measured on 2026-09-26: `dexscreener.com/arc/<CA>` opens that token's
#: page, pools and all.
TOKEN_URLS = {
    "gmgn": "https://gmgn.ai/%s/token/%s",
    "debot": "https://debot.ai/token/%s/%s",
    "dexscreener": "https://dexscreener.com/%s/%s",
}


def token_url(tracker: str, chain_id: str, ca: str) -> str:
    return TOKEN_URLS[tracker] % (chain_id, ca)
SCALARS = tuple(
    name
    for name in TokenMetrics.model_fields
    if name not in ("ca", "provenance", "socials", "websites", "price_series")
)


@dataclass
class Universe:
    metrics: dict[str, TokenMetrics] = field(default_factory=dict)
    reports: list[SourceReport] = field(default_factory=list)
    per_source: dict[str, int] = field(default_factory=dict)
    #: Every source's own view, kept so the enrichment stage can add to it and re-merge.
    #: Merging is lossy (one winner per field), and a second merge has to start from the
    #: first one's inputs, not from its output.
    rows: dict[str, dict[str, TokenMetrics]] = field(default_factory=dict)


def merge(ca: str, by_source: dict[str, TokenMetrics], priority: tuple[str, ...] = DEFAULT_PRIORITY) -> TokenMetrics:
    """One token's merged view, field by field, with the winner recorded."""
    out = TokenMetrics(ca=ca)
    provenance: dict[str, str] = {}
    for name in SCALARS:
        for source in FIELD_PRIORITY.get(name, priority):
            row = by_source.get(source)
            value = getattr(row, name) if row is not None else None
            # An empty list is "this source has none", not "the answer is none". Without
            # this the first source in the order that carries a list field at all - usually
            # a bulk row whose list defaulted to empty - wins the field, and the source that
            # actually answered it never gets read.
            if value is None or (isinstance(value, (list, dict)) and not value):
                continue
            setattr(out, name, value)
            provenance[name] = source
            break

    # The three collection fields are unions, not winners: four sources each knowing one
    # social link is four links, not a disagreement to be settled.
    socials: dict[str, str] = {}
    social_from: set[str] = set()
    for source in reversed(priority):
        row = by_source.get(source)
        if row and row.socials:
            socials.update(row.socials)
            social_from.add(source)
    if socials:
        out.socials = socials
        provenance["socials"] = ",".join(name for name in priority if name in social_from)

    websites: list[str] = []
    web_from: set[str] = set()
    for source in priority:
        row = by_source.get(source)
        for url in (row.websites if row else []):
            if url and url not in websites:
                websites.append(url)
                web_from.add(source)
    if websites:
        out.websites = websites
        provenance["websites"] = ",".join(name for name in priority if name in web_from)

    for source in FIELD_PRIORITY["price_series"]:
        row = by_source.get(source)
        if row and row.price_series:
            out.price_series = row.price_series
            provenance["price_series"] = source
            break

    out.provenance = provenance
    return out


def overlay(base: TokenMetrics | None, extra: TokenMetrics) -> TokenMetrics:
    """`extra`'s filled-in fields over `base`. Used when a source answers twice - once in
    bulk and once about a single token - and neither answer may lose to the other."""
    if base is None:
        return extra
    filled = {
        name: getattr(extra, name)
        for name in TokenMetrics.model_fields
        if name not in ("ca", "provenance")
        and getattr(extra, name) not in (None, [], {})
    }
    return base.model_copy(update=filled)


def merge_all(per_source: dict[str, dict[str, TokenMetrics]]) -> dict[str, TokenMetrics]:
    names = [name for name, rows in per_source.items() if rows]
    ordered = tuple(sorted(names, key=lambda name: DEFAULT_PRIORITY.index(name) if name in DEFAULT_PRIORITY else len(DEFAULT_PRIORITY)))
    seen: set[str] = set()
    for rows in per_source.values():
        seen.update(rows)
    return {ca: merge(ca, {name: per_source[name][ca] for name in ordered if ca in per_source[name]}, ordered) for ca in seen}


def set_scan_floor(sources: list, floor: float | int | str | None) -> None:
    """Tell the sources that can stop early where this round draws its line.

    The floor is the chain's own `prefilter.market_cap_min_usd`, so a scanner is never told
    to stop above the line its own round will filter on. Sources are module-level
    singletons and one round at a time runs against a chain, so the value is set on them
    rather than threaded through `fetch_universe`. A source without the knob ignores this,
    and an unusable value becomes 0 - which means read everything, not read nothing.
    """
    try:
        value = max(0.0, float(floor or 0))
    except (TypeError, ValueError):
        value = 0.0
    for source in sources:
        if hasattr(source, "market_cap_floor"):
            source.market_cap_floor = value

def collect(chain: dict, run_id: str, sources: list,
            on_source: Callable[[int, int], None] | None = None,
            on_pulled: Callable[[], None] | None = None) -> Universe:
    """Pull every configured source once. A failing source is a report, not an exception.

    `on_source(done, total)` fires after each source: the pull is minutes of network and
    nothing else in the round reports from inside it. `on_pulled` fires once, after the
    last source has answered and before the merge - the boundary between the round's
    network time and its arithmetic, which the run panel draws as two rows.
    """
    tell = on_source or (lambda _done, _total: None)
    limit = int((chain.get("pipeline") or {}).get("scan_limit") or 0)
    collected: dict[str, dict[str, TokenMetrics]] = {}
    reports: list[SourceReport] = []
    per_source: dict[str, int] = {}
    for index, source in enumerate(sources):
        # A stop between two sources costs the round the rest of the pull and nothing else,
        # which is the whole difference between a stop and a kill: nothing below this line
        # has been written anywhere yet.
        cancel.raise_if(run_id)
        try:
            rows = source.fetch_universe(chain["id"], limit)
            error = None
        except Exception as exc:
            rows, error = [], repr(exc)[:200]
        collected[source.name] = {row.ca.lower(): row for row in (rows or [])}
        per_source[source.name] = len(collected[source.name])
        reports.append(
            SourceReport(source=source.name, ok=error is None, records=len(collected[source.name]), error=error)
        )
        tell(index + 1, len(sources))

    if on_pulled:
        on_pulled()
    metrics = merge_all(collected)
    # 三个链接 are a pure function of (chain, CA) - the reference built them the same way -
    # so they are filled here, once, instead of in five adapters. A snapshot carries them
    # because a card must be readable without knowing how a URL is spelled.
    for ca, row in metrics.items():
        row.gmgn_url = token_url("gmgn", chain["id"], ca)
        row.debot_url = token_url("debot", chain["id"], ca)
        row.dexscreener_url = token_url("dexscreener", chain["id"], ca)
    # The index of what was in play, kept outside the database: it is the only way to
    # answer "was this token even seen" for a round that is months old.
    archive(
        run_id,
        "universe",
        {
            name: [
                {"ca": row.ca, "symbol": row.symbol, "market_cap_usd": row.market_cap_usd,
                 "liquidity_usd": row.liquidity_usd, "volume_24h_usd": row.volume_24h_usd}
                for row in sorted(collected[name].values(), key=lambda r: -(r.volume_24h_usd or 0))[:400]
            ]
            for name in collected
        },
    )
    return Universe(metrics=metrics, reports=reports, per_source=per_source, rows=collected)
