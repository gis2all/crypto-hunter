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
from ..core.models import TokenMetrics, ca_key
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
#:
#: `dexscreener_rank` is the same host's bulk half - the board that page renders, unioned
#: over a few orders - so it sits immediately behind `dexscreener` on every market number:
#: same caliber, same units, one reading per pair. Like `dexscreener` it sits *behind* the
#: launchpad feeds for `symbol`, so a launch remembered by the place that launched it keeps
#: its own spelling. On the two chains that list it, it is not a fallback for a thin sweep;
#: it is usually the source that produced the row in the first place.
FIELD_PRIORITY: dict[str, tuple[str, ...]] = {
    # The name the card is titled with. DexScreener sits last rather than with the other
    # feed-wide sources at the top of this table: it answers for every token on the chain
    # and so fills the gap a launchpad nobody scrapes leaves (TIDE on Robinhood is an
    # ordinary Uniswap pool, not a launchpad launch), while the launchpad the token did
    # come from keeps its own spelling when it has one.
    "symbol": ("gmgn", "radardex", "tolly", "geckoterminal", "blockscout",
               "coinbarrel", "pez", "peeps", "dexscreener", "dexscreener_rank"),
    "gmgn_url": ("gmgn", "geckoterminal", "radardex"),
    "debot_url": ("gmgn", "geckoterminal", "radardex"),
    "market_cap_usd": ("dexscreener", "dexscreener_rank", "gmgn", "geckoterminal",
                       "radardex", "tolly", "blockscout", "coinbarrel", "pez", "peeps"),
    # The token own high. Three sources report one and they are the same kind of reading - a
    # record of the token own high-water mark: GMGN ships `history_highest_market_cap`,
    # CoinBarrel `peak_fdv_usd`, Peeps `athMarketCap`. None of them is interchangeable with
    # the pool history below it: GeckoTerminal answers the same question in a price, and no
    # merge can convert one into the other. Listed explicitly so a future source cannot
    # quietly outrank the ones that measured it.
    "high_market_cap_usd": ("gmgn", "coinbarrel", "peeps"),
    "liquidity_usd": ("dexscreener", "dexscreener_rank", "gmgn", "geckoterminal",
                      "radardex", "tolly", "blockscout", "coinbarrel", "pez", "peeps"),
    "volume_24h_usd": ("dexscreener", "dexscreener_rank", "gmgn", "geckoterminal",
                       "radardex", "tolly", "blockscout", "coinbarrel", "pez", "peeps"),
    "price_usd": ("dexscreener", "dexscreener_rank", "gmgn", "radardex", "tolly",
                  "geckoterminal", "blockscout", "coinbarrel", "pez", "peeps"),
    # GMGN answers this with the *ranking window's* change, not the 24-hour one:
    # `price_change_percent` read -0.22% on the 5m board and +0.35% on the 24h board for
    # the same token (measured 2026-09-29). The adapter gates it on the window, and the
    # sources that ship a real `h24` - `dexscreener_rank`'s board row, then the launchpad
    # feeds - sit behind it. Listed explicitly so the default order cannot let a
    # wrong-window reading win by accident.
    "price_change_24h_pct": ("gmgn", "dexscreener_rank", "radardex", "tolly", "geckoterminal",
                            "blockscout", "coinbarrel", "pez", "peeps"),
    "holders": ("gmgn", "radardex", "blockscout", "goplus", "coinbarrel", "pez", "peeps"),
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
    "total_supply": ("gmgn", "goplus", "coinbarrel"),
    "circulating_supply": ("gmgn", "goplus"),
    "max_supply": ("gmgn", "goplus"),
    "launchpad": ("gmgn", "radardex", "tolly", "blockscout", "pez", "peeps"),
    "creator": ("gmgn", "radardex", "tolly", "coinbarrel", "pez", "peeps"),
    "renames": ("gmgn",),
    # The card's mark. DexScreener owns it - it is where a project registers one - and the
    # pool feed stands behind it for the tokens that host has never been given a logo for
    # (CASINO on Robinhood, 2026-10-02: not one of its eleven pairs carried an info.imageUrl).
    # GMGN publishes one too, but its CDN refuses the request a page makes - measured in a
    # browser: that image loads from nowhere but gmgn.ai, so it is not a field we can print.
    "logo_url": ("dexscreener", "dexscreener_rank", "geckoterminal"),
    "dex_pair": ("geckoterminal",),
    # Only the pool history answers these two, and it is the only reading that can fail to -
    # see `filters.check_drawdown`.
    "drawdown_pct": ("geckoterminal",),
    "drawdown_unread": ("geckoterminal",),
    "main_pool": ("geckoterminal", "tolly"),
    "pools": ("geckoterminal", "radardex"),
    "price_series": ("geckoterminal",),
    "created_at": ("radardex", "tolly", "geckoterminal", "coinbarrel", "pez", "peeps"),
    # The project’s own blurb. Only the per-token link read has one (see
    # `sources/dexscreener.py`), so an empty 项目自述 line means no source carried it -
    # which is worth printing rather than leaving out.
    "description": ("dexscreener",),
}
#: The order the two stages fall back on when a field has no entry of its own above.
#: The chain-specific sources sit last: they are absent from a chain whose config does
#: not list them, so the tail is a fallback on the one chain that has them and never an
#: override on the others. `sources/arc/` and `sources/robinhood/` are where they live.
DEFAULT_PRIORITY = ("gmgn", "radardex", "tolly", "geckoterminal", "blockscout", "goplus", "sourcify",
                    "dexscreener", "dexscreener_rank", "coinbarrel", "pez", "peeps")

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


#: The trackers that spell a chain differently from this app. Only GMGN does so far: its
#: namespace for Solana is `sol`, while the chain id is `solana` - the spelling DexScreener
#: and GeckoTerminal answer to, so one id cannot be both. A wrong segment here does not 404,
#: it opens a live page about nothing, which is worse; so it gets a table rather than a
#: guess. The measurement behind it, and the request half of the same fact, are in
#: `sources/gmgn.py` (`CHAIN_SLUGS`). A pair that is absent means the chain id is already
#: the name that tracker uses.
TRACKER_CHAINS: dict[tuple[str, str], str] = {("gmgn", "solana"): "sol"}


def token_url(tracker: str, chain_id: str, ca: str) -> str:
    return TOKEN_URLS[tracker] % (TRACKER_CHAINS.get((tracker, chain_id), chain_id), ca)


def stamp_trackers(row: TokenMetrics, chain_id: str, ca: str) -> None:
    """The three tracker pages, stamped onto one row.

    A pure function of (chain, CA), so it is written once and called from both places that
    need it: the collect pass stamps every row a round keeps, and a CA search stamps the one
    row it read. Until it did, a searched card's footer had no GMGN / Debot / DexScreener to
    print - these fields were only ever filled here, and a search never runs a collect.
    """
    row.gmgn_url = token_url("gmgn", chain_id, ca)
    row.debot_url = token_url("debot", chain_id, ca)
    row.dexscreener_url = token_url("dexscreener", chain_id, ca)


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


def set_scan_band(sources: list, prefilter: dict) -> None:
    """Tell a source that filters its own board where this round's box is.

    `set_scan_floor` hands over the one line a scanner stops below. A source that filters
    server-side - `dexscreener_rank` reads the site's own board, which takes market cap and
    liquidity as query parameters - can be told the whole box instead: the same three
    numbers this round's prefilter is about to enforce, so the hundred rows a page holds are
    rows this round could keep. Zero means no line, and a value that will not read as a
    number becomes zero: an unusable knob reads everything, never nothing. A source without
    the knob ignores this.
    """
    band: dict[str, float] = {}
    for attribute, key in (("market_cap_floor", "market_cap_min_usd"),
                           ("market_cap_ceiling", "market_cap_max_usd"),
                           ("liquidity_floor", "liquidity_min_usd")):
        try:
            band[attribute] = max(0.0, float(prefilter.get(key) or 0))
        except (TypeError, ValueError):
            band[attribute] = 0.0
    for source in sources:
        for attribute, value in band.items():
            if hasattr(source, attribute):
                setattr(source, attribute, value)


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
        collected[source.name] = {ca_key(row.ca): row for row in (rows or [])}
        per_source[source.name] = len(collected[source.name])
        reports.append(
            SourceReport(source=source.name, ok=error is None, records=len(collected[source.name]), error=error)
        )
        tell(index + 1, len(sources))

    if on_pulled:
        on_pulled()
    metrics = merge_all(collected)
    # 三个链接 are a pure function of (chain, CA) - the reference built them the same way - so
    # they are stamped from one function instead of in five adapters, and a CA search calls
    # the same one. A snapshot carries them because a card must be readable without knowing
    # how a URL is spelled.
    for ca, row in metrics.items():
        stamp_trackers(row, chain["id"], ca)
    # The index of what was in play, kept outside the database: it is the only way to
    # answer "was this token even seen" for a round that is months old.
    archive(
        run_id,
        "universe",
        {
            name: [
                {"ca": ca_key(row.ca), "symbol": row.symbol, "market_cap_usd": row.market_cap_usd,
                 "liquidity_usd": row.liquidity_usd, "volume_24h_usd": row.volume_24h_usd}
                for row in sorted(collected[name].values(), key=lambda r: -(r.volume_24h_usd or 0))[:400]
            ]
            for name in collected
        },
    )
    return Universe(metrics=metrics, reports=reports, per_source=per_source, rows=collected)
