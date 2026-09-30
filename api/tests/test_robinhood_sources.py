"""The Robinhood launchpad feeds: the columns that need reading before they are used.

Each of these three ships a number that is not the number it looks like, and every one of
them was measured before it was coded:

- CoinBarrel leaves `market_cap_usd` unset on every row (0/50) and publishes the market cap
  in `fdv_usd`; its `liquidity_primary_usd` and `volume_usd_24h` are partly filled and have
  an exact fallback in the native column times the row's own `quote_usd_rate`.
- PEZ prices its volume in the pool's *quote asset*, which on this chain is not a stablecoin,
  and publishes `priceChangePct` as a fraction rather than a percent.
- Peeps rounds `change` to a whole percent and puts the launchpad in `venueId`.

The rows below are real captured answers, trimmed. Nothing here reaches the network.
"""

import pytest

from app.sources.robinhood import coinbarrel, peeps, pez

CB_ROW = {
    "mint": "0xA90bDf0F4F72F3f58bc262Ab8DF7fE28e1959813",
    "symbol": "ONE",
    "market_cap_usd": None,
    "fdv_usd": 4334.027105011464,
    "liquidity_primary_usd": None,
    "pool_total_liquidity_native": 1.6232870302817293,
    "quote_usd_rate": 2674.982892406839,
    "volume_usd_24h": None,
    "volume_sol_24h": 0.19418911865757338,
    "holder_count": 0,
    "peak_fdv_usd": 9999.503953685717,
    "created_at": 1784225506,
    "price_usd": 4.334027105011464e-06,
    "price_change_24h": 0.0,
    "decimals": 18,
    "total_supply": 1000000000000000000000000000,
    "evm_deployer_address": "0x68aD419DB7ee5D4748C484005c48125eA3FF139e",
}

PEZ_ROW = {
    "token": "0x1f954365dd9e0eecafc04cc0b3488ac24637bb2d",
    "symbol": "MEMEBASKET",
    "marketCapUsd": 4285.661120967209,
    "fdvUsd": 4285.661120967209,
    "liquidityUsd": None,
    "holders": 1,
    "priceUsd": 4.285661120967209e-06,
    "priceQuote": 2.888187954967915e-08,
    "volume": {"h1": 0, "h24": 0.2614060027882562, "d7": 0.3953149757893229},
    "priceChangePct": {"h1": 0, "h24": 0.0003467816258886315, "d7": 0.006508136008134751},
    "launchedAt": "2026-09-14T16:56:15.000Z",
    "socials": {"discord": "", "twitter": "https://x.com/otc_labs/status/20", "website": ""},
    "deployer": "0x38627817a3d081f6816a8734e313671d203892b7",
}

PEEPS_ROW = {
    "contractAddress": "0xf202de51bb42a0073948b0971707d14c54ef5f44",
    "ticker": "PEEPS",
    "marketCap": 289047,
    "athMarketCap": 367482,
    "liquidityUsd": 50285,
    "volumeUsd24h": 494,
    "holders": 347,
    "change": -2,
    "createdAt": "2026-07-17T11:34:23.000Z",
    "creatorAddress": "0x753a02f555e6579c347b34a7d93a30f64e0783c7",
    "venueId": "peeps",
}


def test_each_one_only_claims_the_chain_it_indexes():
    for source in (coinbarrel.CoinBarrel(), pez.Pez(), peeps.Peeps()):
        assert source.chains == ("robinhood",)


def test_coinbarrel_reads_the_market_cap_out_of_the_fdv_column():
    """`market_cap_usd` is unset on 50 of 50 rows. Reading it alone leaves the card blank,
    and a blank market cap makes 空壳淘汰 drop the row as if nothing had answered."""
    metrics = coinbarrel.CoinBarrel()._metrics("0xaa", CB_ROW)
    assert metrics.market_cap_usd == pytest.approx(4334.027105011464)


def test_coinbarrel_converts_the_native_columns_with_the_rows_own_rate():
    metrics = coinbarrel.CoinBarrel()._metrics("0xaa", CB_ROW)
    assert metrics.liquidity_usd == pytest.approx(1.6232870302817293 * 2674.982892406839)
    assert metrics.volume_24h_usd == pytest.approx(0.19418911865757338 * 2674.982892406839)


def test_coinbarrel_does_not_read_a_zero_holder_count_as_a_measurement():
    """A token that launched a minute ago has 0 holders. That is not a reading, and taking it
    as one fails the row on the holder floor for being new rather than for being bad."""
    metrics = coinbarrel.CoinBarrel()._metrics("0xaa", CB_ROW)
    assert metrics.holders is None
    assert coinbarrel.CoinBarrel()._metrics("0xaa", {**CB_ROW, "holder_count": 144}).holders == 144


def test_coinbarrel_keeps_a_fall():
    """`price_change_24h` is a signed percent: -28.72 on a real graduated row. The
    positive-only reader the other adapters share would answer None for every fall."""
    row = {**CB_ROW, "price_change_24h": -28.720474909362004}
    assert coinbarrel.CoinBarrel()._metrics("0xaa", row).price_change_24h_pct == -28.720474909362004


def test_coinbarrel_publishes_the_token_high_in_token_units():
    metrics = coinbarrel.CoinBarrel()._metrics("0xaa", CB_ROW)
    assert metrics.high_market_cap_usd == pytest.approx(9999.503953685717)
    assert metrics.total_supply == pytest.approx(1_000_000_000)
    assert metrics.creator == "0x68aD419DB7ee5D4748C484005c48125eA3FF139e"


def test_pez_converts_a_quote_denominated_volume_into_dollars():
    """The quote asset on this chain is not a stablecoin - one row's is a tokenised share -
    so the raw `volume` is not dollars and printing it as if it were would be a wrong number
    where a blank belongs."""
    metrics = pez.Pez()._metrics("0xbb", PEZ_ROW)
    assert metrics.volume_24h_usd == pytest.approx(38.8, rel=0.01)


def test_pez_gives_no_volume_when_the_row_cannot_be_converted():
    row = {**PEZ_ROW, "priceQuote": None}
    assert pez.Pez()._metrics("0xbb", row).volume_24h_usd is None


def test_pez_turns_the_fraction_into_a_percent():
    metrics = pez.Pez()._metrics("0xbb", PEZ_ROW)
    assert metrics.price_change_24h_pct == pytest.approx(0.03467816258886315)


def test_pez_reads_the_handle_out_of_the_socials_map():
    metrics = pez.Pez()._metrics("0xbb", PEZ_ROW)
    assert metrics.socials == {"twitter": "https://x.com/otc_labs/status/20"}
    assert metrics.websites == []
    assert metrics.created_at.year == 2026


def test_peeps_publishes_the_token_high_and_a_signed_change():
    metrics = peeps.Peeps()._metrics("0xcc", PEEPS_ROW)
    assert metrics.market_cap_usd == pytest.approx(289047)
    assert metrics.high_market_cap_usd == pytest.approx(367482)
    assert metrics.price_change_24h_pct == pytest.approx(-2.0)
    assert metrics.launchpad == "peeps"
    assert metrics.holders == 347
