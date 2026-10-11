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

import time
from typing import Any

from ..core.models import SafetyFacts, TokenMetrics, ca_key, is_placeholder_ca
from ..core.parse import positive_float as _num
from .base import web_url
from .http import NEGATIVE, SourceError, UA_JSON, cached, json_get

BASE = "https://api.geckoterminal.com/api/v2"
ROUTINE_PAGES = 3
#: Page 10 is the last one GeckoTerminal answers - 11 and up come back 401 on both
#: chains (probed 2026-10-06). The two pages this used to ask for were dead on
#: arrival, and a dead request burns its own retry cycle, so the fix drops two
#: doomed fetches from every sweep and adds no row.
FULL_PAGES = 10
#: Pool list changes slowly; a new pool still shows up on page 1 of `new_pools` first.
FULL_SWEEP_TTL = 6 * 3600
POOLS_TTL = 12 * 3600
#: A day of daily candles only changes once a day. Six hours was four times the traffic for
#: the same answer, and the traffic is what the API throttles - see `_read`.
OHLCV_TTL = 24 * 3600
TRADES_TTL = 3600
#: A 429 is the API asking for a slower lane, not an answer about the pool. `http.get`
#: already retries three times a second apart, which does not outlast a rate-limit window;
#: these are the tries on top of it, and the wait between them.
RATE_LIMIT_TRIES = 3
RATE_LIMIT_BACKOFF = 5.0
JOURNEY_DAYS = 7
#: The daily window the high is read from. GeckoTerminal answers with the candles the pool
#: actually has - a two-week-old pool returns fourteen - so asking for the maximum is asking
#: for the pool whole life, which is what the drawdown rule means by the historical high.
#: The card still draws `JOURNEY_DAYS` of it. Ten days was the old window and it read the
#: wrong high: the token this rule is for spiked at the very start of its pool and has been
#: falling since, so a ten-day high sits far above the price and close to it at the same
#: time - ARCAT read -81% on ten days against -96% on its whole life.
ATH_DAYS = 1000
#: The pools 距高回撤 reads. A token high is not always in its deepest pool: ARCAT's main
#: pool reached $3.06M of market cap while a second pool holding a third of its depth
#: printed $5.77M. A pool under `POOL_ATH_SHARE` of the deepest one reserves is a dust
#: print rather than a price anyone traded at, and is left out.
POOL_ATH_POOLS = 2
POOL_ATH_SHARE = 0.10


def _split_id(identifier: str | None, chain_id: str) -> str:
    """The address out of a GeckoTerminal id, which is spelled `<network>_<address>`.

    The network is the same slug the endpoint is addressed by, so the prefix is the chain
    rather than a constant: a hardcoded `arc_` answered "" for every other chain's ids,
    which reads downstream as "this pool has no address" instead of "wrong prefix".

    Returned in whatever case the feed spelled it, not folded to lower: on an EVM chain
    that is the same address either way, and on Solana it is the only address there is.
    Folding belongs in `models.ca_key`, where it is decided from the shape of the address,
    rather than here, where it would be every source guessing on its own.
    """
    text = str(identifier or "")
    prefix = "%s_" % chain_id
    return text[len(prefix):] if text.startswith(prefix) else ""


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


def pool_row(item: dict, chain_id: str) -> dict:
    """One pool, flattened. The pool address matters as much as the numbers: it is the
    handle every later per-token question is asked with."""
    attributes = item.get("attributes") or {}
    relationships = item.get("relationships") or {}
    relation = (relationships.get("base_token") or {}).get("data") or {}
    changes = attributes.get("price_change_percentage") or {}
    volumes = attributes.get("volume_usd") or {}
    transactions = (attributes.get("transactions") or {}).get("h24") or {}
    return {
        "base": _split_id(relation.get("id"), chain_id) or str((attributes.get("base_token") or {}).get("address") or "").lower(),
        "pool": _split_id(item.get("id"), chain_id) or str(attributes.get("address") or "").lower(),
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
    chains = ("arc", "bsc", "robinhood", "solana")

    # ---------------------------------------------------------------- universe

    def _pages(self, chain_id: str, page_numbers: list[int], extra: list[str]) -> list[dict]:
        urls = ["%s/networks/%s/pools?page=%d" % (BASE, chain_id, page) for page in page_numbers]
        urls += ["%s/networks/%s/%s?page=1" % (BASE, chain_id, kind) for kind in extra]
        rows: list[dict] = []
        empty = 0
        read = 0
        for url in urls:
            try:
                payload = json_get(url, UA_JSON, tries=3, sleep=10.0, timeout=30.0)
            except SourceError:
                empty += 1
                if empty >= 2:
                    break
                continue
            read += 1
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                empty += 1
                if empty >= 2:
                    break
                continue
            rows.extend(pool_row(item, chain_id) for item in data)
        # A URL that never answered is not a chain with no pools. With nothing read at all,
        # saying `[]` files a rate limit as an empty chain - this is the enumerator for
        # Solana, so the round would then report `ok=True, n=0` and look like a quiet market.
        # One page read, even an empty one, is what tells the two apart.
        if urls and not read:
            raise SourceError("geckoterminal: no pool page readable for %s" % chain_id)
        return rows

    def universe_pools(self, chain_id: str) -> list[dict]:
        """Best pool per token, routine pages fresh and the long tail from a cached sweep."""
        hot = self._pages(chain_id, list(range(1, ROUTINE_PAGES + 1)), ["new_pools", "trending_pools"])

        def sweep() -> list[dict] | None:
            cold = self._pages(
                chain_id,
                list(range(ROUTINE_PAGES + 1, FULL_PAGES + 1)),
                [],
            )
            return cold or None

        cold = cached("gt:sweep:%s" % chain_id, FULL_SWEEP_TTL, sweep) or []
        best: dict[str, dict] = {}
        for row in list(cold) + hot:
            base = row.get("base")
            # The zero address is a chain's own coin, not a token: GeckoTerminal spells the base
            # of every native pool with it, and one such row carries the chain's whole native
            # liquidity into the ranking with no market cap to be judged against. The cold half
            # arrives as rows the cache already built, so the check cannot live in the builder.
            if not base or is_placeholder_ca(base):
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

    def _read(self, url: str) -> Any:
        """One read, with a throttle treated as a wait rather than as an answer.

        GeckoTerminal answers a burst with HTTP 429 - measured 2026-09-28: five of twelve
        daily histories asked for at once came back 429 while the same requests a minute
        apart all succeeded. `http.get` retries three times a second apart, which does not
        outlast a rate-limit window, so a 429 is worth waiting out here. A 404 is the API
        saying "not one of ours" and is remembered (`http.NEGATIVE`); anything else is
        silence, and silence is `None` - never a value a rule may read as an answer.
        """
        for attempt in range(RATE_LIMIT_TRIES):
            try:
                return json_get(url, UA_JSON, tries=3, sleep=1.0, timeout=30.0)
            except SourceError as exc:
                text = str(exc)
                if "HTTP 404" in text:
                    return NEGATIVE
                if "HTTP 429" not in text or attempt + 1 >= RATE_LIMIT_TRIES:
                    return None
                time.sleep(RATE_LIMIT_BACKOFF * (attempt + 1))
        return None

    @staticmethod
    def _ath_pools(pools: list[dict]) -> list[dict]:
        """The pools whose history 距高回撤 reads: the deepest, plus any second pool that
        carries at least `POOL_ATH_SHARE` of its reserves. See `POOL_ATH_POOLS`."""
        ordered = sorted(pools, key=lambda row: -(row.get("liq") or 0))
        if not ordered:
            return []
        deepest = ordered[0].get("liq") or 0
        out = [ordered[0]]
        for row in ordered[1:]:
            if len(out) >= POOL_ATH_POOLS:
                break
            if deepest and (row.get("liq") or 0) >= deepest * POOL_ATH_SHARE:
                out.append(row)
        return out

    def _pools(self, chain_id: str, ca: str) -> tuple[list[dict] | None, str | None]:
        """This token's pools, and the mark that rode in with them.

        `include=base_token` puts the token resource beside its pools and the mark lives on
        that resource, so the one read this adapter already pays for carries it: the fallback
        costs the round no second request. It is matched by address because `base_token` is not
        always *this* token - a pool where the CA is the quote side includes the other side's
        resource, and another token's picture on this card is worse than an empty box (the
        rule the DexScreener read follows for its own `info`). Rows and mark are cached as one
        answer, so a hit cannot hand back one without the other.
        """

        def produce():
            payload = self._read(
                "%s/networks/%s/tokens/%s/pools?page=1&include=base_token" % (BASE, chain_id, ca)
            )
            if payload is None or payload is NEGATIVE:
                return payload
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                return NEGATIVE
            rows = []
            for item in data:
                attributes = item.get("attributes") or {}
                rows.append({
                    "name": attributes.get("name"),
                    "pool": _split_id(item.get("id"), chain_id) or str(attributes.get("address") or "").lower(),
                    "liq": _num(attributes.get("reserve_in_usd")),
                    "vol24": _num((attributes.get("volume_usd") or {}).get("h24")),
                })
            if not rows:
                return NEGATIVE
            mark = None
            for item in payload.get("included") or []:
                attributes = item.get("attributes") or {}
                if ca_key(attributes.get("address")) != ca_key(ca):
                    continue
                mark = web_url(attributes.get("image_url"))
                break
            return {"rows": rows, "mark": mark}

        # `cached` folds both "nothing" and "not ours" into None, so the dict is the only
        # shape that can come back with rows in it. The key moved to v2 with the mark: a v1
        # entry is a bare list, and reading one as the new shape says "this token has no
        # pools" for the rest of the 12-hour TTL.
        answer = cached("gt:pools:v2:%s" % ca, POOLS_TTL, produce)
        if not isinstance(answer, dict):
            return None, None
        return answer["rows"], answer["mark"]

    def journey(self, chain_id: str, pool: str | None) -> dict | None:
        """The six-day price path the card draws, plus how far it is below its historical high."""
        if not pool:
            return None

        def produce():
            payload = self._read(
                "%s/networks/%s/pools/%s/ohlcv/day?limit=%d&currency=usd"
                % (BASE, chain_id, pool, ATH_DAYS)
            )
            if payload is None or payload is NEGATIVE:
                return payload
            try:
                rows = sorted(payload["data"]["attributes"]["ohlcv_list"], key=lambda r: r[0])
            except (KeyError, TypeError):
                return None
            if not rows:
                # An empty history is an answer: this pool has never traded.
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
        pools, mark = self._pools(chain_id, ca)
        metrics = TokenMetrics(ca=ca)
        if not pools:
            return metrics, SafetyFacts()
        main = max(pools, key=lambda row: row.get("liq") or 0)
        total_liq = sum(row.get("liq") or 0 for row in pools)
        pair = main.get("pool")
        # The high is read off every pool worth reading, the journey off the deepest one:
        # the card draws the pool people trade in, and the fall is measured against the
        # highest price any of them reached (see `_ath_pools`).
        histories = [self.journey(chain_id, row.get("pool")) for row in self._ath_pools(pools)]
        path = histories[0] if histories else None
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
        if mark:
            metrics.logo_url = mark
            metrics.provenance["logo_url"] = "geckoterminal"
        if path:
            metrics.price_series = [point[1] for point in path["journey"]]
            metrics.provenance["price_series"] = "geckoterminal"
        highs = [row["ath"] for row in histories if row and row.get("ath")]
        last = path["last"] if path else metrics.price_usd
        if highs and last:
            metrics.price_ath_usd = max(highs)
            metrics.drawdown_pct = (last / metrics.price_ath_usd - 1) * 100
            metrics.provenance["drawdown_pct"] = "geckoterminal"
        else:
            # No reading at all: the pools answered nothing, or could not be asked. That is
            # 未判定 - `filters.check_drawdown` prints it rather than passing in silence,
            # which is what a throttled read used to look like.
            metrics.drawdown_unread = True
        if path:
            metrics.price_usd = metrics.price_usd or path["last"]
        return metrics, SafetyFacts()
