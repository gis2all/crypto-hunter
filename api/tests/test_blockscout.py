"""The full-chain base table: what it walks, and where it stops.

Blockscout is the one source here that answers "what exists" rather than "what is
trading", so its failure modes are quiet ones - a sweep that stops early looks exactly
like a shorter chain. These pin the three that matter: the floor, the cursor, and the
rule that a missing number is never a zero.
"""

from app import config
from app.sources import blockscout

CA_A = "0x" + "aa" * 20
CA_B = "0x" + "bb" * 20
CA_C = "0x" + "cc" * 20
CA_D = "0x" + "dd" * 20
CA_E = "0x" + "ee" * 20


def _row(ca, holders, mcap=None, volume=None):
    return {"address_hash": ca, "symbol": "T", "holders_count": str(holders),
            "circulating_market_cap": mcap, "volume_24h": volume,
            "exchange_rate": "0.01"}


PAGES = [
    [_row(CA_A, 900, "155944.8", "732.2"), _row(CA_B, 600)],
    [_row(CA_C, 200), _row(CA_D, 120)],
    [_row(CA_E, 10)],
]


def _chain(floor):
    return {"id": "arc", "explorer": "https://explorer.arc.io",
            "pipeline": {"universe_holders_floor": floor}}


def _serve(monkeypatch, floor=50, pages=None):
    urls = []
    source = pages if pages is not None else PAGES

    def fake(url, headers=None, **_kw):
        """One sweep, one page per call: the walk is a sequence, not a set."""
        urls.append(url)
        page = source[min(len(urls) - 1, len(source) - 1)]
        cursor = {"name": "N", "contract_address_hash": CA_A, "sort": "holders_count",
                  "order": "desc", "fiat_value": None, "market_cap": None,
                  "holders_count": 1, "items_count": 50, "is_name_null": False}
        return {"items": page, "next_page_params": cursor}

    monkeypatch.setattr(blockscout, "json_get", fake)
    monkeypatch.setattr(config, "chain", lambda _chain_id: _chain(floor))
    return urls


def test_the_sweep_ends_on_the_page_that_carries_the_first_row_under_the_floor(monkeypatch):
    """The listing is ordered by holder count, so the boundary is the end of the walk.
    One page has to be read to find it; the tail behind it is never read at all, which is
    what keeps a full-chain roster at two minutes instead of an hour."""
    urls = _serve(monkeypatch)
    rows = blockscout.Blockscout()._sweep("arc")
    assert [row["holders"] for row in rows] == [900, 600, 200, 120]
    assert len(urls) == 3, "the walk must stop on the page that crosses the floor"
    assert all(row["holders"] >= 50 for row in rows)


def test_the_floor_comes_from_the_chain_config(monkeypatch):
    _serve(monkeypatch, floor=700)
    rows = blockscout.Blockscout()._sweep("arc")
    assert [row["holders"] for row in rows] == [900]


def test_a_cursor_page_keeps_the_type_and_drops_the_ordering(monkeypatch):
    """`sort`/`order` come back in the cursor; sending them again answers with page one,
    which is how a sweep turns into the same fifty tokens forever."""
    urls = _serve(monkeypatch)
    blockscout.Blockscout()._sweep("arc")
    assert "type=ERC-20" in urls[1]
    assert "sort=" not in urls[1] and "order=" not in urls[1]


def test_a_null_cursor_field_goes_back_as_empty_and_a_boolean_as_false():
    """The endpoint parses the cursor rather than coercing it: a null is a 422 and so is
    the string `False`, which is what a raw urlencode produces."""
    got = blockscout._cursor({"name": "Argus", "sort": "holders_count", "order": "desc",
                              "contract_address_hash": CA_A, "fiat_value": None,
                              "market_cap": None, "holders_count": 9246, "items_count": 50,
                              "is_name_null": False})
    assert "sort" not in got and "order" not in got
    assert got["fiat_value"] == "" and got["market_cap"] == ""
    assert got["is_name_null"] == "false"
    assert got["holders_count"] == 9246


def test_a_zero_market_cap_is_a_missing_number_not_a_zero():
    """Every threshold in the prefilter is a floor. A zero written where the source said
    "no price" would drop the token for the wrong reason."""
    row = blockscout._metrics(CA_A, _row(CA_A, 100, mcap="0", volume="0"))
    assert row["market_cap_usd"] is None and row["volume_24h_usd"] is None
    assert row["holders"] == 100 and row["symbol"] == "T"


def test_the_universe_is_truncated_to_the_scan_limit(monkeypatch):
    rows = [blockscout._metrics(CA_A, _row(CA_A, 900)),
            blockscout._metrics(CA_B, _row(CA_B, 600)),
            blockscout._metrics(CA_C, _row(CA_C, 200))]
    monkeypatch.setattr(blockscout, "cached", lambda _key, _ttl, _produce: rows)
    source = blockscout.Blockscout()
    assert [row.symbol for row in source.fetch_universe("arc", 0)] == ["T", "T", "T"]
    assert len(source.fetch_universe("arc", 2)) == 2


def test_the_cached_sweep_is_a_plain_dict_list(monkeypatch):
    """The cache writes JSON. A pydantic object in there would not raise - the write is
    guarded - so the roster would quietly stop being remembered."""
    written = []
    monkeypatch.setattr(blockscout, "cached",
                        lambda _key, _ttl, produce: written.append(produce()) or None)
    _serve(monkeypatch)
    blockscout.Blockscout().fetch_universe("arc", 0)
    import json
    assert json.dumps(written[0])


def test_an_address_the_explorer_cannot_parse_is_never_asked_about(monkeypatch):
    """The detail endpoint answers a malformed address with a 422; the round is not the
    place to find that out one request at a time."""

    def boom(*_a, **_kw):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(blockscout, "json_get", boom)
    metrics, facts = blockscout.Blockscout().fetch_token("arc", "0x0bb3befb")
    assert metrics.ca == "0x0bb3befb" and metrics.symbol is None
    assert facts.source_verified is None


def test_a_detail_answer_maps_the_same_fields_as_the_listing(monkeypatch):
    monkeypatch.setattr(config, "chain", lambda _chain_id: _chain(50))
    monkeypatch.setattr(blockscout, "json_get", lambda *_a, **_kw: _row(CA_A, 1740, "128489.5", "900.1"))
    metrics, _facts = blockscout.Blockscout().fetch_token("arc", CA_A)
    assert metrics.symbol == "T" and metrics.holders == 1740
    assert metrics.market_cap_usd == 128489.5 and metrics.volume_24h_usd == 900.1
    assert metrics.provenance["holders"] == "blockscout"
