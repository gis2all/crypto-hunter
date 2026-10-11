"""DexScreener's own board: the chain as the site lists it, one page per order.

The scanners are launchpad-shaped. RadarDEX and Tolly enumerate Arc, the three Robinhood
feeds enumerate their own launches, GMGN ranks what is trading in one window, and
GeckoTerminal pages pools. That leaves the tokens a bot actually traded on-chain invisible
until a launchpad happens to list them, which is why the pool is tens of rows on a chain
with thousands of pairs. This reads the board the site renders, and it is the only list
source here that answers "what is being traded on this chain right now" without having been
told what to look for.

Two things had to be measured before it could be used, on 2026-10-11:

**The rows are in the document.** `dexscreener.com/<chain>?rankBy=...` answers 200 to the
browser lane with the whole board inside the HTML, as protobuf-JSON under `"pairs"`. The
endpoint the page itself calls is a WebSocket (`wss://io.dexscreener.com`, path
`/dex/screener/v8/pairs-search`), its HTTP side answered 403/404 on every path tried,
`api.dexscreener.com` has no list endpoint at all, and a headless browser is refused by
Cloudflare. So the document is the way in, and `browser` is the lane that gets it.

**One page is a hundred rows and `&page=` is ignored.** Pages 1, 2 and 3 come back with the
same hundred pairs. Seeing more of a chain therefore means asking for a different order and
unioning the answers: measured in this chain own band, six orders came back with 380
distinct pairs where one came back with 100.

The band is the round own, handed over by `collect.set_scan_band`: the site filters its
board server-side on market cap and liquidity, so the hundred rows a page holds are rows
this round could keep. Nothing here is a verdict - the rows join the universe and the same
prefilter, enrich and gate passes judge them, exactly as they judge a launchpad row.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.models import SafetyFacts, TokenMetrics, ca_key
from ..core.parse import as_float as _num, positive_float
from .base import social_url, web_url
from .http import SourceError, UA_HTML, cached, get

BASE = "https://dexscreener.com/%s"
TRANSPORT = "browser"

#: The orders the union is built from. Measured 2026-10-11 in a 10k-200k market-cap band
#: with a 5k liquidity floor: `trendingScoreH6` 100 rows, then +81 new from `volume`, +56
#: from `liquidity`, +69 from `marketCap`, +56 from `priceChangeH24`, +18 from `txns` - 380
#: distinct pairs out of six requests. `fdv` and `activeBoosts` added none in that band and
#: are left out; `pairAge` answers 100 rows on Robinhood and 0 on Solana, and a request that
#: costs two seconds to say nothing is not worth carrying on the chance it answers.
SORTS = ("trendingScoreH6", "volume", "liquidity", "marketCap", "priceChangeH24", "txns")
#: The board moves every few seconds; this only stops two stages in one round from paying
#: for the same six pages twice.
BOARD_TTL = 1800
#: The row carries an id for its mark, not a URL - the page builds the URL from it.
LOGO = "https://cdn.dexscreener.com/cms/images/%s"
#: The link types that are a feed rather than a page. 0 is every site-shaped link
#: (Website, Docs, Github, OpenSea, CMC, Instagram ...), and the labels beside it vary, so
#: the number is what this reads. Whatever is not in here goes to `websites`.
SOCIAL_TYPES: dict[int, str] = {1: "discord", 2: "twitter", 3: "telegram", 5: "tiktok", 7: "instagram"}


def _money(value: Any) -> float | None:
    """A dollar figure, or None. Zero is not a reading here: a market cap of nothing and a
    pool with no reserves both mean this source has no price, not that the price is nil."""
    return positive_float(value)


def _array(document: str, open_at: int) -> str | None:
    """The bracketed value that starts at `open_at`, by matching brackets.

    Strings are tracked because the payload is full of them: a symbol or a URL carrying a
    bracket would otherwise close the array early and the parse would end somewhere in the
    middle of the board - a silent truncation, which is the failure mode worth ten lines.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_at, len(document)):
        char = document[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return document[open_at:index + 1]
    return None


def pairs(html: str) -> list[dict]:
    """The board out of the rendered document, as rows.

    The payload is protobuf-JSON, not JSON: a big integer arrives as `BigInt("33")` so it
    cannot lose precision as a float, and an absent field as `void 0` or `undefined`. None
    of the three parses, so all three are rewritten first. A document with no board in it
    (a challenge page, a chain this site does not list) answers `[]`.
    """
    marker = html.find('"pairs":[')
    if marker < 0:
        return []
    blob = _array(html, html.index("[", marker))
    if blob is None:
        return []
    blob = re.sub(r'BigInt\("(-?\d+)"\)', r"\1", blob)
    blob = blob.replace("void 0", "null").replace("undefined", "null")
    try:
        rows = json.loads(blob)
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _links(profile: dict) -> tuple[list[str], dict[str, str]]:
    """(websites, socials) off the row CMS profile, where a project registers them.

    This is the same pair of lists `dexscreener.fetch_token` reads out of the JSON API info
    block, and it is the half of this source that answers the rounds ending in "no readable
    link": a row that arrives with a site and an X handle can be researched.
    """
    sites: list[str] = []
    socials: dict[str, str] = {}
    for entry in (profile or {}).get("links") or []:
        if not isinstance(entry, dict):
            continue
        kind = SOCIAL_TYPES.get(entry.get("type"))
        if kind is None:
            url = web_url(entry.get("url"))
            if url and url not in sites:
                sites.append(url)
            continue
        url = social_url(kind, entry.get("url"))
        if url and kind not in socials:
            socials[kind] = url
    return sites, socials


def _metrics(row: dict, chain_id: str) -> TokenMetrics | None:
    """One pair row as a token row, or None when the row is not this chain base token.

    The base side is the token: a pair market cap and price describe the base token, and
    writing them against a quote-side address would print one token numbers under another
    token name. A token that only ever appears as the quote side is therefore not picked up
    here - it is not picked up anywhere else on these chains either, and the alternative,
    reading both sides, invents the numbers rather than missing them.
    """
    if str(row.get("chainId") or "") != chain_id:
        return None
    token = row.get("baseToken") or {}
    ca = ca_key(token.get("address"))
    if not ca:
        return None
    profile = row.get("cmsProfile") or {}
    sites, socials = _links(profile)
    cap = _money(row.get("marketCap")) or _money(row.get("fdv"))
    volume = _num((row.get("volume") or {}).get("h24"))
    depth = _money((row.get("liquidity") or {}).get("usd"))
    icon = str(profile.get("iconId") or "").strip()
    return TokenMetrics(
        ca=ca,
        symbol=str(token.get("symbol") or "").strip() or None,
        market_cap_usd=round(cap, 2) if cap else None,
        liquidity_usd=round(depth, 2) if depth else None,
        volume_24h_usd=round(volume, 2) if volume is not None else None,
        price_usd=_money(row.get("priceUSD")),
        price_change_24h_pct=_num((row.get("priceChange") or {}).get("h24")),
        websites=sites,
        socials=socials,
        logo_url=LOGO % icon if icon else None,
    )


class DexScreenerRank:
    """The board, unioned over a few orders. One sweep per round, cached for half an hour."""

    name = "dexscreener_rank"
    chains = ("bsc", "robinhood", "solana")

    #: The band `collect.set_scan_band` injects once per round, from the same
    #: `pipeline.prefilter` the round is about to filter on. 0 means no line, and no line
    #: means read everything - an unusable value must never read nothing.
    market_cap_floor: float = 0.0
    market_cap_ceiling: float = 0.0
    liquidity_floor: float = 0.0

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        rows = self._board(chain_id)
        return rows[: max(0, limit)] if limit else rows

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        """Nothing per token: the sweep already read every pair this source knows about, and
        the band it was asked for is the band this round keeps. `sources/dexscreener.py`
        answers the per-token questions, from the API on the same host family."""
        return TokenMetrics(ca=ca), SafetyFacts()

    # --------------------------------------------------------------- the sweep

    def _query(self) -> str:
        parts = []
        for name, value in (("minMarketCap", self.market_cap_floor),
                            ("maxMarketCap", self.market_cap_ceiling),
                            ("minLiq", self.liquidity_floor)):
            if value:
                parts.append("%s=%d" % (name, int(value)))
        return "&".join(parts)

    def _board(self, chain_id: str) -> list[TokenMetrics]:
        query = self._query()
        return cached(
            "dexscreener_rank:%s:%s" % (chain_id, query),
            BOARD_TTL,
            lambda: self._sweep(chain_id, query),
        ) or []

    def _sweep(self, chain_id: str, query: str) -> list[TokenMetrics]:
        """Every order in turn, one token per CA: the deepest of its pairs.

        Serial rather than parallel: six requests arriving together is a burst this host has
        not been asked for before, and the sweep is a quarter of a minute either way. The
        deepest pair wins because that is the row this source is believed for - the per-token
        read then replaces the market numbers with the token own depth.
        """
        best: dict[str, tuple[float, TokenMetrics]] = {}
        answered = 0
        failures: list[str] = []
        for order in SORTS:
            tail = ("&" + query) if query else ""
            url = "%s?rankBy=%s&order=desc%s" % (BASE % chain_id, order, tail)
            try:
                html = get(url, UA_HTML, transport=TRANSPORT, tries=2, sleep=1.0, timeout=30.0)
            except SourceError as exc:
                failures.append(str(exc)[:60])
                continue
            answered += 1
            for row in pairs(html):
                metrics = _metrics(row, chain_id)
                if metrics is None:
                    continue
                depth = metrics.liquidity_usd or 0.0
                seen = best.get(metrics.ca)
                if seen is None or depth > seen[0]:
                    best[metrics.ca] = (depth, metrics)
        if not answered:
            # Not one page came back. That is a dead source, and the round has to record it
            # as one: a sweep that answers nothing reads exactly like a chain with no pairs.
            raise SourceError("dexscreener.com/%s: no board answered (%s)" % (chain_id, "; ".join(failures)[:160]))
        return [metrics for _, metrics in best.values()]
