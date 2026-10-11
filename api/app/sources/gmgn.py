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

from ..core.models import SafetyFacts, TokenMetrics, ca_key
from .base import social_url, web_url
from .http import SourceError, UA_GMGN, cached, json_get, pmap

BASE = "https://gmgn.ai"
TRANSPORT = "browser"
PERIODS = ("1m", "5m", "1h", "6h", "24h")
PAGES = (1, 2)
PAGE_SIZE = 100

#: GMGN spells one chain its own way: its Solana board is `sol`, where this app calls the
#: chain `solana` - that is the spelling DexScreener and GeckoTerminal use, so one chain id
#: cannot be both. A wrong slug here is not a visible failure, which is why it needs a
#: table rather than a guess: GMGN answers an unknown chain with a 200 and an empty rank,
#: so it reads as "this chain has no tokens". Measured 2026-10-11: `rank/sol/swaps/24h`
#: returns 100 rows, `rank/solana/swaps/24h` returns 0. The `chains` tuple above is the
#: same fact from the other side, and this is the shape `goplus.CHAIN_IDS` and
#: `sourcify.CHAIN_IDS` already have.
CHAIN_SLUGS: dict[str, str] = {"solana": "sol"}

#: The security sheet path per chain: GMGN spelled the same panel `_evm` for EVM chains and
#: `_sol` for Solana, and a chain answers under no other name. Measured 2026-10-11:
#: `token_security_sol/sol/<ca>` answers 13 keys where the `_evm` path answers nothing. The
#: two families carry different fields, which is what the table below is about.
SECURITY_PATHS: dict[str, str] = {"solana": "token_security_sol"}
SECURITY_PATH = "token_security_evm"

#: Chains whose sheet reports no lock percentage and only ever a bare flag. Solana is one:
#: its LP is a pool position rather than a lockable pair, and the sheet says so - for all ten
#: tokens measured here (2026-10-11) `lock_percent` was "0" and `lock_tags` null, while
#: `is_locked` tracked `burn_status`. It also said `false` for BONK and WIF, which nobody
#: would call unlocked, so on this chain a `false` reads as "GMGN has no locker record"
#: rather than as 未锁. A `false` is a hard drop (`core/filters.py`), so writing one down
#: from a record that does not exist would reject the chain on a silence. The positive is
#: kept: it is real evidence, and 7 of the 8 Solana rank rows measured answered `true`.
LP_LOCK_ONLY_WHEN_TRUE: tuple[str, ...] = ("solana",)


def slug(chain_id: str) -> str:
    """This chain as GMGN spells it. A chain with no entry is already spelled right."""
    return CHAIN_SLUGS.get(str(chain_id), str(chain_id))


#: The rank moves every few seconds; this cache only exists to stop two stages in the
#: same round from paying for the same page twice.
RANK_TTL = 240
STAT_TTL = 900
#: Taxes and the honeypot verdict move only when the owner moves them, and the sheet is
#: one request per token in the enrich phase, so it is worth a longer memory than the
#: statistics beside it.
SECURITY_TTL = 21600


def _bool(value: Any) -> bool | None:
    """A feed's boolean, in whatever shape it arrived.

    `lock_summary.is_locked` is a real JSON boolean, while the `0/1` fields next to it
    arrive as strings - `_flag` in the GoPlus adapter reads only the string form, so it
    would call every JSON `true` a false.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


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
    chains = ("arc", "bsc", "robinhood", "solana")

    # ---------------------------------------------------------------- universe

    def _rank_page(self, chain_id: str, period: str, page: int) -> list[dict]:
        """One board. A page that could not be read raises; an empty board returns `[]`.

        The two used to look the same, and keeping them apart is the whole point of this
        signature: the rank is the only place a token no launchpad adapter knows is found
        at all, so a rate limit answered as `[]` left the source reporting `ok=True, n=0`
        - indistinguishable from a chain that genuinely has nothing - and the round ran on
        to the end with an empty universe. `rank_rows` reads the two apart.
        """
        url = "%s/defi/quotation/v1/rank/%s/swaps/%s?orderby=swaps&direction=desc&limit=%d&page=%d" % (
            BASE, slug(chain_id), period, PAGE_SIZE, page,
        )
        payload = json_get(url, UA_GMGN, transport=TRANSPORT, tries=2, sleep=4.0, timeout=30.0)
        data = payload.get("data") if isinstance(payload, dict) else None
        return (data or {}).get("rank") or []

    def rank_rows(self, chain_id: str) -> dict[str, dict]:
        """Every token the rank showed in any window, keyed by CA, with the window that
        answered for it under `_window` - the 24h row winning whenever the token is on
        that board.

        Asking five windows is not redundant: a token that spiked an hour ago and died
        back has no 24h presence, and those are exactly the ones a radar is for.

        The window travels with the row because two of its fields are scoped to it.
        `volume` and `price_change_percent` are the *window's* totals, not the day's: for
        one Arc token on 2026-09-29 the boards read 47.58 (5m), 59.56 (1h) and 9,386.81
        (6h) where the real 24h figure was 14,549 (two independent feeds agreed). Those
        readings are not smaller 24-hour numbers, they are different measurements wearing
        the day's name - and a wrong number is worse than a blank here, because 未判定
        never rejects. `_metrics` reads either field off a 24h row only.

        The window rides *inside* the row rather than beside it because this answer is
        cached through JSON, which hands a `(period, row)` pair back as a list.
        """

        def produce():
            tasks = [(period, page) for period in PERIODS for page in PAGES]
            pages = pmap(lambda task: (task[0], self._rank_page(chain_id, task[0], task[1])), tasks, workers=3)
            # `pmap` hands back a page that raised as None, so the filter below is the whole
            # of the difference between the source failing and the chain being empty. Every
            # board unreadable is the former and is raised to `collect`, which files it as
            # `ok=False`; no board at all is not a verdict either way - `cached` keeps
            # neither an exception nor an empty answer, so the next round asks again.
            readable = [page for page in pages if page is not None]
            if not readable:
                raise SourceError("gmgn: no rank page readable for %s" % chain_id)
            merged: dict[str, dict] = {}
            for period, rows in readable:
                for row in rows or []:
                    ca = ca_key(row.get("address"))
                    if not ca:
                        continue
                    if ca not in merged or period == "24h":
                        merged[ca] = {**row, "_window": period}
            return merged or None

        # The key carries a shape version. A row cached before `_window` existed has no
        # marker, and `fetch_universe` reads one - a stale entry would raise a KeyError in
        # the middle of a round instead of degrading, so the bump retires those rows.
        return cached("gmgn:rank:v2:%s" % chain_id, RANK_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = []
        for ca, row in self.rank_rows(chain_id).items():
            out.append(self._metrics(ca, row, row["_window"]))
        return out

    def _metrics(self, ca: str, row: dict, period: str) -> TokenMetrics:
        # Only the 24h board answers a 24-hour question; see `rank_rows`.
        daily = period == "24h"
        metrics = TokenMetrics(
            ca=ca,
            symbol=_symbol(row.get("symbol")),
            market_cap_usd=_positive(row.get("market_cap")),
            # The token own high, straight off the ranking row - the one reading of it no
            # other source on this chain sells. 距高回撤 wants it first: it is what the
            # source measured, not what one of our rounds happened to sample.
            high_market_cap_usd=_positive(row.get("history_highest_market_cap")),
            liquidity_usd=_positive(row.get("liquidity")),
            volume_24h_usd=_positive(row.get("volume")) if daily else None,
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
            price_change_24h_pct=_num(row.get("price_change_percent")) if daily else None,
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
                "high_market_cap_usd": "gmgn",
                "top10_pct": "gmgn", "bundling_pct": "gmgn",
                "bot_degen_pct": "gmgn", "total_supply": "gmgn", "open_at": "gmgn",
            },
        )
        if not daily:
            # This row answered in another window, so it has no 24-hour volume to
            # attribute. Claiming one here would credit a blank reading to GMGN.
            metrics.provenance.pop("volume_24h_usd", None)
        return metrics

    # ------------------------------------------------------------- per token

    def _stat(self, chain_id: str, ca: str) -> dict:
        def produce():
            try:
                payload = json_get("%s/api/v1/token_stat/%s/%s" % (BASE, slug(chain_id), ca),
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
                payload = json_get("%s/api/v1/token_info/%s/%s" % (BASE, slug(chain_id), ca),
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

        The path is per chain (`SECURITY_PATHS`), and on Solana the fields it answers are a
        different set, which `LP_LOCK_ONLY_WHEN_TRUE` and the LP block in `fetch_token`
        handle. There is no tax on that chain, so the `buy_tax`/`sell_tax` it reports are
        its own literal zeros rather than a reading.
        """

        def produce():
            path = SECURITY_PATHS.get(str(chain_id), SECURITY_PATH)
            try:
                payload = json_get("%s/api/v1/%s/%s/%s" % (BASE, path, slug(chain_id), ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:security:%s" % ca, SECURITY_TTL, produce) or {}

    def dev_info(self, chain_id: str, ca: str) -> dict:
        """The dev's own record: how much they still hold, and the X rename history."""

        def produce():
            try:
                payload = json_get("%s/api/v1/token_dev_info/%s/%s" % (BASE, slug(chain_id), ca),
                                   UA_GMGN, transport=TRANSPORT, tries=2, sleep=2.0, timeout=25.0)
            except SourceError:
                return None
            return (payload or {}).get("data") or None

        return cached("gmgn:dev:%s" % ca, STAT_TTL, produce) or {}

    def _stat_facts(self, ca: str, stat: dict, metrics: TokenMetrics, facts: SafetyFacts) -> None:
        """`token_stat` onto the token's view: the chip numbers, and nothing else.

        Shared by the full read (`fetch_token`) and the chip-only read (`fetch_chips`), so a
        field and the key that feeds it cannot drift between the two.
        """
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
        metrics.provenance.update({
            "holders": "gmgn", "top10_pct": "gmgn", "bundling_pct": "gmgn",
            "honeypot_pct": "gmgn", "bot_degen_pct": "gmgn",
            "fresh_wallet_pct": "gmgn", "private_vault_pct": "gmgn",
            "signal_count": "gmgn",
        })

    def fetch_chips(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        """One request: the chip picture the hard filter reads, and nothing else.

        `fetch_token` asks four endpoints because a card wants the taxes, the LP sheet and
        the deployer too; the chip half of the hard filter reads three numbers, and all
        three live in `token_stat`. This host paces itself at 0.45s per request, so the
        three endpoints this does not ask are three quarters of the pass's wall clock
        (measured on 29 rows: 53.1s through `fetch_token`, 13.6s through this one).

        The `facts` it returns are empty on purpose: this read did not ask the safety
        questions, so a caller that keeps the answers has to ask `fetch_token`.
        """
        stat = self._stat(chain_id, ca) or {}
        metrics = TokenMetrics(ca=ca)
        facts = SafetyFacts()
        self._stat_facts(ca, stat, metrics, facts)
        return metrics, facts

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
        self._stat_facts(ca, stat, metrics, facts)
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

        # GMGN's own security sheet answers the LP question directly, and it is the
        # answer of record: `lock_summary.is_locked` is what the page shows. It also
        # covers the v4 shape, where there is no LP token for the chain read to look at.
        lock = security.get("lock_summary") if isinstance(security, dict) else None
        locked = _bool(lock.get("is_locked")) if isinstance(lock, dict) else None
        if locked is False and chain_id in LP_LOCK_ONLY_WHEN_TRUE:
            # Not a verdict on this chain; see that table. Left as 未判定 rather than
            # dropped, and the card says which reading it was.
            facts.lp_note = "GMGN：未见锁仓记录"
        elif locked is not None:
            facts.lp_locked = locked
            facts.lp_source = "gmgn"
            percent = _num(lock.get("lock_percent")) if isinstance(lock, dict) else None
            facts.lp_note = "GMGN：LP %s" % (
                ("已锁 %g%%" % (percent * 100)) if locked and percent
                else ("已锁" if locked else "未锁"),
            )

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
        # The stat half already claimed its own fields, in `_stat_facts`.
        metrics.provenance.update({
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
