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
