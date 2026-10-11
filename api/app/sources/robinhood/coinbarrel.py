"""CoinBarrel: the Robinhood chain's own index, and the widest list on it.

One endpoint answers the whole chain (293 tokens, a page clamped at 200). Three of its
columns have to be read before they are used:

- `market_cap_usd` is unset on *every* row (measured 2026-09-30: 0/50 filled) while
  `fdv_usd` is filled 50/50. The market cap here is the FDV; there is no second number to
  prefer, and reading the unset column alone would leave every row blank.
- `liquidity_primary_usd` (45/50) and `volume_usd_24h` (31/50) are only partly filled, and
  both have an exact fallback: the native-pool column times the row's own `quote_usd_rate`.
  That rate is the index's own conversion, not a price this module guesses.
- `price_change_24h` is a signed percent (measured on graduated rows: -28.72, -0.15, +1.52),
  so it is read with `_signed`. The positive-only reader the other adapters share would turn
  every fall into a blank.

`peak_fdv_usd` is filled 50/50 and is the token's own high-water mark. That is the same kind
of measurement as GMGN's `history_highest_market_cap` - a source's own record of the high -
and not interchangeable with GeckoTerminal's pool history, which answers in a price. 距高回撤
reads it.

Two things this source does not carry, and both matter for what it can be asked: no social
link on any of the 50 rows read, and no `pool_address` (the pools are Uniswap v4, named by a
32-byte `evm_pool_id` with no contract behind it). So it is a breadth and high-water source,
never the one that answers 社媒.
"""

from __future__ import annotations

from typing import Any

from ...core.filters import is_empty_shell
from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ...core.parse import positive_float as _num, utc_from_unix as _iso
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://coinbarrel.com/market/api/v1"
#: The endpoint clamps a page to 200 whatever you ask for (measured: `limit=300` answered
#: 200), and the chain is 293 tokens, so two pages cover it and the walk stops on
#: `has_more` rather than on a page count.
PAGE_LIMIT = 200
MAX_PAGES = 8
UNIVERSE_TTL = 900
#: The only ordering this endpoint accepts. `sort_by=volume_usd_24h` and
#: `sort_by=market_cap_usd` both answer 400 (measured); `volume_sol_24h` is a Solana-era
#: name left over from the index's other deployment, and it sorts fine on Robinhood.
SORT = "volume_sol_24h"


def _signed(value: Any) -> float | None:
    """A change: zero is a real reading and negatives are the common case."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(row: dict) -> float | None:
    return _num(row.get("quote_usd_rate"))


class CoinBarrel:
    name = "coinbarrel"
    chains = ("robinhood",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every token this source knows, keyed by CA, first sighting wins."""

        def produce():
            merged: dict[str, dict] = {}
            for page in range(MAX_PAGES):
                url = "%s/tokens?chain=%s&sort_by=%s&sort_dir=desc&limit=%d&offset=%d" % (
                    BASE, chain_id, SORT, PAGE_LIMIT, page * PAGE_LIMIT,
                )
                try:
                    payload = json_get(url, UA_JSON, tries=2, sleep=2.0, timeout=45.0)
                except SourceError:
                    break
                items = payload.get("tokens") if isinstance(payload, dict) else payload
                if not items:
                    break
                for row in items:
                    ca = ca_key(row.get("mint"))
                    if ca and ca not in merged:
                        merged[ca] = row
                if not (isinstance(payload, dict) and payload.get("has_more")):
                    break
            return merged or None

        return cached("cb:tokens:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """Every row the index lists, minus the ones carrying nothing to measure.

        A row with neither a market cap nor a reserve is an address with a symbol on it,
        and the free half drops exactly that shape a moment later as 空壳淘汰 (measured
        2026-10-06: 85 of the 302 rows this feed lists, against 0 from every other source
        on the chain). Leaving them in the pull spends funnel rows on tokens no rule can
        judge. Either number answering keeps the row: the predicate is
        `filters.is_empty_shell`, the same one the 空壳淘汰 rule reads.
        """
        out: list[TokenMetrics] = []
        for ca, row in self.rows(chain_id).items():
            metrics = self._metrics(ca, row)
            if is_empty_shell(metrics):
                continue
            out.append(metrics)
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        """The bulk row, which is already cached, re-read as a per-token answer: it carries
        the deployer, and that is what the LP question is asked about."""
        row = self.rows(chain_id).get(ca_key(ca))
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(ca_key(ca), row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        rate = _rate(row)
        liquidity = _num(row.get("liquidity_primary_usd"))
        if liquidity is None and rate:
            native = _num(row.get("pool_total_liquidity_native"))
            liquidity = native * rate if native else None
        volume = _num(row.get("volume_usd_24h"))
        if volume is None and rate:
            native_volume = _num(row.get("volume_sol_24h"))
            volume = native_volume * rate if native_volume else None
        decimals = row.get("decimals") or 18
        supply = _num(row.get("total_supply"))
        metrics = TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            market_cap_usd=_num(row.get("market_cap_usd")) or _num(row.get("fdv_usd")),
            high_market_cap_usd=_num(row.get("peak_fdv_usd")),
            liquidity_usd=liquidity,
            volume_24h_usd=volume,
            holders=int(row["holder_count"]) if row.get("holder_count") else None,
            price_usd=_num(row.get("price_usd")),
            price_change_24h_pct=_signed(row.get("price_change_24h")),
            created_at=_iso(row.get("created_at")),
            total_supply=(supply / (10 ** int(decimals))) if supply else None,
        )
        metrics.creator = row.get("evm_deployer_address") or row.get("creator") or None
        metrics.provenance = {
            "market_cap_usd": "coinbarrel",
            "high_market_cap_usd": "coinbarrel",
            "liquidity_usd": "coinbarrel",
            "volume_24h_usd": "coinbarrel",
            "holders": "coinbarrel",
            "price_usd": "coinbarrel",
            "price_change_24h_pct": "coinbarrel",
            "created_at": "coinbarrel",
            "creator": "coinbarrel",
        }
        return metrics
