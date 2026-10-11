"""The Solana launchpad feeds: two columns that are not the column they look like.

- Raydium LaunchLab (`launch-mint-v1.raydium.io/get/list`) is the launchpad behind bonk.fun,
  which is one `platformId` of the program rather than a source of its own: asking without a
  `platformId` returns every platform at once, and each row names its own platform in
  `platformInfo.name` (the captured row below is `letsbonk.fun`). Its `marketCap` is dollars
  - measured equal to the DexScreener figure to the dollar - while `volumeU` is the curve
  total and stops moving once a token migrates, so it is not read.
- pump.fun (`frontend-api-v3.pump.fun/coins`) publishes two market-cap columns. `market_cap`
  is denominated in SOL and `usd_market_cap` is dollars: the captured row reads 98.40 against
  10,831.74, a ratio of 110 that is the price of SOL. Reading the SOL column would put a
  token two orders of magnitude below where it trades. `ath_market_cap` is dollars, measured
  by ratio against `usd_market_cap` on tokens minutes old. There is no volume column at all,
  so the 24h floor stays unread and never rejects.

Both rows below are real captured answers, trimmed. Nothing here reaches the network.
"""

import pytest

from app.sources.solana import launchlab, pumpfun

LAUNCHLAB_ROW = {
    "mint": "CvZgbVQc8NS1cLVteuxgssyamtUecEqbCjobUxerbonk",
    "poolId": "CRKw3LM3EBXYn2DMqH7FWgmB4cPLcwspMEjb83qr6PPs",
    "creator": "FxeUPJLkjcQBy5RkaE2MrpjiB2EXQAiwSkzY6As8HSNj",
    "createAt": 1791668400000,
    "name": "1b",
    "symbol": "1b",
    "description": "",
    "imgUrl": "https://metadata.j7tracker.io/images/74GmxRq0uj",
    "twitter": "",
    "platformInfo": {"name": "letsbonk.fun", "web": "https://letsbonk.fun/"},
    "decimals": 6,
    "supply": 1000000000,
    "marketCap": 10315.031203800436,
    "volumeA": 1707054034.4582412,
    "volumeU": 12.5,
}

PUMP_ROW = {
    "mint": "FMLwPQkZ1j2hX9BAE9KRLnZcGmU1DtiHJbiBf93Tpump",
    "symbol": "alon",
    "name": "this user is targeting you",
    "creator": "63EbUyj9a4K86NimRCdpFzobzDUKjAZHa18raKvyqdsA",
    "created_timestamp": 1741584542885,
    "usd_market_cap": 10831.73692602,
    "market_cap": 98.40323804062,
    "ath_market_cap": 12896.155971911596,
    "complete": True,
    "image_uri": "https://ipfs.io/ipfs/Qmen5pQCuvcXMD4nWik4g6HDPXEXjyUALpSHPNLsgUmR3B",
    "twitter": "https://x.com/a1lon9",
    "website": None,
    "pool_address": "ANg4LLGnUXdqHYFa6dJZWZePiN8soLXicKfaSfwq5mSz",
    "protocol": "pump",
    "total_supply": 997397507880248,
}


def test_each_feed_only_claims_the_chain_it_indexes():
    assert launchlab.LaunchLab().chains == ("solana",)
    assert pumpfun.PumpFun().chains == ("solana",)


def test_the_launchpad_names_itself_and_a_row_without_one_still_gets_a_name():
    assert launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW).launchpad == "letsbonk.fun"
    row = {**LAUNCHLAB_ROW, "platformInfo": None}
    assert launchlab.LaunchLab()._metrics("mint", row).launchpad == "launchlab"


def test_the_launchlab_market_cap_is_the_dollar_column():
    metrics = launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW)
    assert metrics.market_cap_usd == pytest.approx(10315.031203800436)


def test_the_launchlab_curve_volume_is_not_read_as_a_day_trade():
    """`volumeU` is the curve total and stops moving at migration: on 2026-10-11 the same
    tokens carried 0 there where their pools had traded 62,759 / 21,138 / 9,331 over 24h.
    Handing the volume floor a number from the wrong window is the mistake the field table
    exists to prevent, so this adapter answers no volume rather than that one."""
    metrics = launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW)
    assert metrics.volume_24h_usd is None
    assert "volume_24h_usd" not in metrics.provenance


def test_the_launchlab_time_is_read_as_milliseconds():
    metrics = launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW)
    assert metrics.created_at.timestamp() == pytest.approx(1791668400.0)
    bad = launchlab.LaunchLab()._metrics("mint", {**LAUNCHLAB_ROW, "createAt": None})
    assert bad.created_at is None


def test_the_launchlab_handle_is_read_and_an_empty_one_is_a_gap():
    assert launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW).socials == {}
    row = {**LAUNCHLAB_ROW, "twitter": "https://x.com/foo"}
    assert launchlab.LaunchLab()._metrics("mint", row).socials == {"twitter": "https://x.com/foo"}


def test_the_launchlab_artwork_is_already_an_absolute_url():
    metrics = launchlab.LaunchLab()._metrics("mint", LAUNCHLAB_ROW)
    assert metrics.logo_url == "https://metadata.j7tracker.io/images/74GmxRq0uj"
    bare = launchlab.LaunchLab()._metrics("mint", {**LAUNCHLAB_ROW, "imgUrl": "Qm123"})
    assert bare.logo_url is None


def test_the_pump_feed_reads_the_dollar_cap_and_never_the_sol_one():
    metrics = pumpfun.PumpFun()._metrics("mint", PUMP_ROW)
    assert metrics.market_cap_usd == pytest.approx(10831.73692602)


def test_the_pump_feed_publishes_the_token_high_in_dollars():
    metrics = pumpfun.PumpFun()._metrics("mint", PUMP_ROW)
    assert metrics.high_market_cap_usd == pytest.approx(12896.155971911596)


def test_the_pump_feed_answers_no_volume_because_it_has_none():
    metrics = pumpfun.PumpFun()._metrics("mint", PUMP_ROW)
    assert metrics.volume_24h_usd is None
    assert metrics.liquidity_usd is None
    assert "volume_24h_usd" not in metrics.provenance


def test_the_pump_time_is_read_as_milliseconds_and_a_bad_one_is_a_gap():
    metrics = pumpfun.PumpFun()._metrics("mint", PUMP_ROW)
    assert metrics.created_at.timestamp() == pytest.approx(1741584542.885)
    bad = pumpfun.PumpFun()._metrics("mint", {**PUMP_ROW, "created_timestamp": None})
    assert bad.created_at is None


def test_the_pump_artwork_is_already_an_absolute_url():
    metrics = pumpfun.PumpFun()._metrics("mint", PUMP_ROW)
    assert metrics.logo_url == "https://ipfs.io/ipfs/Qmen5pQCuvcXMD4nWik4g6HDPXEXjyUALpSHPNLsgUmR3B"
    bare = pumpfun.PumpFun()._metrics("mint", {**PUMP_ROW, "image_uri": "Qm123"})
    assert bare.logo_url is None