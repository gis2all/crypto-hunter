"""PEZ: one of the two Robinhood launchpads, and the only source that ships social links.

`api.pez.family/api/launches` answers the launchpad's whole history, including the token too
new to have been ranked anywhere (316 tokens; `pageSize` is clamped to 100). The USD columns
are native - `marketCapUsd`, `fdvUsd`, `realMcapUsd` - but the volume column is *not*:
`volume` is denominated in the pool's quote asset, and on this chain the quote asset is not
necessarily a stablecoin (one row's quote asset is `SPCX`, a tokenised SpaceX share). So the
USD rate is derived from the row itself as `priceUsd / priceQuote` and the volume is
converted with it, rather than printed as if the number were already dollars.

`priceChangePct` is a fraction, not a percent (measured: `{h1: 0, h24: 0.000347, d7: 0.00651}`
on a token whose FDV had moved a fraction of a percent), so it is multiplied by 100.

`liquidityUsd` is null on every curve-phase row and filled once a token graduates. That is
the source's own answer, not a gap to patch, and DexScreener leads the field anyway.
"""

from __future__ import annotations

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ...core.parse import positive_float as _num, utc_from_iso_text as _iso
from ..base import SOCIAL_COLUMNS, socials_from, website_list
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://api.pez.family/api"
#: `pageSize` is clamped to 100 whatever you ask for (measured: `pageSize=200` answered 100).
PAGE_SIZE = 100
MAX_PAGES = 8
UNIVERSE_TTL = 900


class Pez:
    name = "pez"
    chains = ("robinhood",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every launch this source knows, keyed by CA."""

        def produce():
            merged: dict[str, dict] = {}
            for page in range(1, MAX_PAGES + 1):
                url = "%s/launches?origin=pez&version=all&page=%d&pageSize=%d" % (
                    BASE, page, PAGE_SIZE,
                )
                try:
                    payload = json_get(url, UA_JSON, tries=2, sleep=2.0, timeout=45.0)
                except SourceError:
                    break
                items = payload.get("items") if isinstance(payload, dict) else payload
                if not items:
                    break
                for row in items:
                    ca = ca_key(row.get("token"))
                    if ca and ca not in merged:
                        merged[ca] = row
                if len(items) < PAGE_SIZE:
                    break
            return merged or None

        return cached("pez:launches:%s" % chain_id, UNIVERSE_TTL, produce) or {}

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
            market_cap_usd=_num(row.get("marketCapUsd")) or _num(row.get("fdvUsd")),
            liquidity_usd=_num(row.get("liquidityUsd")),
            volume_24h_usd=_volume_usd(row),
            holders=int(row["holders"]) if row.get("holders") else None,
            price_usd=_num(row.get("priceUsd")),
            price_change_24h_pct=_change_pct(row),
            launchpad=row.get("origin") or "pez",
            created_at=_iso(row.get("launchedAt")),
            socials=_socials(row),
            websites=_websites(row),
        )
        metrics.creator = row.get("deployer") or None
        metrics.provenance = {
            "market_cap_usd": "pez", "liquidity_usd": "pez", "volume_24h_usd": "pez",
            "holders": "pez", "price_usd": "pez", "price_change_24h_pct": "pez",
            "created_at": "pez", "launchpad": "pez", "creator": "pez", "socials": "pez", "websites": "pez",
        }
        return metrics


def _volume_usd(row: dict) -> float | None:
    """`volume` is denominated in the quote asset, so the row's own price ratio converts it.
    A row whose ratio cannot be read gives no volume rather than a number in the wrong unit."""
    volume = _num((row.get("volume") or {}).get("h24"))
    price_usd, price_quote = _num(row.get("priceUsd")), _num(row.get("priceQuote"))
    if volume is None or not price_quote:
        return None
    return volume * (price_usd / price_quote) if price_usd else None


def _change_pct(row: dict) -> float | None:
    """The fraction the feed publishes, as a percent. A real fall is negative, so this is
    read without the positive-only helper."""
    try:
        return float((row.get("priceChangePct") or {}).get("h24")) * 100
    except (TypeError, ValueError):
        return None


def _socials(row: dict) -> dict[str, str]:
    return socials_from(row.get("socials") or {}, SOCIAL_COLUMNS)


def _websites(row: dict) -> list[str]:
    return website_list((row.get("socials") or {}).get("website"))
