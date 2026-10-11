"""GMGN's tax sheet - the only endpoint on Arc that answers the 税 row.

The ranking row carries `buy_tax`, but it is a hard "0" for every Arc token (measured
2026-09-25), which is why the tax fields are read from `token_security_evm` alone.
"""

import pytest

from app.sources.gmgn import _tax_pct


def test_the_total_is_the_number_the_card_prints():
    """`total_*` is the base tax plus the transfer tax a global-tax token adds. It is
    what GMGN's own page shows, and the plain `buy_tax`/`sell_tax` beside it read "0"
    on exactly these tokens."""
    row = {"buy_tax": "0", "sell_tax": "0",
           "total_buy_tax": "0.03", "total_sell_tax": "0.02"}
    assert _tax_pct(row, "total_buy_tax", "buy_tax") == 3.0
    assert _tax_pct(row, "total_sell_tax", "sell_tax") == 2.0


def test_the_base_tax_stands_in_when_the_total_is_absent():
    assert _tax_pct({"buy_tax": "0.05"}, "total_buy_tax", "buy_tax") == 5.0


def test_a_token_with_no_sheet_answers_nothing():
    """No sheet is "—", not 0%: the two must not look alike on the card."""
    assert _tax_pct({}, "total_buy_tax", "buy_tax") is None


def test_an_untaxed_token_still_reports_a_measured_zero():
    assert _tax_pct({"total_buy_tax": "0"}, "total_buy_tax", "buy_tax") == 0.0


def test_the_chip_read_asks_one_endpoint_and_answers_the_three_numbers(monkeypatch):
    """The top-up pass is paced per GMGN request, so it buys only the endpoint the hard
    filter reads: one stat payload, mapped by the same code the full read uses."""
    from app.sources import gmgn

    calls: list[str] = []

    def fake_json_get(url, *_args, **_kwargs):
        calls.append(url)
        if "/token_stat/" in url:
            return {"data": {
                "holder_count": 1500,
                "top_10_holder_rate": 0.222,
                "top_bundler_trader_percentage": 0.031,
            }}
        raise AssertionError("the chip read asked for %s" % url)

    monkeypatch.setattr(gmgn, "json_get", fake_json_get)
    monkeypatch.setattr(gmgn, "cached", lambda _key, _ttl, produce: produce())

    metrics, facts = gmgn.GmGn().fetch_chips("arc", "0xabc")

    assert len(calls) == 1
    assert metrics.holders == 1500
    assert metrics.top10_pct == 22.2
    assert metrics.bundling_pct == 3.1
    assert metrics.provenance["holders"] == "gmgn"
    assert facts.lp_locked is None, "the chip read did not ask the safety questions"


def test_the_full_read_asks_four_endpoints_and_the_chip_read_asks_its_numbers(monkeypatch):
    """Same mapping, two widths: the full read still asks everything a card needs, so the
    two readings cannot drift apart on the fields they share."""
    from app.sources import gmgn

    urls: list[str] = []

    def fake_json_get(url, *_args, **_kwargs):
        urls.append(url)
        return {"data": {
            "holder_count": 1500,
            "top_10_holder_rate": 0.222,
            "top_bundler_trader_percentage": 0.031,
            "total_buy_tax": 0.04,
            "is_honeypot": 0,
            "lock_summary": {"is_locked": True, "lock_percent": 1},
            "creator_address": "0xdead",
        }}

    monkeypatch.setattr(gmgn, "json_get", fake_json_get)
    monkeypatch.setattr(gmgn, "cached", lambda _key, _ttl, produce: produce())

    metrics, facts = gmgn.GmGn().fetch_token("arc", "0xabc")

    assert len(urls) == 4
    assert metrics.holders == 1500
    assert metrics.buy_tax_pct == 4.0
    assert facts.lp_locked is True and facts.lp_source == "gmgn"
    assert metrics.creator == "0xdead"


def test_the_ranking_row_carries_the_high_gmgn_measured():
    """`history_highest_market_cap` is the one reading of a token own high that no other
    source on Arc sells, and the ranking row already carries it - so the bulk pull gets it
    for free. 距高回撤 reads this first (`filters.drawdown_readings`): it is a
    measurement, not one of our samples."""
    from app.sources.gmgn import GmGn

    metrics = GmGn()._metrics("0xabc", {"symbol": "ABC", "market_cap": "113800",
                                        "history_highest_market_cap": "5048753"}, "24h")
    assert metrics.high_market_cap_usd == 5_048_753
    assert metrics.provenance["high_market_cap_usd"] == "gmgn"


def test_a_row_with_no_high_answers_nothing():
    """A missing or zero high is None, not 0: the rule must not read "no data" as "the token
    never went anywhere" and pass it."""
    from app.sources.gmgn import GmGn

    source = GmGn()
    assert source._metrics("0xabc", {"market_cap": "113800"}, "24h").high_market_cap_usd is None
    assert source._metrics("0xabc", {"history_highest_market_cap": "0"},
                           "24h").high_market_cap_usd is None


def test_a_short_window_row_answers_no_24_hour_volume():
    """`volume` and `price_change_percent` are the ranking window's totals, not the day's.

    One Arc token read 47.58 on the 5m board, 59.56 on the 1h board and 9,386.81 on the 6h
    board where the real 24h figure was 14,549 (measured 2026-09-29, two independent feeds
    agreeing). Stored as a 24h number that reading put a $14.5K-a-day token at $6 and the
    volume floor dropped it - so the adapter leaves both fields blank off a short window,
    and 未判定 never rejects. The spot numbers on the same row are not window-scoped and
    still answer."""
    from app.sources.gmgn import GmGn

    row = {"market_cap": "178794", "liquidity": "37153.4", "holder_count": 646,
           "volume": "5.75836", "price_change_percent": "-0.319918"}
    source = GmGn()

    daily = source._metrics("0xabc", row, "24h")
    assert daily.volume_24h_usd == 5.75836
    assert daily.price_change_24h_pct == -0.319918
    assert daily.provenance["volume_24h_usd"] == "gmgn"

    hourly = source._metrics("0xabc", row, "1h")
    assert hourly.volume_24h_usd is None
    assert hourly.price_change_24h_pct is None
    assert "volume_24h_usd" not in hourly.provenance
    assert hourly.market_cap_usd == 178794 and hourly.liquidity_usd == 37153.4
    assert hourly.holders == 646


def test_rank_rows_hands_on_the_window_that_answered(monkeypatch):
    """The window is the only thing that says what `volume` means, so it travels with the
    row; the 24h board wins whenever the token is on it. It rides inside the row because
    the answer is cached through JSON, which would hand a pair back as a list."""
    from app.sources import gmgn

    boards = {"1m": [], "5m": [{"address": "0xAA", "volume": "5"}],
              "1h": [{"address": "0xaa", "volume": "59"}], "6h": [],
              "24h": [{"address": "0xaa", "volume": "9386"}]}
    source = gmgn.GmGn()
    monkeypatch.setattr(gmgn, "cached", lambda _key, _ttl, produce: produce())
    monkeypatch.setattr(source, "_rank_page",
                        lambda chain_id, period, page: boards[period] if page == 1 else [])

    rows = source.rank_rows("arc")
    assert list(rows) == ["0xaa"]
    assert rows["0xaa"]["_window"] == "24h"
    assert rows["0xaa"]["volume"] == "9386"


def test_a_board_that_never_answered_is_a_failure_not_an_empty_chain(monkeypatch):
    """The rank is the only place a token no launchpad adapter knows is found at all.

    A rate limit answered as `[]` left the source reporting `ok=True, n=0` - the same report
    an empty chain gives - and the round ran on to the end on an empty universe. Every board
    unreadable raises instead, so `collect` files it as `ok=False` with the reason. A board
    that answered and was empty is still an empty chain, and neither answer is remembered:
    `cached` keeps no exception and no empty dict.
    """
    from app.sources import gmgn
    from app.sources.http import SourceError

    source = gmgn.GmGn()
    monkeypatch.setattr(gmgn, "cached", lambda _key, _ttl, produce: produce())

    def dead(chain_id, period, page):
        raise SourceError("HTTP 429")

    monkeypatch.setattr(source, "_rank_page", dead)
    with pytest.raises(SourceError):
        source.rank_rows("solana")

    monkeypatch.setattr(source, "_rank_page", lambda chain_id, period, page: [])
    assert source.rank_rows("solana") == {}


def test_the_solana_sheet_only_counts_a_lock_when_it_is_true(monkeypatch):
    """`token_security_sol` is the same panel under a different path, and on Solana its
    `is_locked` is not the same claim the EVM sheet makes.

    Measured 2026-10-11 over ten Solana tokens: `lock_percent` "0" and `lock_tags` null every
    time, and `false` for BONK and WIF, which nobody would call unlocked. A `false` is a hard
    drop (`core/filters.py`), so this chain keeps the positive and files the negative as
    未判定, with the reading printed on the card.
    """
    from app.sources import gmgn

    urls: list[str] = []
    sheet = {"data": {"lock_summary": {"is_locked": False, "lock_percent": "0"}}}

    def fake_json_get(url, *_args, **_kwargs):
        urls.append(url)
        return sheet if "/token_security_sol/" in url else {"data": {}}

    monkeypatch.setattr(gmgn, "json_get", fake_json_get)
    monkeypatch.setattr(gmgn, "cached", lambda _key, _ttl, produce: produce())
    ca = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

    _metrics, facts = gmgn.GmGn().fetch_token("solana", ca)
    assert any("token_security_sol" in url for url in urls), "the EVM path answers nothing here"
    assert facts.lp_locked is None and facts.lp_source is None
    assert "未见锁仓记录" in (facts.lp_note or "")

    sheet["data"] = {"lock_summary": {"is_locked": True, "lock_percent": "0"}}
    _metrics, facts = gmgn.GmGn().fetch_token("solana", ca)
    assert facts.lp_locked is True and facts.lp_source == "gmgn"
    assert facts.lp_note == "GMGN：LP 已锁", "no percentage is reported on this chain"
