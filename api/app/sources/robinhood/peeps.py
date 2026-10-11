"""Peeps: the other Robinhood launchpad, and the only source that ships a measured high.

`peeps.wtf/api/launchpad/tokens?tab=trending` answers the launchpad's whole list (84 tokens in
one page at `pageSize=100`, which is not clamped). The USD columns are native - `marketCap`,
`liquidityUsd`, `volumeUsd24h` - and `athMarketCap` is the token's own all-time-high market
cap. That last one is what 距高回撤 reads, and it is the same kind of measurement as GMGN's
`history_highest_market_cap`: a record of the token's high, not a pool's price history.

`change` is a signed percent rounded to a whole number (-2 reads as -2%), so it is coarse
rather than wrong; DexScreener leads the field and this sits behind it.

`tab=new` answers an empty list while `tab=trending` answers all 84, so the walk uses
`trending` for both - it is the tab that has the tokens, not the one that has the newest.
The feed carries no social link and no website at all, so PEZ is the one that answers 社媒
on this chain.
"""

from __future__ import annotations

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ...core.parse import positive_float as _num, utc_from_iso_text as _iso
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://peeps.wtf/api/launchpad/tokens"
PAGE_SIZE = 100
MAX_PAGES = 8
UNIVERSE_TTL = 900
TAB = "trending"


class Peeps:
    name = "peeps"
    chains = ("robinhood",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every launch this source knows, keyed by CA."""

        def produce():
            merged: dict[str, dict] = {}
            for page in range(1, MAX_PAGES + 1):
                url = "%s?tab=%s&page=%d&pageSize=%d&source=peeps" % (
                    BASE, TAB, page, PAGE_SIZE,
                )
                try:
                    payload = json_get(url, UA_JSON, tries=2, sleep=2.0, timeout=45.0)
                except SourceError:
                    break
                items = payload.get("items") if isinstance(payload, dict) else payload
                if not items:
                    break
                for row in items:
                    ca = ca_key(row.get("contractAddress"))
                    if ca and ca not in merged:
                        merged[ca] = row
                if len(items) < PAGE_SIZE:
                    break
            return merged or None

        return cached("peeps:tokens:%s" % chain_id, UNIVERSE_TTL, produce) or {}

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
            symbol=row.get("ticker") or None,
            description=str(row.get("description") or "").strip() or None,
            market_cap_usd=_num(row.get("marketCap")),
            high_market_cap_usd=_num(row.get("athMarketCap")),
            liquidity_usd=_num(row.get("liquidityUsd")),
            volume_24h_usd=_num(row.get("volumeUsd24h")),
            holders=int(row["holders"]) if row.get("holders") else None,
            price_change_24h_pct=_change_pct(row),
            launchpad=row.get("venueId") or row.get("source") or "peeps",
            created_at=_iso(row.get("createdAt")),
        )
        metrics.creator = (row.get("creatorAddress") or row.get("creator") or None)
        metrics.provenance = {
            "market_cap_usd": "peeps", "high_market_cap_usd": "peeps",
            "liquidity_usd": "peeps", "volume_24h_usd": "peeps", "holders": "peeps",
            "price_change_24h_pct": "peeps", "created_at": "peeps", "launchpad": "peeps",
            "creator": "peeps",
        }
        return metrics


def _change_pct(row: dict) -> float | None:
    try:
        return float(row.get("change"))
    except (TypeError, ValueError):
        return None
