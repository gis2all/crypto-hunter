"""Tolly: the second full-chain registry on Arc, and the one that carries the long tail.

RadarDEX indexes what ranks. Tolly answers a different question: `scope=all` walks every
token it has ever seen, which is how a pair that is ten minutes old, has no volume and a
market cap under a thousand still gets a row. Measured against RadarDEX the overlap is 36
CAs out of roughly 7,300, so reading both is coverage, not redundancy.

Three properties of the endpoint shape the pull:

- `limit` is clamped server-side to 120 whatever you ask for, so a full read is about 41
  requests. They are cheap and the host does not rate-limit the way the trackers do.
- There is no range filter. `minMarketCap`, `min_market_cap` and `marketCapMin/Max` are all
  ignored: the answer comes back byte for byte identical and `total` does not move. There
  is `sort=mcap&dir=desc`, and that is what makes an early stop possible, because the chain
  is a spike rather than a curve. Page one runs from $78T down to $19.6K and page two tops
  out at $19,999, so every row above a $10K floor sits in the first three pages, while the
  remaining four thousand rows are launchpad placeholders at the $8.9K seed price.
- Paging is `offset` over a live ordering. Re-reading the same offset returns the same
  page, but the ordering shifts while the pull runs, so about 11% of the slots repeat a
  token already seen on an earlier page. The merge keys on CA, which makes that harmless.

So a pull is two reads: the default first page, which catches a token that is too new to
have been ranked yet, and the market-cap walk, which is told where to stop by the same
floor the round will filter on. The stop is a shortcut, never a filter: rows below the
floor that arrive anyway are kept, and the prefilter remains the only thing that drops a
token. A missing floor means the walk reads the whole registry the way it used to, so the
failure direction is extra requests, not lost coverage.

Tolly publishes no holder count, so RadarDEX stays the authority for that field; what it
adds is the bottom of the curve that a ranking never reaches.
"""

from __future__ import annotations

from ...core.filters import is_empty_shell
from ...core.models import SafetyFacts, TokenMetrics
from ...core.parse import positive_float as _num, utc_from_unix as _iso
from ..base import socials_from, website_list
from ..http import SourceError, UA_JSON, cached, json_get, pmap

BASE = "https://api.tollylabs.com"
#: Server-side clamp. Asking for more does not get more.
PAGE = 120
#: 60 pages is 7,200 slots against a chain of about 5,000: headroom for the repeat rate.
MAX_PAGES = 60
#: The ordering the early stop needs. Measured by hand: with `dir=desc`, page one holds every
#: row at or above $19.6K and page two tops out at $19,999.
SORT = "mcap"
#: A page costs the host about four seconds to assemble, and the pages are independent,
#: so they are asked for together. Eight in flight turns a three-minute walk into thirty
#: seconds; the per-host pacer still serialises them to one every 0.2s. The floor walk is
#: sequential on purpose - it exists to stop, and stopping needs the page before it.
WORKERS = 8
UNIVERSE_TTL = 900


def _all_below(rows: list[dict], floor: float) -> bool:
    """True when a page holds nothing at or above `floor`.

    A row with no market cap counts as below. Those rows sort to the end of a descending
    walk, so a page of them means the priced part of the chain is behind us. Measured at
    one token with real liquidity in that group: that is the price of not reading all 41
    pages, and it is the only one.
    """
    for row in rows:
        try:
            if float(row.get("marketCap")) >= floor:
                return False
        except (TypeError, ValueError):
            continue
    return True


class Tolly:
    name = "tolly"
    chains = ("arc",)

    #: The market-cap floor the sorted walk stops at, in USD. Injected once per round by
    #: `collect.set_scan_floor`, from the same `prefilter.market_cap_min_usd` the round is
    #: about to filter on. 0 means no floor is known, and the walk then reads everything.
    market_cap_floor: float = 0.0

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every token that could still matter, keyed by CA, first sighting wins.

        Two reads merged: the default first page, which is the newest rows, and the
        market-cap walk, which stops once a page holds nothing at or above the floor.
        """

        def produce():
            merged: dict[str, dict] = {}
            for payload in (self._page(0), self._walk()):
                for row in (payload or {}).get("tokens") or []:
                    ca = str(row.get("address") or "").lower()
                    if ca and ca not in merged:
                        merged[ca] = row
            return merged or None

        return cached("tl:tokens:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def _walk(self) -> dict | None:
        """Market cap, descending, until the floor is under our feet.

        The floor is a stopping rule, not a filter: whatever does arrive is returned, and
        the prefilter still decides. It only ever saves requests.
        """

        head = self._page(0, sort=SORT)
        if not head:
            return None
        rows = list(head.get("tokens") or [])
        # `total` arrives with page one, so the walk knows its own length up front: no
        # probing for an empty page, and no guessing how far to fan out.
        total = int(head.get("total") or 0)
        pages = max(1, min(MAX_PAGES, -(-total // PAGE)))
        if self.market_cap_floor > 0:
            for page in range(1, pages):
                payload = self._page(page * PAGE, sort=SORT)
                if not payload:
                    break
                got = list(payload.get("tokens") or [])
                rows.extend(got)
                if _all_below(got, self.market_cap_floor):
                    break
            return {"tokens": rows}
        tail = pmap(lambda offset: self._page(offset, sort=SORT),
                    [page * PAGE for page in range(1, pages)], workers=WORKERS)
        for payload in tail:
            rows.extend((payload or {}).get("tokens") or [])
        return {"tokens": rows}
    def _page(self, offset: int, sort: str | None = None) -> dict | None:
        """One page of the registry, or None if the host refused it three times."""
        query = "scope=all&limit=%d&offset=%d" % (PAGE, offset)
        if sort:
            query += "&sort=%s&dir=desc" % sort
        try:
            return json_get("%s/tokens?%s" % (BASE, query), UA_JSON, tries=3, sleep=2.5, timeout=45.0)
        except SourceError:
            return None

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """Every row the registry lists, minus the ones carrying nothing to measure.

        A row with neither a market cap nor a reserve is a launchpad placeholder with an
        address on it, and the free half drops that exact shape a moment later as 空壳淘汰.
        Handing them over spends funnel rows on tokens no rule can judge, and credits the
        count to the rule rather than to the source that shipped them.
        `filters.is_empty_shell` is the predicate on both sides.
        """
        out: list[TokenMetrics] = []
        for ca, row in self.rows(chain_id).items():
            metrics = self._metrics(ca, row)
            if is_empty_shell(metrics):
                continue
            out.append(metrics)
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.rows(chain_id).get(ca.lower()) or {}
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(ca, row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        return TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            market_cap_usd=_num(row.get("marketCap")),
            liquidity_usd=_num(row.get("liquidity")),
            volume_24h_usd=_num(row.get("volume24h"), zero_ok=True),
            price_usd=_num(row.get("price")),
            price_change_24h_pct=_num(row.get("change24h")),
            creator=row.get("creator") or None,
            main_pool=row.get("pool") or None,
            launchpad="tolly" if row.get("tolly") else None,
            created_at=_iso(row.get("created_ts")),
            socials=_socials(row),
            websites=_websites(row),
            provenance={
                "market_cap_usd": "tolly", "liquidity_usd": "tolly",
                "volume_24h_usd": "tolly", "price_usd": "tolly",
                "creator": "tolly", "main_pool": "tolly", "created_at": "tolly",
            },
        )


def _socials(row: dict) -> dict[str, str]:
    return socials_from(row, (("twitter", "twitter"), ("telegram", "telegram")))


def _websites(row: dict) -> list[str]:
    return website_list(row.get("website"))


