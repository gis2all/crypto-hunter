"""The chain config is the source of the口径; mistakes here poison everything."""

import os

from app import config


def test_the_file_is_the_install_step(tmp_path, monkeypatch):
    """Read, re-read, edit and delete `system.yaml`, which is the whole install step.

    Called on every model call rather than once at import: an edit lands on the next round, a
    deleted value is withdrawn from the environment, and a key that never went in cannot linger.
    """
    path = tmp_path / "system.yaml"
    monkeypatch.setattr(config, "SYSTEM_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    # Importing this module already applied the real file, so every key this test writes has to
    # start out unset - otherwise "the shell owns it" is what the loader would be answering.
    for name in ("CH_LLM_MODEL", "CH_LLM_TIMEOUT", "CH_LLM_EXTRA_HEADERS", "CH_LLM_REASONING"):
        monkeypatch.delenv(name, raising=False)

    path.write_text("llm:\n  model: from-file\n", encoding="utf-8")
    assert config.load_env_file() == 1
    assert os.environ["CH_LLM_MODEL"] == "from-file"

    path.write_text("llm:\n  model: null\n", encoding="utf-8")
    assert config.load_env_file() == 0
    assert "CH_LLM_MODEL" not in os.environ

    # The file's own values are read as the file wrote them: an integer stays one, a JSON
    # object arrives as the string the client parses, and an empty box is not a value at all.
    path.write_text(
        "llm:\n  timeout: 300\n  extra_headers:\n    a: b\n  reasoning:\n",
        encoding="utf-8",
    )
    assert config.load_env_file() == 2
    assert os.environ["CH_LLM_TIMEOUT"] == "300"
    assert os.environ["CH_LLM_EXTRA_HEADERS"] == '{"a": "b"}'
    assert "CH_LLM_REASONING" not in os.environ


def test_a_real_environment_variable_is_never_touched(tmp_path, monkeypatch):
    """A variable the shell exported beats the file, including after a re-read.

    The re-read is on every model call, so the record of what the file owns is what keeps that
    reload from withdrawing a variable it never set - the bug that once deleted the search
    file's credentials on the first call of every process, and left every X search after it
    failing "needs CH_X_AUTH_TOKEN" in a round that looked like it had found nothing.
    """
    path = tmp_path / "system.yaml"
    monkeypatch.setattr(config, "SYSTEM_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    monkeypatch.setenv("CH_LLM_MODEL", "from-shell")

    path.write_text("llm:\n  model: from-file\n", encoding="utf-8")
    assert config.load_env_file() == 0
    assert os.environ["CH_LLM_MODEL"] == "from-shell"

    # And a file that stops setting a key does not reach for the shell's version of it.
    path.write_text("llm:\n  model: null\n", encoding="utf-8")
    assert config.load_env_file() == 0
    assert os.environ["CH_LLM_MODEL"] == "from-shell"


def test_every_chain_declared():
    chains = config.load_chains()
    assert set(chains) == {"arc", "bsc", "robinhood"}
    assert chains["arc"]["enabled"] is True
    assert chains["bsc"]["enabled"] is False
    assert chains["robinhood"]["enabled"] is True


def test_robinhood_scans_its_own_sources():
    """The three launchpad feeds only exist for Robinhood, and the two Arc-only sources must
    not be in the list: RadarDEX and Tolly answer about Robinhood with the Arc rollout, so a
    config that named them would fill the pool with another chain's tokens."""
    chain = config.chain("robinhood")
    assert set(chain["pipeline"]["scan_sources"]) == {
        "gmgn", "geckoterminal", "coinbarrel", "pez", "peeps",
    }
    for arc_only in ("radardex", "tolly"):
        assert arc_only not in chain["sources"]


def test_arc_keeps_the_established_cadence():
    arc = config.chain("arc")
    assert arc["schedule"]["cron"] == "0 2,5,8,11,14,17,20,23 * * *"
    assert arc["schedule"]["push_delay_minutes"] == 6
    assert arc["report"]["per_round"] == 5


def test_the_universe_sweep_stops_below_the_holder_floor():
    """blockscout is the full-chain roster: the sweep floor has to stay under the rule it
    is there to serve, or the base table would silently exclude tokens the rule lets in."""
    for chain_id in ("arc", "robinhood"):
        pipeline = config.chain(chain_id)["pipeline"]
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


def test_prune_unknown_keeps_the_yaml_shape_and_nothing_else():
    '''A saved version may only carry paths the YAML still has. A key it dropped - a
    rename, or a rule that is now fixed in code - is not a setting any more, and a merge
    would otherwise copy it into every version after it.'''
    base = {"pipeline": {"scoring": {"offchain_weight": 45}, "gate_limit": 20}}
    payload = {"pipeline": {"scoring": {"offchain_weight": 45, "product_weight": 45},
                            "gate_limit": 20, "gone": 1},
               "dropped_section": {"a": 1}}
    assert config.prune_unknown(base, payload) == {
        "pipeline": {"scoring": {"offchain_weight": 45}, "gate_limit": 20}}
