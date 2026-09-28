"""GMGN's tax sheet - the only endpoint on Arc that answers the 税 row.

The ranking row carries `buy_tax`, but it is a hard "0" for every Arc token (measured
2026-09-25), which is why the tax fields are read from `token_security_evm` alone.
"""

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
                                        "history_highest_market_cap": "5048753"})
    assert metrics.high_market_cap_usd == 5_048_753
    assert metrics.provenance["high_market_cap_usd"] == "gmgn"


def test_a_row_with_no_high_answers_nothing():
    """A missing or zero high is None, not 0: the rule must not read "no data" as "the token
    never went anywhere" and pass it."""
    from app.sources.gmgn import GmGn

    source = GmGn()
    assert source._metrics("0xabc", {"market_cap": "113800"}).high_market_cap_usd is None
    assert source._metrics("0xabc",
                           {"history_highest_market_cap": "0"}).high_market_cap_usd is None
