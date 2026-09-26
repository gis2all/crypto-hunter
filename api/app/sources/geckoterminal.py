"""GeckoTerminal: the universe pull, the pool table, and the price journey.

Public API, no key, and the only source that can enumerate a chain without being told
what to look for. It is also the most expensive one: a full sweep is fifteen requests
at a cadence the API tolerates, so the sweep is cached for six hours and the routine
round only re-reads the pages that new pools actually appear on.

One pool row carries everything the prefilter needs - market cap, reserves, 24h
volume, 24h price change, pool creation time - which is why the coarse pass can run
without a single per-token request.
"""

from __future__ import annotations

from typing import Any

from ..core.models import SafetyFacts, TokenMetrics
from .http import NEGATIVE, SourceError, UA_JSON, cached, json_get

BASE = "https://api.geckoterminal.com/api/v2"
ROUTINE_PAGES = 3
FULL_PAGES = 12
#: Pool list changes slowly; a new pool still shows up on page 1 of `new_pools` first.
FULL_SWEEP_TTL = 6 * 3600
POOLS_TTL = 12 * 3600
OHLCV_TTL = 6 * 3600
TRADES_TTL = 3600
JOURNEY_DAYS = 7


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _split_id(identifier: str | None, prefix: str = "arc_") -> str:
    text = str(identifier or "")
    return text.split("_", 1)[1].lower() if text.startswith(prefix) else ""


def base_symbol(name: str | None) -> str | None:
    """The base token's symbol as GeckoTerminal spells it: the first half of the pool name.

    Terminal is the only source that can enumerate the chain and the only one that does not
    hand the symbol over separately - it writes `ARK / USDC 1%`. Filling the blank from the
    name is worth it: otherwise a card whose token no other source indexed shows the
    missing-value marker where its name should be.
    """
    if not name:
        return None
    head = str(name).split(" / ")[0].strip()
    return head or None


def pool_row(item: dict) -> dict:
    """One pool, flattened. The pool address matters as much as the numbers: it is the
    handle every later per-token question is asked with."""
    attributes = item.get("attributes") or {}
    relationships = item.get("relationships") or {}
    relation = (relationships.get("base_token") or {}).get("data") or {}
    changes = attributes.get("price_change_percentage") or {}
    volumes = attributes.get("volume_usd") or {}
    transactions = (attributes.get("transactions") or {}).get("h24") or {}
    return {
        "base": _split_id(relation.get("id")) or str((attributes.get("base_token") or {}).get("address") or "").lower(),
        "pool": _split_id(item.get("id")) or str(attributes.get("address") or "").lower(),
        "name": attributes.get("name"),
        "sym": base_symbol(attributes.get("name")),
        "fdv": _num(attributes.get("fdv_usd")),
        "mcap": _num(attributes.get("market_cap_usd")),
        "liq": _num(attributes.get("reserve_in_usd")),
        "vol24": _num(volumes.get("h24")),
        "price": _num(attributes.get("base_token_price_usd")),
        "chg24": _num(changes.get("h24")),
        "buys24": transactions.get("buys"),
        "sells24": transactions.get("sells"),
        "created": attributes.get("pool_created_at"),
    }


class GeckoTerminal:
    name = "geckoterminal"
    chains = ("arc",)

    # ---------------------------------------------------------------- universe

    def _pages(self, chain_id: str, page_numbers: list[int], extra: list[str]) -> list[dict]:
        urls = ["%s/networks/%s/pools?page=%d" % (BASE, chain_id, page) for page in page_numbers]
        urls += ["%s/networks/%s/%s?page=1" % (BASE, chain_id, kind) for kind in extra]
        rows: list[dict] = []
        empty = 0
        for url in urls:
            try:
                payload = json_get(url, UA_JSON, tries=3, sleep=10.0, timeout=30.0)
            except SourceError:
                empty += 1
                if empty >= 2:
                    break
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                empty += 1
                if empty >= 2:
                    break
                continue
            rows.extend(pool_row(item) for item in data)
        return rows

    def universe_pools(self, chain_id: str) -> list[dict]:
        """Best pool per token, routine pages fresh and the long tail from a cached sweep."""
        hot = self._pages(chain_id, list(range(1, ROUTINE_PAGES + 1)), ["new_pools", "trending_pools"])

        def sweep() -> list[dict] | None:
            cold = self._pages(
                chain_id,
                list(range(ROUTINE_PAGES + 1, FULL_PAGES + 1)),
                ["new_pools", "trending_pools"],
            )
            return cold or None

        cold = cached("gt:sweep:%s" % chain_id, FULL_SWEEP_TTL, sweep) or []
        best: dict[str, dict] = {}
        for row in list(cold) + hot:
            base = row.get("base")
            if not base:
                continue
            if base not in best or (_num(row.get("liq")) or 0) > (_num(best[base].get("liq")) or 0):
                best[base] = row
        return list(best.values())

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        rows = self.universe_pools(chain_id)[: max(0, limit)] if limit else self.universe_pools(chain_id)
        out = []
        for row in rows:
            out.append(
                TokenMetrics(
                    ca=row["base"],
                    symbol=row.get("sym"),
                    market_cap_usd=row["mcap"] or row["fdv"],
                    liquidity_usd=row["liq"],
                    volume_24h_usd=row["vol24"],
                    price_usd=row["price"],
                    price_change_24h_pct=row["chg24"],
                    dex_pair=row["pool"] or None,
                    main_pool=row["name"],
                    provenance={
                        "market_cap_usd": "geckoterminal",
                        "liquidity_usd": "geckoterminal",
                        "volume_24h_usd": "geckoterminal",
                        "dex_pair": "geckoterminal",
                        # Provenance claims a source only when the source had a value:
                        # `symbol` here is derived from the pool name and can be absent.
                        **({"symbol": "geckoterminal"} if row.get("sym") else {}),
                    },
                )
            )
        return out

    # ------------------------------------------------------------- per token

    def _pools(self, chain_id: str, ca: str) -> list[dict] | None:
        def produce():
            try:
                payload = json_get(
                    "%s/networks/%s/tokens/%s/pools?page=1" % (BASE, chain_id, ca),
                    UA_JSON, tries=3, sleep=1.0, timeout=30.0,
                )
            except SourceError as exc:
                # 404 is GeckoTerminal saying "not one of ours"; anything else is silence.
                # Only the first is worth remembering - see `http.NEGATIVE`.
                return NEGATIVE if "HTTP 404" in str(exc) else None
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                return NEGATIVE
            rows = []
            for item in data:
                attributes = item.get("attributes") or {}
                rows.append({
                    "name": attributes.get("name"),
                    "pool": _split_id(item.get("id")) or str(attributes.get("address") or "").lower(),
                    "liq": _num(attributes.get("reserve_in_usd")),
                    "vol24": _num((attributes.get("volume_usd") or {}).get("h24")),
                })
            return rows or NEGATIVE

        return cached("gt:pools:%s" % ca, POOLS_TTL, produce)

    def journey(self, chain_id: str, pool: str | None) -> dict | None:
        """The six-day price path the card draws, plus how far it has fallen from its high."""
        if not pool:
            return None

        def produce():
            try:
                payload = json_get(
                    "%s/networks/%s/pools/%s/ohlcv/day?limit=10&currency=usd" % (BASE, chain_id, pool),
                    UA_JSON, tries=3, sleep=1.0, timeout=30.0,
                )
            except SourceError as exc:
                return NEGATIVE if "HTTP 404" in str(exc) else None
            try:
                rows = sorted(payload["data"]["attributes"]["ohlcv_list"], key=lambda r: r[0])
            except (KeyError, TypeError):
                return None
            if not rows:
                # An empty history is an answer: this pool has not traded in ten days.
                return NEGATIVE
            high = max(float(row[2]) for row in rows)
            last = float(rows[-1][4])
            return {
                "ath": high,
                "last": last,
                "dd": (last / high - 1) * 100 if high else None,
                "journey": [[int(row[0]), float(row[4])] for row in rows[-JOURNEY_DAYS:]],
            }

        return cached("gt:ohlcv:%s" % pool, OHLCV_TTL, produce)

    def trades(self, chain_id: str, pool: str | None) -> dict | None:
        """A 24h sample of the pool's trades. Used to answer "can people actually sell"."""
        if not pool:
            return None

        def produce():
            try:
                payload = json_get(
                    "%s/networks/%s/pools/%s/trades" % (BASE, chain_id, pool),
                    UA_JSON, tries=3, sleep=1.0, timeout=30.0,
                )
            except SourceError as exc:
                return NEGATIVE if "HTTP 404" in str(exc) else None
            rows = []
            for item in payload.get("data") or []:
                attributes = item.get("attributes") or {}
                rows.append({
                    "kind": attributes.get("kind"),
                    "usd": _num(attributes.get("volume_in_usd")),
                    "who": str(attributes.get("tx_from_address") or "")[:10],
                    "ts": attributes.get("block_timestamp"),
                })
            if not rows:
                return NEGATIVE
            rows.sort(key=lambda r: -(r["usd"] or 0))
            sells = sum(1 for r in rows if r.get("kind") == "sell")
            return {"top": rows[:5], "n": len(rows), "sells": sells, "buys": len(rows) - sells}

        return cached("gt:trades:%s" % pool, TRADES_TTL, produce)

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        pools = self._pools(chain_id, ca)
        metrics = TokenMetrics(ca=ca)
        if not pools:
            return metrics, SafetyFacts()
        main = max(pools, key=lambda row: row.get("liq") or 0)
        total_liq = sum(row.get("liq") or 0 for row in pools)
        pair = main.get("pool")
        path = self.journey(chain_id, pair)
        metrics.pools = len(pools)
        metrics.pool_volume_usd = sum(row.get("vol24") or 0 for row in pools) or None
        metrics.main_pool = main.get("name")
        metrics.main_pool_share_pct = (
            100.0 * (main.get("liq") or 0) / total_liq if total_liq else None
        )
        metrics.dex_pair = pair
        metrics.provenance.update({
            "pools": "geckoterminal", "main_pool": "geckoterminal", "dex_pair": "geckoterminal",
        })
        if path:
            metrics.price_series = [point[1] for point in path["journey"]]
            metrics.price_ath_usd = path["ath"]
            metrics.drawdown_pct = path["dd"]
            metrics.price_usd = metrics.price_usd or path["last"]
            metrics.provenance["price_series"] = "geckoterminal"
        return metrics, SafetyFacts()
