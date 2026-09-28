"""The chain config is the source of the口径; mistakes here poison everything."""

import os

from app import config


def test_reloading_one_env_file_keeps_what_the_other_put_in(tmp_path):
    """The LLM file is re-read on every model call; the search file is read once, at
    import. While the record of what came from a file was one flat dict, that reload
    withdrew the search file's credentials too - so from the first model call of every
    process, every X search and every timeline failed "needs CH_X_AUTH_TOKEN" and the round
    looked like it had simply found nothing."""
    first = tmp_path / "llm.env"
    second = tmp_path / "search.env"
    first.write_text("CH_TEST_LLM_KEY=1\n", encoding="utf-8")
    second.write_text("CH_TEST_SEARCH_KEY=2\n", encoding="utf-8")
    os.environ.pop("CH_TEST_LLM_KEY", None)
    os.environ.pop("CH_TEST_SEARCH_KEY", None)
    try:
        assert config.load_env_file(first) == 1
        assert config.load_env_file(second) == 1
        assert config.load_env_file(first) == 1
        assert os.environ["CH_TEST_SEARCH_KEY"] == "2"
        assert os.environ["CH_TEST_LLM_KEY"] == "1"
    finally:
        for path in (first, second):
            config._FROM_FILE.pop(path, None)
        os.environ.pop("CH_TEST_LLM_KEY", None)
        os.environ.pop("CH_TEST_SEARCH_KEY", None)


def test_both_chains_declared():
    chains = config.load_chains()
    assert set(chains) == {"arc", "bsc"}
    assert chains["arc"]["enabled"] is True
    assert chains["bsc"]["enabled"] is False


def test_arc_keeps_the_established_cadence():
    arc = config.chain("arc")
    assert arc["schedule"]["cron"] == "0 2,5,8,11,14,17,20,23 * * *"
    assert arc["schedule"]["push_delay_minutes"] == 6
    assert arc["report"]["per_round"] == 5


def test_the_universe_sweep_stops_below_the_holder_floor():
    """blockscout is the full-chain roster: the sweep floor has to stay under the rule it
    is there to serve, or the base table would silently exclude tokens the rule lets in."""
    pipeline = config.chain("arc")["pipeline"]
    assert pipeline["universe_holders_floor"] <= pipeline["prefilter"]["holders_min"]


def test_arc_prefilter_matches_the_ui():
    pre = config.chain("arc")["pipeline"]["prefilter"]
    assert pre["market_cap_min_usd"] == 10000
    assert pre["market_cap_max_usd"] == 200000
    assert pre["liquidity_min_usd"] == 5000
    assert pre["volume_24h_min_usd"] == 1000
    assert pre["holders_min"] == 100


def test_the_holder_band_ships_with_an_open_ceiling():
    """The ceiling ships unset: an absent key means the rule is off, so the key has to
    exist in the YAML for the settings page to be able to set one at all."""
    for chain_id in ("arc", "bsc"):
        pre = config.chain(chain_id)["pipeline"]["prefilter"]
        assert "holders_max" in pre
        assert pre["holders_max"] is None
        assert pre["holders_min"] is not None


def test_scoring_weights_sum_to_100():
    """Each block's five factors split a 100 that is theirs, not a share of the block
    weight: the on-chain factors sum to 100 and so do the five off-chain judgements."""
    for chain_id in ("arc", "bsc"):
        scoring = config.chain(chain_id)["pipeline"]["scoring"]
        assert scoring["onchain_weight"] + scoring["offchain_weight"] == 100
        assert len(scoring["onchain_factors"]) == 5
        assert sum(scoring["onchain_factor_weights"].values()) == 100
        assert scoring["offchain_factor_weights"] == {
            "f_completeness": 20, "f_substance": 20, "f_team": 20,
            "f_narrative": 20, "f_community": 20,
        }
