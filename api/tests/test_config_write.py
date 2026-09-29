"""Editing a chain file from the page: add a set of settings, rename one, delete one.

These are the only writes to `config/chains/*.yaml` that happen outside a text editor, so
what has to hold is: a new one is seeded from the base's own values, the file keeps the
comments and the newline it was written with, an id never moves, and a delete takes the
whole report library or none of it. `default` and the live set of settings are the two that
cannot go, and a delete needs its name typed back before it happens at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.pipeline import run as pipeline_run, states
from app.store import db

ARC = "/api/v1/chains/arc"

#: One chain file, with a comment in it: what a write must not take with it.
ONE_SET = """\
# Every knob says why it is the number it is, and a writer that loses this line loses the
# reason the file is readable at all.
id: arc
label: Arc
enabled: true
explorer: https://explorer.arc.io
rpc: https://rpc.arc-scan.org
configs:
  - id: default
    label: 默认配置
    enabled: true
    locked: true
    sources: [gmgn]
    schedule: {cron: "0 2 * * *", timezone: Asia/Shanghai}
    pipeline: {gate_limit: 20}
    report: {per_round: 5}
    alerts: {rename_count: true}
"""

#: Two sets of settings with different values, so what a copy took can be told apart.
TWO_SETS = """\
id: arc
label: Arc
enabled: true
explorer: https://explorer.arc.io
rpc: https://rpc.arc-scan.org
configs:
  - id: default
    label: 默认配置
    enabled: true
    locked: true
    sources: [gmgn]
    pipeline: {gate_limit: 20}
  - id: wide
    label: Wide
    enabled: false
    sources: [tolly]
    pipeline: {gate_limit: 40}
"""


def _seed(tmp_path: Path, monkeypatch, text: str) -> Path:
    """One chain file on disk, in a directory the app reads instead of the shipped one."""
    path = tmp_path / "arc.yaml"
    path.write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(config, "CHAINS_DIR", tmp_path)
    config.reload()
    return path


@pytest.fixture()
def chain_file(tmp_path, monkeypatch):
    """The chain file under test. Written here, because a write must never reach the
    shipped one - the first version of this test would have edited `config/chains/arc.yaml`
    out from under the repository."""
    yield _seed(tmp_path, monkeypatch, ONE_SET)
    config.reload()


@pytest.fixture()
def two_sets_file(tmp_path, monkeypatch):
    yield _seed(tmp_path, monkeypatch, TWO_SETS)
    config.reload()


@pytest.fixture()
def two_live_file(tmp_path, monkeypatch):
    """A file switched on twice, which only a human with a text editor can produce."""
    yield _seed(tmp_path, monkeypatch, TWO_SETS.replace("    enabled: false",
                                                        "    enabled: true"))
    config.reload()


def test_a_new_set_of_settings_starts_from_the_default_one(chain_file):
    made = config.create_config("arc", "new")
    assert made["label"] == "配置 2"
    assert made["enabled"] is False
    assert made["locked"] is False
    seeded = config.chain("arc", made["id"])
    assert [seeded[s] for s in config.SETTINGS_SECTIONS] == [
        config.chain("arc", "default")[s] for s in config.SETTINGS_SECTIONS]


def test_a_new_one_is_named_around_the_ones_that_exist(chain_file):
    assert [config.create_config("arc", "new")["label"] for _ in range(2)] == ["配置 2",
                                                                              "配置 3"]


def test_a_copy_is_named_after_the_one_it_came_from(chain_file):
    wide = config.create_config("arc", "new", label="Wide")
    first = config.create_config("arc", "copy", from_config_id=wide["id"])
    second = config.create_config("arc", "copy", from_config_id=wide["id"])
    assert (first["label"], second["label"]) == ("Wide 副本", "Wide 副本 2")


def test_a_copy_takes_the_values_of_the_one_it_copied(two_sets_file):
    """Not the default's: a copy of a calibre that screens wide has to screen wide."""
    copy = config.create_config("arc", "copy", from_config_id="wide")
    assert config.chain("arc", copy["id"])["pipeline"] == {"gate_limit": 40}
    assert config.chain("arc", copy["id"])["sources"] == ["tolly"]
    assert config.chain("arc", "default")["pipeline"] == {"gate_limit": 20}


def test_a_generated_id_is_not_reused(chain_file):
    first = config.create_config("arc", "new")
    assert first["id"].startswith("c") and len(first["id"]) == 7
    config.delete_config("arc", first["id"])
    assert config.create_config("arc", "new")["id"] != first["id"]


def test_renaming_moves_the_label_and_not_the_id(chain_file):
    made = config.create_config("arc", "new")
    renamed = config.rename_config("arc", made["id"], "打新雷达")
    assert renamed["label"] == "打新雷达"
    assert renamed["id"] == made["id"]
    assert [c["id"] for c in config.configs_of("arc")] == ["default", made["id"]]


def test_the_default_set_of_settings_can_be_renamed(chain_file):
    """What is locked is the entry, not the words on it."""
    assert config.rename_config("arc", "default", "默认口径")["label"] == "默认口径"
    assert config.config_entry("arc", "default")["locked"] is True


def test_a_name_keeps_its_own_entry(chain_file):
    made = config.create_config("arc", "new", label="Wide")
    assert config.rename_config("arc", made["id"], "  Wide  ")["label"] == "Wide"


def test_a_name_another_entry_has_is_refused(chain_file):
    config.create_config("arc", "new", label="Wide")
    with pytest.raises(ValueError):
        config.create_config("arc", "new", label="Wide")
    with pytest.raises(ValueError):
        config.rename_config("arc", "default", "Wide")


def test_a_blank_or_overlong_name_is_refused(chain_file):
    for bad in ("", "   ", "\n", "x" * 21):
        with pytest.raises(ValueError):
            config.create_config("arc", "new", label=bad)


def test_a_name_is_trimmed_to_one_line(chain_file):
    assert config.create_config("arc", "new", label="  a\n  b ")["label"] == "a b"


def test_a_written_file_keeps_its_comments_and_its_newline(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, ONE_SET.replace("\n", "\r\n"))
    try:
        config.create_config("arc", "new")
        raw = path.read_bytes()
        assert raw.count(b"\r\n") == raw.count(b"\n")  # still CRLF, every line
        assert b"# Every knob says why" in raw
        assert not path.with_name("arc.yaml.tmp").exists()
    finally:
        config.reload()


def test_a_file_whose_values_nobody_changed_comes_back_byte_for_byte(chain_file):
    """The property that makes a one-line edit a one-line diff: a rewrite that changes
    nothing on the page changes nothing in the file either."""
    before = chain_file.read_bytes()
    config.rename_config("arc", "default", "默认配置")
    assert chain_file.read_bytes() == before


def test_a_file_with_two_sets_of_settings_on_is_read_as_one(two_live_file):
    """The ticker would start the first and leave the second waiting for a slot that then
    goes by. So the first one wins, and the page is told the file says otherwise."""
    assert [c["enabled"] for c in config.configs_of("arc")] == [True, False]
    assert "只按第一套" in config.chain_entry("arc")["warning"]


def test_a_single_live_set_of_settings_is_not_a_warning(chain_file):
    assert config.chain_entry("arc")["warning"] is None


def test_the_page_is_told_about_the_file_it_has_to_fix(two_live_file, client):
    assert client.get(ARC).json()["warning"]


def test_the_page_can_add_rename_and_read_back_a_set_of_settings(chain_file, client):
    made = client.post(f"{ARC}/configs", json={"mode": "new"}).json()
    assert made["label"] == "配置 2"
    assert made["library"] == {"runs": 0, "reports": 0, "entries": 0, "versions": 0}
    renamed = client.patch(f"{ARC}/configs/{made['id']}", json={"label": "打新雷达"}).json()
    assert renamed["label"] == "打新雷达"
    assert client.get(f"{ARC}/configs/{made['id']}").json()["id"] == made["id"]
    assert [c["label"] for c in client.get(ARC).json()["configs"]] == ["默认配置", "打新雷达"]


def test_a_name_that_is_taken_comes_back_as_the_backends_own_sentence(chain_file, client):
    client.post(f"{ARC}/configs", json={"mode": "new", "label": "Wide"})
    refused = client.post(f"{ARC}/configs", json={"mode": "new", "label": "Wide"})
    assert refused.status_code == 400
    assert "Wide" in refused.json()["detail"]


def test_a_name_nobody_has_is_a_404(chain_file, client):
    assert client.get(f"{ARC}/configs/nope").status_code == 404
    assert client.patch(f"{ARC}/configs/nope", json={"label": "x"}).status_code == 404
    assert client.request("DELETE", f"{ARC}/configs/nope",
                          json={"label": "x"}).status_code == 404


def test_the_default_entry_is_refused_a_delete(chain_file, client):
    refused = client.request("DELETE", f"{ARC}/configs/default", json={"label": "默认配置"})
    assert refused.status_code == 409
    assert "默认配置" in refused.json()["detail"]
    assert config.config_entry("arc", "default")["id"] == "default"


def test_a_delete_needs_the_name_typed_back(chain_file, client):
    made = client.post(f"{ARC}/configs", json={"mode": "new"}).json()
    refused = client.request("DELETE", f"{ARC}/configs/{made['id']}",
                             json={"label": "配置 3"})
    assert refused.status_code == 409
    assert config.config_entry("arc", made["id"])["label"] == "配置 2"


def test_the_live_entry_is_refused_a_delete(chain_file, client):
    made = config.create_config("arc", "new", label="Wide")
    config.set_active_config("arc", made["id"])
    refused = client.request("DELETE", f"{ARC}/configs/{made['id']}", json={"label": "Wide"})
    assert refused.status_code == 409
    assert "正在生效" in refused.json()["detail"]


def test_saving_a_set_of_settings_is_what_makes_it_live(chain_file, client):
    made = config.create_config("arc", "new", label="Wide")
    assert client.put(f"{ARC}/settings?config_id={made['id']}",
                      json={"patch": {}}).status_code == 200
    live = {c["id"]: c["enabled"] for c in config.configs_of("arc")}
    assert live == {"default": False, made["id"]: True}


def test_deleting_takes_the_whole_report_library(chain_file, client, isolated_db,
                                                tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    made = config.create_config("arc", "new", label="Wide")
    run = pipeline_run.create_run("arc", config_id=made["id"])
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO reports (run_id, ca, rank, status, payload_json, created_at)"
            " VALUES (?, '0xaa', 1, 'written', '{}', ?)", (run["id"], db.now_iso()))
        conn.execute(
            "INSERT INTO lexicon (ca, chain_id, config_id, run_id, updated_at)"
            " VALUES ('0xaa', 'arc', ?, ?, ?)", (made["id"], run["id"], db.now_iso()))
        conn.execute(
            "INSERT INTO lexicon_sources (ca, chain_id, config_id, kind, url, fetched_at)"
            " VALUES ('0xaa', 'arc', ?, 'website', 'https://x', ?)",
            (made["id"], db.now_iso()))
        conn.execute(
            "INSERT INTO settings_versions (chain_id, config_id, version, payload_json,"
            " created_at) VALUES ('arc', ?, 9, '{}', ?)", (made["id"], db.now_iso()))
        conn.execute("INSERT INTO tokens (ca, chain_id, updated_at) VALUES ('0xaa', 'arc', ?)",
                     (db.now_iso(),))
        conn.execute("INSERT INTO llm_calls (run_id, purpose, created_at)"
                     " VALUES (?, 'research', ?)", (run["id"], db.now_iso()))
    finally:
        conn.close()
    raw = config.RAW_DIR / run["id"]
    raw.mkdir(parents=True)
    (raw / "gmgn.json").write_text("{}", encoding="utf-8")

    # A round of it in flight is a row a worker owns, so the delete waits for it.
    busy = client.request("DELETE", f"{ARC}/configs/{made['id']}", json={"label": "Wide"})
    assert busy.status_code == 409
    assert "正在跑" in busy.json()["detail"]

    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?",
                     (states.S.RESEARCHED.value, run["id"]))
    finally:
        conn.close()

    gone = client.request("DELETE", f"{ARC}/configs/{made['id']}", json={"label": "Wide"})
    assert gone.status_code == 200
    assert gone.json() == {"runs": 1, "reports": 1, "entries": 1, "versions": 1}
    assert [c["id"] for c in config.configs_of("arc")] == ["default"]
    assert not raw.exists()
    assert db.library_size("arc", made["id"]) == {"runs": 0, "reports": 0, "entries": 0,
                                                  "versions": 0}
    conn = db.connect()
    try:
        left = {table: conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in ("lexicon", "lexicon_sources", "tokens", "llm_calls")}
    finally:
        conn.close()
    # The chain-level facts stay, so does the ledger of what the round spent.
    assert left == {"lexicon": 0, "lexicon_sources": 0, "tokens": 1, "llm_calls": 1}


def test_a_delete_takes_only_its_own_report_library(two_sets_file, client, isolated_db):
    pipeline_run.create_run("arc", config_id="default")
    doomed = pipeline_run.create_run("arc", config_id="wide")
    # Finished, or the round in flight is the gate that answers first.
    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?",
                     (states.S.RESEARCHED.value, doomed["id"]))
    finally:
        conn.close()
    assert client.request("DELETE", f"{ARC}/configs/wide",
                          json={"label": "Wide"}).status_code == 200
    assert db.library_size("arc", "default")["runs"] == 1
    assert db.library_size("arc", "wide")["runs"] == 0
