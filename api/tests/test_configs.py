"""A chain's sets of settings: two libraries, and no way to blend them together.

The model is one chain carrying N sets of settings. What has to hold is that each one keeps
its own runs, its own report library, its own settings history and its own lexicon - the
same CA screened under two calibres is two judgements, not one overwritten judgement.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.pipeline import run as pipeline_run, schedule
from app.report import lexicon
from app.store import db

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: One chain file holding two sets of settings. The first is due at 02:00 and 03:00 and
#: the second at 03:00 only, so one instant has both of them due together.
TWO_SETS = """\
id: arc
label: Arc
enabled: true
explorer: https://explorer.arc.io
rpc: https://rpc.arc-scan.org
configs:
  - id: default
    label: Default
    enabled: true
    locked: true
    sources: [gmgn]
    schedule: {cron: "0 2,3 * * *", timezone: Asia/Shanghai}
    pipeline: {gate_limit: 20}
    report: {per_round: 5}
    alerts: {rename_count: true}
  - id: wide
    label: Wide
    enabled: true
    sources: [tolly]
    schedule: {cron: "0 3 * * *", timezone: Asia/Shanghai}
    pipeline: {gate_limit: 40}
    report: {per_round: 8}
    alerts: {rename_count: false}
"""

CA = "0xaa"


@pytest.fixture()
def two_sets(tmp_path, monkeypatch):
    """One chain file holding two sets of settings, loaded in place of the shipped ones."""
    (tmp_path / "arc.yaml").write_text(TWO_SETS, encoding="utf-8")
    monkeypatch.setattr(config, "CHAINS_DIR", tmp_path)
    config.reload()
    yield tmp_path
    config.reload()


def test_one_file_carries_every_set_of_settings(two_sets):
    assert [c["id"] for c in config.configs_of("arc")] == ["default", "wide"]
    assert config.chain("arc")["pipeline"]["gate_limit"] == 20
    assert config.chain("arc", "wide")["pipeline"]["gate_limit"] == 40


def test_each_set_of_settings_brings_its_own_sources(two_sets):
    assert config.chain("arc")["sources"] == ["gmgn"]
    assert config.chain("arc", "wide")["sources"] == ["tolly"]


def test_the_id_stays_the_chains_under_a_second_set_of_settings(two_sets):
    """Both calibres screen the same tokens on the same node, and that is what the id
    says - the set of settings is named beside it and never instead of it."""
    wide = config.chain("arc", "wide")
    assert wide["id"] == "arc"
    assert wide["config_id"] == "wide"
    assert wide["explorer"] == "https://explorer.arc.io"


def test_the_default_entry_is_the_one_nobody_can_delete(two_sets):
    entries = {c["id"]: c for c in config.configs_of("arc")}
    assert entries["default"]["locked"] is True
    assert entries["wide"]["locked"] is False


def test_a_blank_name_reads_the_default_set_of_settings(two_sets):
    assert config.chain("arc", None)["config_id"] == "default"
    assert config.chain("arc", "")["config_id"] == "default"


def test_a_name_that_is_not_there_is_refused(two_sets):
    with pytest.raises(KeyError):
        config.chain("arc", "nope")


def test_each_set_of_settings_keeps_its_own_history(two_sets, isolated_db):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO settings_versions (chain_id, config_id, version, payload_json,"
            " note, created_at) VALUES ('arc', 'wide', 1, '{}', NULL, ?)",
            (db.now_iso(),),
        )
    finally:
        conn.close()
    assert config.latest_settings_version("arc", "default") is None
    assert config.latest_settings_version("arc", "wide") is not None


def test_a_round_is_read_against_its_own_set_of_settings(two_sets, isolated_db):
    """The last round of *this* calibre, not of the chain: a token would otherwise be
    reported as new-in or dropped-out against a universe it was never in."""
    first = pipeline_run.create_run("arc", config_id="default")
    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET selected = 1 WHERE id = ?", (first["id"],))
        conn.execute(
            "INSERT INTO reports (run_id, ca, rank, verdict, status, payload_json,"
            " created_at) VALUES (?, ?, 1, NULL, 'written', '{}', ?)",
            (first["id"], CA, db.now_iso()),
        )
    finally:
        conn.close()
    second = pipeline_run.create_run("arc", config_id="wide")
    assert pipeline_run.previous_picks("arc", second["id"], "wide") == []
    assert len(pipeline_run.previous_picks("arc", second["id"], "default")) == 1


def test_a_run_carries_the_set_of_settings_it_was_created_under(two_sets, isolated_db):
    row = pipeline_run.create_run("arc", config_id="wide")
    assert row["config_id"] == "wide"
    assert row["run_key"].startswith("arc:wide:")


def test_the_lexicon_holds_one_entry_per_set_of_settings(two_sets, isolated_db):
    """The same CA judged twice is two entries. Keyed by CA alone, the second calibre's
    round would overwrite the judgement the first calibre's card was written from."""
    lexicon.store_entries("arc", "run-a", {CA: {"symbol": "AAA"}}, config_id="default")
    lexicon.store_entries("arc", "run-b", {CA: {"symbol": "AAA"}}, config_id="wide")
    conn = db.connect()
    try:
        rows = conn.execute("SELECT config_id, run_id FROM lexicon WHERE ca = ?",
                            (CA,)).fetchall()
    finally:
        conn.close()
    assert sorted(r["config_id"] for r in rows) == ["default", "wide"]


def test_clearing_a_round_leaves_the_other_set_of_settings_alone(two_sets, isolated_db):
    """The cleanup after a round deletes the rounds that came before *it* - not the other
    calibre's, which is still the evidence behind a card somebody can read."""
    lexicon.store_entries("arc", "run-old", {CA: {"symbol": "AAA"}}, config_id="default")
    lexicon.store_entries("arc", "run-new", {CA: {"symbol": "AAA"}}, config_id="wide")
    assert lexicon.forget_other_rounds("arc", "wide", "run-new") == 0
    conn = db.connect()
    try:
        rows = conn.execute("SELECT config_id FROM lexicon WHERE ca = ?", (CA,)).fetchall()
    finally:
        conn.close()
    assert sorted(r["config_id"] for r in rows) == ["default", "wide"]


def test_the_ticker_starts_one_set_of_settings_per_chain(two_sets, isolated_db, monkeypatch):
    """A chain's calibres pull from the same sources at the same rate limits, so one of
    them starts and the other waits its turn rather than reading beside it."""
    started = []
    monkeypatch.setattr(
        schedule, "submit",
        lambda chain_id, run_id, config_id=None: started.append(config_id))
    # Both are due at this instant, which is what makes the assertion worth making.
    assert schedule.tick(datetime(2026, 9, 24, 3, 0, 10, tzinfo=SHANGHAI)) != []
    assert started == ["default"]