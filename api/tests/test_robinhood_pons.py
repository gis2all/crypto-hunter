"""PONS: the fourth Robinhood feed, and the one that sweeps graduated launches only.

Two columns need reading before they are used, both measured on 2026-10-11:

- `volumeUsd` is the curve volume, not the day volume, once a token graduates. Three
  graduated rows carried 0 there where their DexScreener pools had traded 61,856 / 62,759 /
  21,138 over 24h, so the column stops moving at migration and a row that has not graduated
  is asked for no volume at all.
- `artwork` arrives as `ipfs://<cid>`. The public gateways refuse this client (ipfs.io 403,
  dweb.link 429) while the route the site itself builds answers 200 image/webp, so that is
  the URL the card gets.

The row below is a real captured answer, trimmed. Nothing here reaches the network.
"""

import pytest

from app.sources.robinhood import pons

PONS_ROW = {
    "address": "0x867d3f36fc4a35541675e8a147fa879c1804b8e1",
    "name": "flowroute",
    "symbol": "FLW",
    "protocol": "v2",
    "stage": "graduated",
    "artwork": "ipfs://bafybeid5hbl35x4q2m6ets63fc3gvrtqqlijgvf3vjjg3rqln3vftbt36a",
    "deployer": "0x24eef00ee09bef1fafd16975bc5dd3e2dd9b0252",
    "createdAt": 1791658875,
    "raisedUsd": 0,
    "priceUsd": 4.632974397785709e-05,
    "marketCapUsd": 45210.880660791845,
    "volumeUsd": 5902.33,
    "tradeCount": 1381,
    "socials": {
        "twitter": "https://x.com/flowrouteapp",
        "telegram": "https://t.me/FlowrouteRadarBot",
        "website": "https://www.flowroute.cloud",
        "discord": None,
        "farcaster": None,
    },
    "launchedBlock": 85227641,
    "decimals": 18,
    "totalSupply": 1000000000,
    "quoteUsd": 2504.7500408773567,
}

CID = "bafybeid5hbl35x4q2m6ets63fc3gvrtqqlijgvf3vjjg3rqln3vftbt36a"


def test_this_feed_only_claims_the_chain_it_indexes():
    assert pons.Pons().chains == ("robinhood",)


def test_the_launchpad_is_named_so_the_card_can_print_it():
    metrics = pons.Pons()._metrics("0xaa", PONS_ROW)
    assert metrics.launchpad == "pons"
    assert metrics.symbol == "FLW"
    assert metrics.market_cap_usd == pytest.approx(45210.880660791845)
    assert metrics.creator == "0x24eef00ee09bef1fafd16975bc5dd3e2dd9b0252"


def test_the_volume_of_a_graduated_row_is_read():
    metrics = pons.Pons()._metrics("0xaa", PONS_ROW)
    assert metrics.volume_24h_usd == pytest.approx(5902.33)


def test_a_curve_row_is_asked_for_no_volume():
    """On a curve row the same column is the curve total, and a launch with no pool cannot be
    ranked beside one that has a pool."""
    row = {**PONS_ROW, "stage": "curve"}
    assert pons.Pons()._metrics("0xaa", row).volume_24h_usd is None


def test_the_artwork_is_resolved_through_the_route_the_site_builds():
    metrics = pons.Pons()._metrics("0xaa", PONS_ROW)
    assert metrics.logo_url == (
        "https://dbk-vercel.vercel.app/api/ipfs/content/%s?variant=card" % CID
    )


def test_an_artwork_that_is_not_ipfs_is_left_as_a_link_or_dropped():
    https = {**PONS_ROW, "artwork": "https://cdn.example/x.png"}
    assert pons.Pons()._metrics("0xaa", https).logo_url == "https://cdn.example/x.png"
    bare = {**PONS_ROW, "artwork": "bafybei"}
    assert pons.Pons()._metrics("0xaa", bare).logo_url is None


def test_the_launch_time_is_read_as_unix_seconds():
    metrics = pons.Pons()._metrics("0xaa", PONS_ROW)
    assert metrics.created_at.timestamp() == pytest.approx(1791658875.0)


def test_a_created_at_that_will_not_read_is_a_gap_not_the_epoch():
    for bad in (None, "soon", 0):
        metrics = pons.Pons()._metrics("0xaa", {**PONS_ROW, "createdAt": bad})
        assert metrics.created_at is None, bad


def test_the_handles_and_the_site_are_read_out_of_the_socials_map():
    metrics = pons.Pons()._metrics("0xaa", PONS_ROW)
    assert metrics.socials == {
        "twitter": "https://x.com/flowrouteapp",
        "telegram": "https://t.me/FlowrouteRadarBot",
    }
    assert metrics.websites == ["https://www.flowroute.cloud"]