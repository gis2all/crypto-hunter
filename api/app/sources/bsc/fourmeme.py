"""four.meme: this chain launchpad, and the only feed that sees BSC tokens early.

`four.meme/meme-api/v1/public/token/search` is the site own endpoint - POST, JSON body, and
the web client own `meme-api-key` header, which the shipped bundle carries in clear
(`axios.create({baseURL: "/meme-api", headers: {"meme-api-key": ...}})`). Measured
2026-10-11: every payload below answers 200 with no cookie and no signature, and on the
plain client - no browser lane.

**The sweep is the graduated set, over two orders.** `status: "TRADE"` with
`listType: "NOR_DEX"` is the payload the site own PancakeSwap tab builds, and it is the set
that matters: those tokens have a pool, so their liquidity and volume can be read by the
sources that own those numbers. Curve rows (`status: "PUBLISH"`, `listType: "NOR"`) carry
caps of 4 to 593 USD and no pool at all (measured), and a token with no pool cannot be
ranked beside one that has - the same reason PONS on Robinhood sweeps `graduated` only.
`minCap`/`maxCap` are not read by this endpoint - measured, the same 50 rows come back with
a band and without one - so there is no server-side cut to hand it and none of the
`market_cap_floor` knobs are declared here.

The two orders answer different halves and both are swept: `NEW` (newest graduation first)
is where the just-launched tokens are - four pages carry 103 CAs, 193 of 202 measured over
six pages sit at or under 200K with a median of 3,138 - while `CAP` is the only place the
upper band appears (page 1 starts at 892,076 and page 2 covers 274,547 to 872,237, of which
65 rows land in 200K-2M). Its third page carries no rows at all, which is what ends it. The
newest order does not run dry - its sixth page still answered with 49 new CAs - so it is
the one the page ceiling exists for. `VOL` is deliberately not swept: its rows are the ones
the DexScreener board already ranks, and every extra order is another hundred tokens the
enrich pass pays for.

**`pageIndex` is not trusted, which is why the two orders are unioned and deduped.** Page 3
of the `NEW` order repeated page 1 row for row (same first CA, same `createDate`), and five
pages yielded 153 unique CAs against 250 rows. The sweep asks for a bounded number of pages
and keeps whatever is new.

**What is read from a row, and what is deliberately not.** A row carries `cap`, `volume`
and a ladder of window volumes, and none of them is read:

- `cap` is not DexScreener market cap. Measured the same day, AKE carried 2,703,401,221
  here against 616,653,496 there (x4.4) while the row beside it agreed to five digits
  (560,303,067 against 560,082,849). Two calibers, one field name - so `market_cap_usd` is
  not written from here, and this source is absent from that field list in
  `pipeline/collect.py` rather than listed low.
- `day1Vol` is usually the day trade and sometimes is not (AKE 595,445.97 against a real
  621,626.82; `$BANANA` 360.33 against 1,652,020.09; Tag 55,976,851.46 against 208,838.20),
  and `volume` disagrees with it on the same row. `volume_24h_usd` is not read here either.
- `hold` was 0 on all 20 rows measured, so `holders` is not read.

What is left is what only this feed has: the ticker, the deployer, the creation instant, the
artwork, and the fact that the token came from four.meme. `symbol` in the row is the
currency the curve prices in (measured: "BNB" on every row); the ticker is `shortName`.

**The artwork is a path on the site own asset host.** `img` arrives as `/market/<id>.png`,
and the host is published by the site itself at `/v1/public/file/host`
(`https://static.four.meme`, measured 200 `image/png`). A mark that will not read as one of
those is no mark, never a row lost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ..http import SourceError, UA_JSON, cached, json_post

BASE = "https://four.meme/meme-api"
SEARCH = BASE + "/v1/public/token/search"

#: The web client own key, read out of the bundle the site ships. It is not a credential -
#: it is in the page every visitor loads - and the endpoint answers without a cookie.
API_KEY = "0b42934b-46f5-4583-b510-682137226b30"

HEADERS = {
    **UA_JSON,
    "Origin": "https://four.meme",
    "Referer": "https://four.meme/",
    "meme-api-key": API_KEY,
}

#: The asset host the site publishes at `/v1/public/file/host`, measured 2026-10-11.
ARTWORK_HOST = "https://static.four.meme"

#: Rows a page holds. Measured: 50 is accepted and answered.
PAGE_SIZE = 50

#: The two orders. A page that comes back short ends that order, which is what stops the
#: cap order on its own - measured, its third page carries no rows. The newest order does
#: not run dry (its sixth page still answered with 49 new CAs), so `MAX_PAGES` is the
#: ceiling for the one that does not.
ORDERS: tuple[str, ...] = ("CAP", "NEW")
MAX_PAGES = 4

#: The payload the site own PancakeSwap tab builds: listed, and trading there.
GRADUATED = {"status": "TRADE", "listType": "NOR_DEX"}

UNIVERSE_TTL = 900
TOKEN_TTL = 900


def _ticker(value: Any) -> str | None:
    """The row ticker, which is `shortName` - see the module docstring for `symbol`."""
    text = str(value or "").strip()
    return text or None


def _when(value: Any) -> datetime | None:
    """`createDate` is unix milliseconds. A value that will not read as one is a gap, and a
    gap must not become a timestamp of the epoch."""
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _artwork(value: Any) -> str | None:
    """`img` as the URL the page itself builds, or no mark at all."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("https://"):
        return text
    return ARTWORK_HOST + text if text.startswith("/") else None


class FourMeme:
    name = "fourmeme"
    chains = ("bsc",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every graduated launch this source knows, keyed by CA."""

        def produce():
            merged: dict[str, dict] = {}
            for order in ORDERS:
                for page in range(1, MAX_PAGES + 1):
                    body = {"pageIndex": page, "pageSize": PAGE_SIZE, "type": order,
                            "sort": "DESC", **GRADUATED}
                    try:
                        payload = json_post(SEARCH, body, HEADERS, tries=2, sleep=2.0, timeout=45.0)
                    except SourceError:
                        break
                    items = payload.get("data") if isinstance(payload, dict) else None
                    if not items:
                        break
                    for row in items:
                        ca = ca_key(row.get("tokenAddress"))
                        if ca and ca not in merged:
                            merged[ca] = row
                    if len(items) < PAGE_SIZE:
                        break
            return merged or None

        return cached("fourmeme:search:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = [self._metrics(ca, row) for ca, row in self.rows(chain_id).items()]
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        key = ca_key(ca)

        def produce():
            # The address goes in lower case: measured, `0X...` answers with nothing while
            # the same address behind a lower-case `0x` answers with the row. `ca_key` has
            # already folded the EVM address, so the key is the spelling to send.
            body = {"pageIndex": 1, "pageSize": 5, "keyword": key}
            try:
                payload = json_post(SEARCH, body, HEADERS, tries=2, sleep=2.0, timeout=30.0)
            except SourceError:
                return None
            items = payload.get("data") if isinstance(payload, dict) else None
            for row in items or []:
                if ca_key(row.get("tokenAddress")) == key:
                    return row
            return None

        row = cached("fourmeme:token:%s" % key, TOKEN_TTL, produce)
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(key, row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        metrics = TokenMetrics(
            ca=ca,
            symbol=_ticker(row.get("shortName")),
            logo_url=_artwork(row.get("img")),
            launchpad="fourmeme",
            created_at=_when(row.get("createDate")),
        )
        #: Folded like every other EVM address this app holds: the row spells it lower
        #: already, and a creator that arrives in another case is the same address.
        metrics.creator = str(row.get("userAddress") or "").strip().lower() or None
        metrics.provenance = {
            "symbol": "fourmeme", "logo_url": "fourmeme", "launchpad": "fourmeme",
            "created_at": "fourmeme", "creator": "fourmeme",
        }
        return metrics
