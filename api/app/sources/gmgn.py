"""GMGN: the ranking signal, the anti-bot numbers, and the dev's history.

This is the only source that needs the browser lane. GMGN's edge answers `requests`
and `urllib` and 403s `httpx` on the same URL with the same headers (measured
2026-09-24, SOURCES.md), so requests here go out through curl_cffi replaying
Chromium. That detail is contained to this file.

It returns two different things and both matter:

- the swap rank, which is how a token that RadarDEX has not indexed yet is found at all;
- per-token statistics no other source on Arc sells - bundler share, sniper hold,
  entrapment (the 貔貅 signal), insider share, the dev's own holding and how many
  tokens the dev has launched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.models import SafetyFacts, TokenMetrics
from .base import social_url, web_url
from .http import SourceError, UA_GMGN, cached, json_get, pmap

BASE = "https://gmgn.ai"
TRANSPORT = "browser"
PERIODS = ("1m", "5m", "1h", "6h", "24h")
PAGES = (1, 2)
PAGE_SIZE = 100
#: The rank moves every few seconds; this cache only exists to stop two stages in the
#: same round from paying for the same page twice.
RANK_TTL = 240
STAT_TTL = 900
#: Taxes and the honeypot verdict move only when the owner moves them, and the sheet is
#: one request per token in the enrich phase, so it is worth a longer memory than the
#: statistics beside it.
SECURITY_TTL = 21600


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _positive(value: Any) -> float | None:
    number = _num(value)
    return number if number and number > 0 else None


def _first(*values: Any) -> Any:
    """The first value that is not None.

    `or` is the wrong tool for these: it reads a measured zero as "no answer", which is
    how a token with no bundlers and no snipers came out 未判定 instead of 0%.
    """
    for value in values:
        if value is not None:
            return value
    return None


def _fraction_to_pct(value: Any) -> float | None:
    """GMGN reports shares as 0..1; every threshold in config is a percentage."""
    number = _num(value)
    return None if number is None else round(number * 100, 4)


def _when(value: Any) -> datetime | None:
    """GMGN's timestamps are unix seconds. A zero is "no answer", not 1970."""
    seconds = _num(value)
    if not seconds or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _count(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _symbol(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if " / " in text:
        text = text.split(" / ")[0].strip()
    return text or None


class GmGn:
    name = "gmgn"
    chains = ("arc",)

    # ---------------------------------------------------------------- universe

    def _rank_page(self, chain_id: str, period: str, page: int) -> list[dict]:
        url = "%s/defi/quotation/v1/rank/%s/swaps/%s?orderby=swaps&direction=desc&limit=%d&page=%d" % (
            BASE, chain_id, period, PAGE_SIZE, page,
        )
        try:
            payload = json_get(url, UA_GMGN, transport=TRANSPORT, tries=2, sleep=4.0, timeout=30.0)
        except SourceError:
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        return (data or {}).get("rank") or []

    def rank_rows(self, chain_id: str) -> dict[str, dict]:
        """Every token the rank showed in any window, keyed by CA. The 24h row wins.

        Asking five windows is not redundant: a token that spiked an hour ago and died
        back has no 24h presence, and those are exactly the ones a radar is for.
        """

        def produce():
            tasks = [(period, page) for period in PERIODS for page in PAGES]
            pages = pmap(lambda task: (task[0], self._rank_page(chain_id, task[0], task[1])), tasks, workers=3)
            merged: dict[str, dict] = {}
            for period, rows in pages:
                for row in rows or []:
                    ca = str(row.get("address") or "").lower()
                    if not ca:
                        continue
                    if ca not in merged or period == "24h":
                        merged[ca] = row
            return merged or None

        return cached("gmgn:rank:%s" % chain_id, RANK_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = []
        for ca, row in self.rank_rows(chain_id).items():
            out.append(self._metrics(ca, row))
        return out

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        return TokenMetrics(
            ca=ca,
            symbol=_symbol(row.get("symbol")),
            market_cap_usd=_positive(row.get("market_cap")),
            liquidity_usd=_positive(row.get("liquidity")),
            volume_24h_usd=_positive(row.get("volume")),
            holders=int(row["holder_count"]) if _positive(row.get("holder_count")) else None,
            top10_pct=_fraction_to_pct(row.get("top_10_holder_rate")),
            bundling_pct=_fraction_to_pct(row.get("bundler_rate")),
            sniper_pct=_fraction_to_pct(row.get("top70_sniper_hold_rate")),
            insider_pct=_fraction_to_pct(row.get("rat_trader_amount_rate")),
            honeypot_detected=_honeypot(row.get("is_honeypot")),
            # No tax from here: the ranking row carries a hard "0" for every Arc token
            # (measured 2026-09-25), which is a placeholder rather than a reading. The
            # number `_security` returns is the one GMGN's own page prints.
            launchpad=row.get("launchpad") or None,
            price_usd=_positive(row.get("price")),
            price_change_24h_pct=_num(row.get("price_change_percent")),
            creator=row.get("creator") or None,
            bot_degen_pct=_fraction_to_pct(row.get("bot_degen_rate")),
            bluechip_holder_pct=_fraction_to_pct(row.get("bluechip_owner_percentage")),
            total_supply=_num(row.get("total_supply")),
            open_at=_first(_when(row.get("open_timestamp")), _when(row.get("creation_timestamp"))),
            renames=int(row["twitter_rename_count"]) if _num(row.get("twitter_rename_count")) else None,
            socials=_socials(row),
            websites=_websites(row),
            provenance={
                "market_cap_usd": "gmgn", "liquidity_usd": "gmgn", "volume_24h_usd": "gmgn",
                "top10_pct": "gmgn", "bundling_pct": "gmgn",
                "bot_degen_pct": "gmgn", "total_supply": "gmgn", "open_at": "gmgn",
            },
        )

    # ------------------------------------------------------------- per token

    def _stat(self, chain_id: str, ca: str) -> dict:
        def produce():
            try:
                payload = json_get("%s/api/v1/token_stat/%s/%s" % (BASE, chain_id, ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:stat:%s" % ca, STAT_TTL, produce) or {}

    def _info(self, chain_id: str, ca: str) -> dict:
        """The token's own record: when trading opened, and the supply figures.

        `token_stat` answers the anti-bot numbers, `token_info` the supply; a card that
        prints 开盘/供应量 needs this for every token the rank never listed, which on Arc
        is most of them.
        """

        def produce():
            try:
                payload = json_get("%s/api/v1/token_info/%s/%s" % (BASE, chain_id, ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:info:%s" % ca, STAT_TTL, produce) or {}

    def _security(self, chain_id: str, ca: str) -> dict:
        """The sheet behind GMGN's own security panel: taxes, honeypot, LP lock summary.

        `token_stat` carries no tax at all, and the ranking row reports a hard "0" on
        Arc, so this is the only GMGN endpoint that answers the 税 row. Its own
        `buy_tax`/`sell_tax` are "0" as well - what GMGN prints is the `total_*` pair,
        the base tax plus the transfer tax a global-tax token adds.
        """

        def produce():
            try:
                payload = json_get("%s/api/v1/token_security_evm/%s/%s" % (BASE, chain_id, ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:security:%s" % ca, SECURITY_TTL, produce) or {}

    def dev_info(self, chain_id: str, ca: str) -> dict:
        """The dev's own record: how much they still hold, and the X rename history."""

        def produce():
            try:
                payload = json_get("%s/api/v1/token_dev_info/%s/%s" % (BASE, chain_id, ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:dev:%s" % ca, STAT_TTL, produce) or {}

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        # The three per-token lookups go out together: asked in sequence they would add a
        # third of the enrich phase's wall time for numbers the card shows side by side.
        asked = (
            lambda: self._stat(chain_id, ca),
            lambda: self.dev_info(chain_id, ca),
            lambda: self._info(chain_id, ca),
            lambda: self._security(chain_id, ca),
        )
        stat, dev, info, security = [answer or {} for answer in pmap(lambda ask: ask(), asked, workers=len(asked))]
        metrics = TokenMetrics(ca=ca)
        facts = SafetyFacts()

        holders = _positive(stat.get("holder_count"))
        if holders:
            metrics.holders = int(holders)
        entrapment = _fraction_to_pct(stat.get("top_entrapment_trader_percentage"))
        if entrapment is not None:
            metrics.honeypot_pct = entrapment
            facts.entrapment_pct = entrapment
        metrics.top10_pct = _first(
            _fraction_to_pct(stat.get("top_10_holder_rate")), metrics.top10_pct)
        metrics.bundling_pct = _first(
            _fraction_to_pct(stat.get("top_bundler_trader_percentage")), metrics.bundling_pct)
        facts.bundling_pct = metrics.bundling_pct
        metrics.sniper_pct = _first(
            _fraction_to_pct(stat.get("top70_sniper_hold_rate")), metrics.sniper_pct)
        if metrics.sniper_pct is not None:
            facts.sniper_pct = metrics.sniper_pct
        metrics.insider_pct = _first(
            _fraction_to_pct(stat.get("top_rat_trader_percentage")), metrics.insider_pct)
        if metrics.insider_pct is not None:
            facts.insider_pct = metrics.insider_pct

        metrics.bot_degen_pct = _first(
            _fraction_to_pct(stat.get("bot_degen_rate")), metrics.bot_degen_pct)
        metrics.fresh_wallet_pct = _fraction_to_pct(stat.get("fresh_wallet_rate"))
        metrics.private_vault_pct = _fraction_to_pct(stat.get("private_vault_hold_rate"))
        metrics.bluechip_holder_pct = _first(
            _fraction_to_pct(stat.get("bluechip_owner_percentage")), metrics.bluechip_holder_pct)
        metrics.degen_call_count = _count(stat.get("degen_call_count"))
        metrics.signal_count = _count(stat.get("signal_count"))
        metrics.open_at = _first(
            _when(info.get("open_timestamp")), _when(info.get("creation_timestamp")),
            metrics.open_at)
        metrics.total_supply = _first(_num(info.get("total_supply")), metrics.total_supply)
        metrics.circulating_supply = _num(info.get("circulating_supply"))
        metrics.max_supply = _num(info.get("max_supply"))
        metrics.buy_tax_pct = _tax_pct(security, "total_buy_tax", "buy_tax")
        metrics.sell_tax_pct = _tax_pct(security, "total_sell_tax", "sell_tax")
        # A second voice on the verdict, for the tokens GoPlus has no row for at all.
        if metrics.honeypot_detected is None:
            metrics.honeypot_detected = _honeypot(security.get("is_honeypot"))

        dev_hold = stat.get("dev_team_hold_rate") if stat.get("dev_team_hold_rate") is not None else stat.get("creator_hold_rate")
        facts.dev_holding_pct = _fraction_to_pct(dev_hold)
        # A dev with no other launches is a finding, not a gap: `_positive` read that
        # zero as "no answer" and the card said 未判定 where the truth was 0.
        created = _num(stat.get("creator_created_count"))
        facts.dev_created_count = int(created) if created is not None else None
        facts.holders = metrics.holders
        facts.top10_pct = metrics.top10_pct

        if dev.get("creator_address"):
            metrics.creator = str(dev["creator_address"]).lower()
        renames = dev.get("twitter_rename_count")
        if renames is None:
            renames = len(dev.get("twitter_name_change_history") or [])
        if _num(renames):
            metrics.renames = int(renames)
        metrics.provenance.update({
            "holders": "gmgn", "top10_pct": "gmgn", "bundling_pct": "gmgn",
            "honeypot_pct": "gmgn",
            "bot_degen_pct": "gmgn", "fresh_wallet_pct": "gmgn",
            "private_vault_pct": "gmgn", "signal_count": "gmgn",
            "open_at": "gmgn", "total_supply": "gmgn",
            "buy_tax_pct": "gmgn", "sell_tax_pct": "gmgn",
            "honeypot_detected": "gmgn",
        })
        return metrics, facts


def _tax_pct(row: dict, total: str, direct: str) -> float | None:
    """GMGN's tax as a percentage of the trade.

    Both keys arrive as 0..1 fractions. The `total_*` headline is the answer - it is
    what the page shows and what a global-tax token actually costs - while the plain
    `*_tax` is the base alone and reads "0" on tokens whose tax is real.
    """
    headline = _fraction_to_pct(row.get(total))
    return headline if headline is not None else _fraction_to_pct(row.get(direct))


def _honeypot(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes")
    return bool(int(value))


def _websites(row: dict) -> list[str]:
    url = web_url(row.get("website"))
    return [url] if url else []


def _socials(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, kind in (("twitter_username", "twitter"), ("telegram", "telegram")):
        url = social_url(kind, row.get(key))
        if url:
            out[kind] = url
    return out
