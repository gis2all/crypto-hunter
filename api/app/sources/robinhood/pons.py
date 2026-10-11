"""PONS: this chain's largest launchpad, and the fourth Robinhood feed.

`ponsfamily.com/api/launches` is the launchpad's own endpoint - same origin as the
`/launchpad` page it renders, no key. `api.ponsfamily.com` is a different, keyed
service (401) and is not this. Rows come 40 to a page and the cursor is opaque
(`nextCursor`; a guessed `cursor=0` answers 503), and the list is `stage`-filtered.

**The sweep is `graduated` only.** Measured 2026-10-11: `stage=all&sort=active`
returns 319 curve rows to 1 graduated, and 34 of those 320 curve rows land in this chain's
market-cap band carrying no liquidity at all - which the liquidity floor reads as 未判定 and
waves straight through. A token whose pool does not exist yet cannot be ranked beside one
whose does, so only the graduated set is swept. `sort=marketCap` adds nothing to the band
(its first page starts at $922K, an order of magnitude above the ceiling), so the union is
`active` + `newest`.

**`volumeUsd` really is the day's trade, not the launch's total.** That was measured in
the site's own bundle rather than assumed: it prints "24h vol" beside this field on a
graduated row and "vol" on a curve row. Cross-read against DexScreener the same day, three
graduated rows carried `0` here where their pools had traded 61,856 / 62,759 / 21,138 in
24h - the column is the *curve's* own volume, and a migrated token stops updating it. A
number with two meanings must not be read by one floor, which is the second reason the sweep
is graduated-only.

**The artwork is resolved through the site's own gateway.** `artwork` arrives as
`ipfs://<cid>` and the page builds `.../api/ipfs/content/<cid>?variant=card` on the host
its shipped bundle names (measured 200, `image/webp`). The public gateways refuse this
client - ipfs.io answers 403, dweb.link 429 - so the measured host is what the card gets. A
gateway that dies costs a mark, never a row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ..base import social_url, web_url
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://ponsfamily.com/api"
#: The page is the site's own, and this feed reads its own referer.
HEADERS = {**UA_JSON, "Referer": "https://ponsfamily.com/launchpad"}
#: Measured: 40 rows a page.
PAGE_SIZE = 40
#: Band rows keep surfacing deep in both orders (measured on `active`: 24 in page 1, then
#: 11, 7, 7, 4, 6 ...), so one page is not a sweep. Eight pages an order is sixteen requests,
#: every one of which answered in the probe round.
MAX_PAGES = 8
SORTS = ("active", "newest")
UNIVERSE_TTL = 900

#: The site's own artwork route, host included - see the module docstring for why it is this
#: host and not a public gateway.
ARTWORK = "https://dbk-vercel.vercel.app/api/ipfs/content/%s?variant=card"
IPFS_URI = re.compile(r"^ipfs://([a-zA-Z0-9]+)")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _when(value: Any) -> datetime | None:
    """`createdAt` is unix seconds. A value that will not read as one is a gap, and a gap
    must not become a timestamp of the epoch."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _artwork(value: Any) -> str | None:
    """`ipfs://<cid>` as the URL the page itself builds. A value that is already https is
    left alone; anything else is no mark at all."""
    text = str(value or "").strip()
    if not text:
        return None
    match = IPFS_URI.match(text)
    if match:
        return ARTWORK % match.group(1)
    return text if text.startswith("https://") else None


class Pons:
    name = "pons"
    chains = ("robinhood",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every graduated launch this source knows, keyed by CA."""

        def produce():
            merged: dict[str, dict] = {}
            for sort in SORTS:
                cursor = None
                for _ in range(MAX_PAGES):
                    url = "%s/launches?sort=%s&stage=graduated" % (BASE, sort)
                    if cursor:
                        url += "&cursor=%s" % cursor
                    try:
                        payload = json_get(url, HEADERS, tries=2, sleep=2.0, timeout=45.0)
                    except SourceError:
                        break
                    items = payload.get("items") if isinstance(payload, dict) else None
                    if not items:
                        break
                    for row in items:
                        ca = ca_key(row.get("address"))
                        if ca and ca not in merged:
                            merged[ca] = row
                    cursor = payload.get("nextCursor")
                    if not cursor or len(items) < PAGE_SIZE:
                        break
            return merged or None

        return cached("pons:launches:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = [self._metrics(ca, row) for ca, row in self.rows(chain_id).items()]
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.rows(chain_id).get(ca_key(ca))
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(ca_key(ca), row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        # The day's trade exists only on a graduated row; on a curve row the same column is
        # the curve's own total (module docstring). Read by name, never merged blind.
        volume = _num(row.get("volumeUsd")) if row.get("stage") == "graduated" else None
        metrics = TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            description=str(row.get("description") or "").strip() or None,
            market_cap_usd=_num(row.get("marketCapUsd")),
            volume_24h_usd=volume,
            price_usd=_num(row.get("priceUsd")),
            launchpad="pons",
            created_at=_when(row.get("createdAt")),
            logo_url=_artwork(row.get("artwork")),
            socials=_socials(row),
            websites=_websites(row),
        )
        metrics.creator = row.get("deployer") or None
        metrics.provenance = {
            "market_cap_usd": "pons", "volume_24h_usd": "pons", "price_usd": "pons",
            "created_at": "pons", "launchpad": "pons", "creator": "pons",
            "logo_url": "pons", "description": "pons", "socials": "pons", "websites": "pons",
        }
        return metrics


def _socials(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    source = row.get("socials") or {}
    for kind in ("twitter", "telegram", "discord"):
        url = social_url(kind, source.get(kind))
        if url:
            out[kind] = url
    return out


def _websites(row: dict) -> list[str]:
    """The site is kept beside the handles, so it cannot be read by the per-token loop that
    reads `websites` off the metrics."""
    url = web_url((row.get("socials") or {}).get("website"))
    return [url] if url else []
