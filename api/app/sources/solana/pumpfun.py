"""pump.fun: the other Solana launchpad, and the only source here that ships a token's own
high on this chain.

`frontend-api-v3.pump.fun/coins` answers the whole launchpad as JSON with an offset pager.
The older `frontend-api.pump.fun` host is gone (530) and the per-token endpoints 404, so
the list is the whole source. Measured: a page holds `70` rows even when `limit=100` is
asked for, and the server's own answer for the sorts it accepts is `created_timestamp` /
`market_cap` / `ath_market_cap` / `reply_count` / `last_reply` / `last_trade_timestamp`.

**Two market-cap columns, and only one of them is dollars.** `market_cap` is denominated in
SOL and `usd_market_cap` is the dollar figure: on 2026-10-11 the same rows read `market_cap`
29 / 50 / 24 against `usd_market_cap` 3,143 / 5,459 / 2,614, a ratio of 108 that is simply
the price of SOL. Reading the SOL column would put a token's cap two orders of magnitude
below where it trades - through the floor and out of the band.

**`ath_market_cap` is dollars too**, and that was measured rather than assumed, because it
is the token's own high and the 距高回撤 rule drops on it. Tokens created 0.1-0.4 minutes
before the probe carried `ath_market_cap` / `usd_market_cap` ratios of 1.0, 1.18, 1.37,
2.13 - ratios a token cannot show on its first minutes if the two columns are in different
units, where SOL denomination would have put that ratio near 108.

**There is no volume column.** Nothing here can answer the 24h floor or the volume factor, so
this source leaves both unread rather than borrowing a number from somewhere else; that is
what 未判定 is for, and it never rejects. Liquidity is the same: a token still on the bonding
curve has no pool, and the DexScreener read that fills everyone else's liquidity finds no
pair for it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ..base import social_url
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://frontend-api-v3.pump.fun"
#: Measured: the feed answers 70 rows a page whatever `limit` says.
PAGE_SIZE = 70
#: Ordered by last trade, the band keeps refilling (measured 2026-10-11: 22, 11, 13, 11 rows
#: in band over four pages), so the sweep is three pages rather than one.
MAX_PAGES = 3
SORT = "last_trade_timestamp"
UNIVERSE_TTL = 900

HTTP = re.compile(r"^https?://")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _when(value: Any) -> datetime | None:
    """`created_timestamp` is milliseconds. A value that will not read as one is a gap, and
    a gap must not become a timestamp of the epoch."""
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


def _image(value: Any) -> str | None:
    """`image_uri` is already an absolute URL - usually the feed's own pinned gateway,
    sometimes ipfs.io - so it is printed as it arrives, and anything else is no mark."""
    text = str(value or "").strip()
    return text if HTTP.match(text) else None


class PumpFun:
    name = "pumpfun"
    chains = ("solana",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every launch this source lists, keyed by mint."""

        def produce():
            merged: dict[str, dict] = {}
            for page in range(MAX_PAGES):
                url = "%s/coins?limit=%d&sort=%s&order=desc&offset=%d" % (
                    BASE, PAGE_SIZE, SORT, page * PAGE_SIZE,
                )
                try:
                    items = json_get(url, UA_JSON, tries=2, sleep=2.0, timeout=45.0)
                except SourceError:
                    break
                if not isinstance(items, list) or not items:
                    break
                for row in items:
                    ca = ca_key(row.get("mint"))
                    if ca and ca not in merged:
                        merged[ca] = row
                if len(items) < PAGE_SIZE:
                    break
            return merged or None

        return cached("pumpfun:coins:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = [self._metrics(ca, row) for ca, row in self.rows(chain_id).items()]
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.rows(chain_id).get(ca_key(ca))
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(ca_key(ca), row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        metrics = TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            description=str(row.get("description") or "").strip() or None,
            # The dollar column, never `market_cap` - see the module docstring.
            market_cap_usd=_num(row.get("usd_market_cap")),
            high_market_cap_usd=_num(row.get("ath_market_cap")),
            launchpad="pump.fun",
            created_at=_when(row.get("created_timestamp")),
            logo_url=_image(row.get("image_uri")),
            socials=_socials(row),
        )
        metrics.creator = row.get("creator") or None
        metrics.provenance = {
            "market_cap_usd": "pumpfun", "high_market_cap_usd": "pumpfun",
            "created_at": "pumpfun", "launchpad": "pumpfun", "creator": "pumpfun",
            "logo_url": "pumpfun", "description": "pumpfun", "socials": "pumpfun",
        }
        return metrics


def _socials(row: dict) -> dict[str, str]:
    url = social_url("twitter", row.get("twitter"))
    return {"twitter": url} if url else {}
