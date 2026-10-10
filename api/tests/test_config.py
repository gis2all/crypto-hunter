"""The chain config is the source of the口径; mistakes here poison everything."""

import json
import os

import pytest

from app import config
from app.store import db


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
    assert set(chains) == {"arc", "bsc", "robinhood", "solana"}
    assert chains["arc"]["enabled"] is True
    assert chains["bsc"]["enabled"] is False
    assert chains["robinhood"]["enabled"] is True


def test_robinhood_scans_its_own_sources():
    """`f_narrative` is judged against the chain's own line, so a chain that ships none
    ships a prompt with no target. Every shipped chain carries one, and a file that leaves
    it out reads as empty rather than as some other chain's line."""
    for chain_id in ("arc", "bsc", "robinhood", "solana"):
        assert config.chain_thesis(config.chain(chain_id)), chain_id
    assert config.chain_thesis({"thesis": "  x  "}) == "x", "the line is trimmed"
    assert config.chain_thesis({}) == ""


    """The three launchpad feeds only exist for Robinhood, and the two Arc-only sources must
    not be in the list: RadarDEX and Tolly answer about Robinhood with the Arc rollout, so a
    config that named them would fill the pool with another chain's tokens. The DexScreener
    board is the one scanner here that nobody had to be told about: it enumerates the chain
    as the site lists it."""
    chain = config.chain("robinhood")
    assert set(chain["pipeline"]["scan_sources"]) == {
        "gmgn", "geckoterminal", "coinbarrel", "pez", "peeps", "dexscreener_rank",
    }
    for arc_only in ("radardex", "tolly"):
        assert arc_only not in chain["sources"]


def test_arc_keeps_the_established_cadence():
    arc = config.chain("arc")
    assert arc["schedule"]["interval_hours"] == 3
    assert arc["schedule"]["start_hour"] == 2
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


@pytest.fixture()
def chains_dir(tmp_path, monkeypatch):
    """A throwaway chain directory, with the loader's cache emptied on both sides of the test.

    `load_chains()` is cached for the life of the process, so a test that points the loader at
    its own directory has to clear it first - or whichever test read first answers - and again
    on the way out, or this directory answers for the rest of the run.
    """
    monkeypatch.setattr(config, "CHAINS_DIR", tmp_path)
    config.reload()
    yield tmp_path
    config.reload()


def test_the_tracked_template_is_not_a_chain(chains_dir):
    """The factory file is `<id>.example.yaml`; the live one is `<id>.yaml` beside it.

    Both in one directory so a reader can diff what they have against what we ship, which only
    holds if the loader skips the template - reading it would give one chain id two entries and
    the same set of settings twice on the rail.
    """
    (chains_dir / "demo.example.yaml").write_text(
        "id: demo\nenabled: true\n"
        "configs:\n"
        "  - id: default\n"
        "    label: 出厂默认\n"
        "  - id: wide\n"
        "    label: 宽口\n",
        encoding="utf-8")
    (chains_dir / "demo.yaml").write_text(
        "id: demo\nenabled: true\n"
        "configs:\n"
        "  - id: default\n"
        "    label: 本机的默认配置\n"
        "  - id: wide\n"
        "    label: 宽口\n"
        "  - id: tight\n"
        "    label: 紧口\n",
        encoding="utf-8")

    chains = config.load_chains()
    assert set(chains) == {"demo"}
    assert [cfg["id"] for cfg in chains["demo"]["configs"]] == ["default", "wide", "tight"]
    assert chains["demo"]["configs"][0]["label"] == "本机的默认配置"


def test_a_machine_that_has_never_run_gets_the_file_from_the_template(chains_dir):
    """A clone has the templates only; the first read writes the live file beside one.

    That read is every way into the app - the API, the ticker, a test - so the seeding sits on
    the loader rather than on a startup hook the tests would never run.
    """
    (chains_dir / "demo.example.yaml").write_text(
        "id: demo\nenabled: true\nconfigs:\n  - id: default\n    label: 默认配置\n",
        encoding="utf-8")

    chains = config.load_chains()
    assert set(chains) == {"demo"}
    live = chains_dir / "demo.yaml"
    assert live.exists()
    assert live.read_bytes() == (chains_dir / "demo.example.yaml").read_bytes()


def test_the_ticker_obeys_the_schedule_the_page_saved_not_the_files(chains_dir, isolated_db):
    """The two knobs are saved as a version, while the file keeps the factory ones.

    Reading the file here would mean a schedule set on the page never fires: the ticker
    would go on starting rounds at the hours the operator replaced.
    """
    (chains_dir / "demo.yaml").write_text(
        "id: demo\n"
        "enabled: true\n"
        "configs:\n"
        "  - id: default\n"
        "    label: default\n"
        "    schedule:\n"
        "      interval_hours: 3\n"
        "      start_hour: 2\n",
        encoding="utf-8")
    config.reload()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO settings_versions (chain_id, config_id, version, payload_json,"
            " note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "default", 1,
             json.dumps({"schedule": {"interval_hours": 6, "start_hour": 5}}), None,
             "2026-10-06T00:00:00+08:00"))
    finally:
        conn.close()

    assert config.live_schedule("demo")["interval_hours"] == 6
    assert config.enabled_configs()[0]["schedule"]["interval_hours"] == 6
    assert config.enabled_configs()[0]["schedule"]["start_hour"] == 5
