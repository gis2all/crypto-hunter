"""`config/system.yaml` and the 系统设置 page over it: one file, two faces.

The file is what the app reads (through `config.load_env_file`, on every model call), and the page
is a form in front of it. These tests are about the seam: what the page may show, what a save
writes, and what is in force the moment it returns.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from app import config, logging_setup, system_settings
from app.api.v1 import forms
from app.llm import client as llm_client
from app.search.engines.xsearch import PROBE_TIMEOUT

#: The file as an operator writes it - comments, empty boxes and all.
FILE = """\
# 本机接线。
llm:
  # 写手的网关。
  base_url: https://gateway/v1
  api_key: sk-live-0123456789
  model: m
  reasoning: none
research_workers: 12
x:
  auth_token: cookie
  ct0: null
  cli: twitter
# 日志等级。
log_level: INFO
"""


@pytest.fixture()
def system_file(tmp_path, monkeypatch):
    """A `system.yaml` of its own, and an environment with none of its keys in it."""
    path = tmp_path / "system.yaml"
    path.write_text(FILE, encoding="utf-8")
    monkeypatch.setattr(config, "SYSTEM_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    for _key, name, _kind in config.SYSTEM_KEYS:
        monkeypatch.delenv(name, raising=False)
    # And the file is in force, as it is in a running process: `config` applies it at import.
    config.load_env_file()
    return path


def _fields() -> dict[str, dict]:
    """The page's rows by key - what the SPA receives, one flatten away."""
    blocks = forms.system_blocks(system_settings.state())
    return {f["key"]: f for block in blocks for group in block["groups"] for f in group["fields"]}


def test_every_key_has_a_row():
    """A key in the table with no label is a row nobody can read, and a label with no key is a
    box nothing writes - unless the key is file-only, which is the one deliberate way for the
    page not to offer something the file takes (the gateway headers rule). Both are caught here
    rather than on the page."""
    keys = {key for key, _env, _kind in config.SYSTEM_KEYS}
    assert set(forms.SYSTEM_LABELS) | config.FILE_ONLY_KEYS == keys
    assert not (set(forms.SYSTEM_LABELS) & config.FILE_ONLY_KEYS)


def test_a_file_only_key_is_not_drawn():
    """The other half of the same rule: file-only is not a drawer for keys somebody forgot to
    label, so a key listed there must really be absent from what the page is sent."""
    assert config.FILE_ONLY_KEYS.isdisjoint(_fields())


def test_the_page_draws_the_file_and_never_a_credential(system_file):
    fields = _fields()
    assert fields["llm.base_url"]["value"] == "https://gateway/v1"
    # Nothing marks a row that came from the file: that is the normal case, and only the
    # exceptions - a variable the shell owns, a value nobody set - are worth saying.
    assert fields["llm.base_url"]["dim"] == ""
    assert fields["llm.tries"]["dim"] == "默认"
    # The key itself is never sent: the row says one is stored, shows its two ends, and a blank
    # box keeps it - and the box is a password box, so typing one is not for the room.
    assert fields["llm.api_key"]["value"] == ""
    assert fields["llm.api_key"]["dim"] == "已配置 · sk-liv…6789"
    assert fields["llm.api_key"]["widget"] == "password"
    assert "sk-live-0123456789" not in str(fields["llm.api_key"])
    # X 的两个旧键从 2026-10-28 起由页面上的「X会话」列表接管，不再画成两行。
    assert "x.ct0" not in fields and "x.auth_token" not in fields
    assert {"x.auth_token", "x.ct0"} <= config.FILE_ONLY_KEYS
    assert fields["llm.reasoning"]["widget"] == "select"


def test_a_variable_the_shell_set_is_drawn_read_only(system_file, monkeypatch):
    """A shell variable beats the file, so its row is a reading: a box there would accept a save
    and change nothing, which is the one thing this page must not do."""
    monkeypatch.setenv("CH_LLM_BASE_URL", "https://shell/v1")
    fields = _fields()
    assert fields["llm.base_url"]["widget"] == "ro"
    assert fields["llm.base_url"]["value"] == "https://shell/v1"
    assert fields["llm.base_url"]["dim"] == "环境变量"
    assert fields["llm.model"]["widget"] == "text"


def test_two_readers_never_leave_a_value_the_file_does_not_own(system_file):
    """`state()` re-reads the file on every request. Two at once used to delete each other's
    variables and write back an empty ownership record, and the page then drew every row
    read-only as 「环境变量」 until a restart (2026-10-28)."""
    ready = threading.Barrier(8)

    def load(_):
        ready.wait()
        config.load_env_file()
        return config.file_owner()

    with ThreadPoolExecutor(max_workers=8) as pool:
        owners = list(pool.map(load, range(8)))
    owned = config.file_owner()
    assert "CH_LLM_MODEL" in owned
    assert os.environ["CH_LLM_MODEL"] == owned["CH_LLM_MODEL"]
    assert all("CH_LLM_MODEL" in owner for owner in owners)


def test_a_whole_number_is_drawn_without_a_decimal(system_file):
    """A YAML scalar is a ruamel subclass of int, and the response model turned it into `0.0`."""
    from ruamel.yaml.scalarint import ScalarInt

    value = system_settings._shown(ScalarInt(0), "num")
    assert type(value) is int and value == 0
    assert system_settings._shown(ScalarInt(300), "num") == 300
    assert system_settings._shown(0.5, "num") == 0.5
    assert system_settings._shown(True, "bool") is True


def test_a_save_leaves_every_other_line_alone(system_file):
    """One edited row is one changed line: the comments are the reason each number is what it is,
    so a save that reformats the file is a diff nobody can read."""
    system_settings.save({"llm.model": "m2"})
    changed = [
        (before, after)
        for before, after in zip(FILE.splitlines(), system_file.read_text(encoding="utf-8").splitlines())
        if before != after
    ]
    assert changed == [("  model: m", "  model: m2")]
    assert "  # 写手的网关。" in system_file.read_text(encoding="utf-8")


def test_a_save_keeps_the_file_it_replaced(system_file):
    """The save leaves what it replaced in `system.yaml.bak`, so a wrong save is undone by
    copying that back - the copy that was missing when a test wrote this file by accident."""
    system_settings.save({"llm.model": "m2"})
    backup = system_file.with_name(system_file.name + ".bak")
    assert backup.read_text(encoding="utf-8") == FILE
    saved = system_file.read_text(encoding="utf-8")
    system_settings.save({"llm.model": "m3"})
    assert backup.read_text(encoding="utf-8") == saved


def test_a_save_is_in_force_before_it_returns(system_file):
    assert os.environ["CH_LLM_MODEL"] == "m"
    system_settings.save({"llm.model": "m2"})
    assert os.environ["CH_LLM_MODEL"] == "m2"
    # An emptied box means "not configured" and the variable goes with it, so the next call
    # reads the code's own default instead of a value the operator just deleted.
    system_settings.save({"llm.model": ""})
    assert "CH_LLM_MODEL" not in os.environ
    assert "CH_LLM_MODEL" not in config.file_owner()


def test_a_blank_secret_box_keeps_the_stored_key(system_file):
    """Clearing and keeping are the same keystroke in a box that never shows the value, and
    keeping is the one that cannot cost an operator a working key."""
    system_settings.save({"llm.base_url": "https://other/v1", "llm.api_key": ""})
    assert os.environ["CH_LLM_API_KEY"] == "sk-live-0123456789"
    system_settings.save({"llm.api_key": "sk-new"})
    assert os.environ["CH_LLM_API_KEY"] == "sk-new"


def test_a_json_row_is_stored_as_a_map_not_a_string(system_file):
    system_settings.save({"llm.extra_headers": '{"x-opencode-session": "crypto-hunter"}'})
    assert "x-opencode-session: crypto-hunter" in system_file.read_text(encoding="utf-8")
    assert os.environ["CH_LLM_EXTRA_HEADERS"] == '{"x-opencode-session": "crypto-hunter"}'


def test_a_bad_answer_is_refused_and_nothing_is_written(system_file):
    """Every one of these would be a file the next call fails on, an hour after the save that
    looked like it worked."""
    for patch in (
        {"llm.nope": "x"},
        {"llm.reasoning": "sometimes"},
        {"llm.tries": "1.5"},
        {"llm.max_tokens": "nan"},
        {"llm.extra_headers": "not json"},
    ):
        with pytest.raises(ValueError):
            system_settings.save(patch)
    assert os.environ["CH_LLM_MODEL"] == "m"
    assert system_file.read_text(encoding="utf-8") == FILE


def test_the_log_level_a_save_changes_is_in_force(system_file):
    logging_setup.setup()
    assert logging.getLogger(logging_setup.NAMESPACE).level == logging.INFO
    # Lower case is accepted and stored as the table spells it, so the file keeps one form.
    system_settings.save({"log_level": "warning"})
    assert logging.getLogger(logging_setup.NAMESPACE).level == logging.WARNING
    assert "log_level: WARNING" in system_file.read_text(encoding="utf-8")


def test_the_first_save_seeds_the_file_from_the_example(tmp_path, monkeypatch):
    """Nothing to edit yet: the copy carries the template's comments, so the operator's first
    edit lands in a file that still explains itself."""
    (tmp_path / "config").mkdir()
    example = (tmp_path / "config" / "system.yaml.example")
    example.write_text(FILE.replace("api_key: sk-live-0123456789", "api_key: null"), encoding="utf-8")
    path = tmp_path / "config" / "system.yaml"
    monkeypatch.setattr(config, "SYSTEM_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    for _key, name, _kind in config.SYSTEM_KEYS:
        monkeypatch.delenv(name, raising=False)

    system_settings.save({"llm.model": "m9"})
    text = path.read_text(encoding="utf-8")
    assert "# 本机接线。" in text
    assert "  model: m9" in text
    assert os.environ["CH_LLM_MODEL"] == "m9"


def test_the_page_and_a_save_go_through_the_api(system_file, client):
    form = client.get("/api/v1/system/form")
    assert form.status_code == 200
    assert form.json()["path"] == str(system_file)
    assert [b["title"] for b in form.json()["blocks"]] == ["模型", "搜索", "日志"]
    x_block = next(b for b in form.json()["blocks"] if b["key"] == "x")
    assert [g["title"] for g in x_block["groups"]] == ["X会话"]

    saved = client.put("/api/v1/system", json={"patch": {"llm.model": "m2"}})
    assert saved.status_code == 200
    assert os.environ["CH_LLM_MODEL"] == "m2"

    refused = client.put("/api/v1/system", json={"patch": {"llm.nope": "x"}})
    assert refused.status_code == 422
    assert "llm.nope" in refused.json()["detail"]


def _reply(response: httpx.Response) -> httpx.Response:
    return response


def _models(*ids: str) -> httpx.Response:
    return _reply(httpx.Response(200, json={"data": [{"id": i} for i in ids]},
                                 request=httpx.Request("GET", "http://gateway")))


def _answer(content: str, finish_reason: str = "stop") -> httpx.Response:
    return _reply(httpx.Response(
        200, request=httpx.Request("GET", "http://gateway"),
        json={"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
    ))


def _refusal(status: int, text: str) -> httpx.Response:
    return _reply(httpx.Response(status, text=text, request=httpx.Request("GET", "http://gateway")))


def _with_standby(path, standby: str) -> None:
    """The same file with a 备用 Key under the primary, and the change in force: the fixture
    wrote it once, and a second write is how a test says what the other key is."""
    path.write_text(FILE.replace("  api_key: sk-live-0123456789",
                                 "  api_key: sk-live-0123456789\n  api_key_fallback: %s" % standby),
                    encoding="utf-8")
    config.load_env_file()


@pytest.fixture()
def gateway(monkeypatch):
    """Every line of a probe real except the socket: it records what was asked and answers
    whatever the test put in `box.response` - the same response to every call, or one per call
    when the box holds a list."""
    seen: list[dict] = []
    box = SimpleNamespace(response=None)

    def next_response():
        if isinstance(box.response, list):
            return box.response.pop(0)
        return box.response

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            seen.append({"method": "GET", "url": url, "headers": headers or {}})
            return next_response()

        def post(self, url, headers=None, json=None):
            seen.append({"method": "POST", "url": url, "headers": headers or {}, "body": json})
            return next_response()

    monkeypatch.setattr(llm_client.httpx, "Client", FakeClient)
    return SimpleNamespace(seen=seen, box=box)


def test_the_page_offers_the_usual_gateways_and_the_two_probes(system_file):
    """The form carries them, so the page needs no list of its own."""
    fields = _fields()
    assert [p["label"] for p in fields["llm.base_url"]["presets"]] == [
        "opencode zen", "OpenAI", "DeepSeek"]
    actions = [a["id"] for block in forms.system_blocks(system_settings.state())
               for group in block["groups"] for a in group["actions"]]
    assert actions == ["models", "test", "add-session", "x"]


def test_a_probe_reads_the_draft_not_the_file(system_file, client, gateway):
    """A row typed but not yet saved is the row the probe tests - otherwise the operator would
    have to write a wrong endpoint down in order to find out it is wrong."""
    gateway.box.response = _models("b", "a", "a")
    answer = client.post("/api/v1/system/models",
                         json={"patch": {"llm.base_url": "https://draft/v1/"}})
    assert answer.status_code == 200
    assert gateway.seen[0]["url"] == "https://draft/v1/models"
    # The key box was untouched, so the probe used the one already stored.
    assert gateway.seen[0]["headers"]["Authorization"] == "Bearer sk-live-0123456789"
    body = answer.json()
    assert body["ok"] is True
    assert body["models"] == ["a", "b"]
    assert body["apply_to"] == "llm.model"
    assert body["note"]


def test_a_blank_key_box_probes_with_the_stored_key(system_file, client, gateway):
    gateway.box.response = _models("m")
    client.post("/api/v1/system/models",
                json={"patch": {"llm.api_key": "", "llm.base_url": "https://draft/v1"}})
    assert gateway.seen[0]["headers"]["Authorization"] == "Bearer sk-live-0123456789"


def test_a_refused_gateway_is_an_answer_not_an_error(system_file, client, gateway):
    """A wrong key is what the button is for: it prints beside the button and leaves the form
    exactly as it was."""
    gateway.box.response = _refusal(401, "bad key")
    answer = client.post("/api/v1/system/models", json={"patch": {}})
    assert answer.status_code == 200
    assert answer.json()["ok"] is False
    assert "401" in answer.json()["note"]


def test_a_200_with_no_text_is_the_failure_worth_reporting(system_file, client, gateway):
    """The one this repo already paid for: the model answers, the text is empty, nothing is an
    error - a probe that only checked reachability would call this configuration healthy."""
    gateway.box.response = _answer("", "length")
    answer = client.post("/api/v1/system/test", json={"patch": {}})
    body = answer.json()
    assert body["ok"] is False
    line = body["lines"][0]
    assert line["status"] == 200
    assert line["chars"] == 0
    assert "length" in line["note"]


def test_a_working_endpoint_says_so_with_its_latency(system_file, client, gateway):
    gateway.box.response = _answer("pong")
    body = client.post("/api/v1/system/test", json={"patch": {}}).json()
    assert body["ok"] is True
    line = body["lines"][0]
    assert line["chars"] == 4
    assert line["finish_reason"] == "stop"
    assert "毫秒" in line["note"]


def test_a_probe_is_not_a_round(system_file, client, gateway, monkeypatch):
    """No bill line and no cooldown: `llm_calls` is what a round spent, and a button that
    cooled a key down would punish the operator for testing - a standby that is refused here
    still has to be the key a round tries next."""
    recorded: list[dict] = []
    monkeypatch.setattr(llm_client, "_record", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(llm_client, "_DOWN", {})
    _with_standby(system_file, "sk-backup-9876543210")
    gateway.box.response = _models("m")
    client.post("/api/v1/system/models", json={"patch": {}})
    gateway.box.response = [_refusal(401, "bad key"), _answer("pong")]
    client.post("/api/v1/system/test", json={"patch": {}})
    assert recorded == []
    assert llm_client._DOWN == {}


def test_a_typo_is_refused_before_anything_is_called(system_file, client, gateway):
    """The probe coerces the draft the way a save does, so an endpoint is never asked about a
    value the file would not take."""
    answer = client.post("/api/v1/system/test", json={"patch": {"llm.max_tokens": "abc"}})
    assert answer.status_code == 422
    assert gateway.seen == []


def test_a_stored_credential_shows_its_ends_and_never_its_middle():
    """The row has to answer "is the panel using the key I think it is?" without handing a
    credential to a screenshot or a bug report."""
    assert system_settings.masked("sk-live-0123456789") == "sk-liv…6789"
    assert system_settings.masked("") == ""
    assert system_settings.masked("tiny") == "…"             # nothing to hide behind a middle
    assert "secret-tail" not in system_settings.masked("secret-tail")


def test_every_key_is_asked_not_just_the_primary(system_file, client, gateway):
    """The standby is the key nobody has ever watched answer, and a round only meets it on the
    day the primary is refused - the worst day to learn it is bad too."""
    _with_standby(system_file, "sk-backup-9876543210")
    gateway.box.response = _answer("pong")
    body = client.post("/api/v1/system/test", json={"patch": {}}).json()
    assert [call["headers"]["Authorization"] for call in gateway.seen] == [
        "Bearer sk-live-0123456789", "Bearer sk-backup-9876543210"]
    assert [line["label"] for line in body["lines"]] == ["API Key", "备用 Key"]
    assert [line["ok"] for line in body["lines"]] == [True, True]
    assert body["ok"] is True


def test_each_line_carries_its_own_key_s_answer(system_file, client, gateway):
    """One refused key does not speak for the other: telling the two apart is the whole point
    of pressing this with a standby configured."""
    _with_standby(system_file, "sk-backup-9876543210")
    gateway.box.response = [_refusal(429, "Go usage limit exceeded"), _answer("pong")]
    body = client.post("/api/v1/system/test", json={"patch": {}}).json()
    first, second = body["lines"]
    assert body["ok"] is False
    assert first["label"] == "API Key" and first["ok"] is False
    assert "429" in first["note"]
    assert second["label"] == "备用 Key" and second["ok"] is True
    assert second["chars"] == 4


def test_a_long_key_list_stops_at_three_calls_and_says_what_is_left(system_file, client,
                                                                     gateway):
    """A row may hold a comma list, so the walking stops: one press is not five billed calls."""
    system_file.write_text(FILE.replace("api_key: sk-live-0123456789", "api_key: k1, k2, k3, k4"),
                           encoding="utf-8")
    config.load_env_file()
    gateway.box.response = _answer("pong")
    body = client.post("/api/v1/system/test", json={"patch": {}}).json()
    assert len(gateway.seen) == 3
    assert [line["label"] for line in body["lines"]] == ["API Key", "备用 Key", "备用 Key 2", ""]
    assert body["lines"][-1]["note"] == "未探测 1 个 Key"


#: The client's own answer to `whoami --json`, cut to the fields the probe reads.
WHOAMI = '{"ok": true, "schema_version": 1, "data": {"user": {"screenName": "cryptohunter"}}}'


def _with_session(path, ct0: str = "csrf") -> None:
    """The same file with a ct0 under the token the fixture wrote, and the change in force."""
    path.write_text(FILE.replace("  ct0: null", "  ct0: %s" % ct0), encoding="utf-8")
    config.load_env_file()


@pytest.fixture()
def x_cli(monkeypatch):
    """Every line of the X probe real except the process: it records the argv it was handed and
    the environment and budget it was handed with, and answers whatever `box` holds."""
    seen: list[dict] = []
    box = SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(argv, **kwargs):
        seen.append({"argv": list(argv), **kwargs})
        return SimpleNamespace(returncode=box.returncode, stdout=box.stdout, stderr=box.stderr)

    monkeypatch.setattr("app.search.engines.xsearch.subprocess.run", fake_run)
    return SimpleNamespace(seen=seen, box=box)


def test_the_x_group_carries_its_own_probe():
    """The button belongs to the rows it tests: the page hands the group's key back and the
    answer is printed under that group, so the action travels with the group."""
    groups = {g["key"]: g for block in forms.system_blocks(system_settings.state())
              for g in block["groups"]}
    assert [a["label"] for a in groups["x-session"]["actions"]] == ["添加会话", "测试连接"]
    assert groups["llm-call"]["actions"] == []


def test_the_log_endpoint_reads_the_tail_and_filters(system_file, client, monkeypatch, tmp_path):
    """One URL instead of the redirect: the last lines, or the ones matching a word."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    (tmp_path / "api-run.err.log").write_text(
        "first line\nx: cookie2 search q -> 4 rows in 900ms\nlast line\n", encoding="utf-8")
    text = client.get("/api/v1/system/log?tail=2").text
    assert text.splitlines() == ["x: cookie2 search q -> 4 rows in 900ms", "last line"]
    assert client.get("/api/v1/system/log?grep=x%3A").text.strip().startswith("x: cookie2")


def test_the_ring_answers_only_what_a_reader_has_not_seen(system_file, client):
    """`since` is the record id, so a page can poll without re-reading the same lines."""
    logging_setup.setup()
    logger = logging.getLogger("crypto-hunter.test")
    before = logging_setup.RING.recent(limit=None)
    since = before[-1]["id"] if before else 0
    logger.info("one line a page would poll for")
    body = client.get("/api/v1/system/logs", params={"since": since, "name": "crypto-hunter.test"})
    items = body.json()["items"]
    assert [item["message"] for item in items][-1] == "one line a page would poll for"
    assert body.json()["next"] == items[-1]["id"]


def test_the_page_reads_the_pools_health_not_only_the_file(system_file, monkeypatch):
    """A session the file calls enabled can be resting from a 429 or retired by a 401; the page
    has to say which, or every call silently waits out a cooldown."""
    _with_session(system_file)
    from app.search import x_sessions as pool_module

    monkeypatch.setattr(pool_module, "pool", lambda: SimpleNamespace(snapshot=lambda: [
        {"label": "main", "enabled": True, "cooling": True}]))
    assert system_settings.x_sessions()[0]["state"] == "cooling"

    monkeypatch.setattr(pool_module, "pool", lambda: SimpleNamespace(snapshot=lambda: [
        {"label": "main", "enabled": False, "cooling": False}]))
    assert system_settings.x_sessions()[0]["state"] == "disabled"

    monkeypatch.setattr(pool_module, "pool", lambda: SimpleNamespace(snapshot=lambda: [
        {"label": "main", "enabled": True, "cooling": False}]))
    assert system_settings.x_sessions()[0]["state"] == ""


def test_the_logs_endpoint_gives_nothing_when_asked_for_nothing(system_file, client):
    body = client.get("/api/v1/system/logs", params={"limit": 0}).json()
    assert body["items"] == []


def test_the_log_endpoint_says_so_when_the_file_is_missing(system_file, client, monkeypatch,
                                                          tmp_path):
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    assert "还没有日志文件" in client.get("/api/v1/system/log").text


def test_a_working_x_session_says_who_it_is(system_file, client, x_cli):
    """One profile read, the session it was handed, and a budget a person can wait out."""
    _with_session(system_file)
    x_cli.box.stdout = WHOAMI
    body = client.post("/api/v1/system/x", json={"patch": {}}).json()
    assert body["ok"] is True
    assert "@cryptohunter" in body["note"]
    call = x_cli.seen[0]
    assert call["argv"][:2] == ["twitter", "whoami"]
    assert call["argv"][-1] == "--json"
    assert call["env"]["TWITTER_AUTH_TOKEN"] == "cookie"
    assert call["env"]["TWITTER_CT0"] == "csrf"
    assert call["timeout"] == PROBE_TIMEOUT


def test_a_refused_session_repeats_what_the_client_said(system_file, client, x_cli):
    """A dead cookie is the reading the button was pressed for, not a 500."""
    _with_session(system_file)
    x_cli.box.returncode = 1
    x_cli.box.stdout = ('{"ok": false, "schema_version": 1, "error": {"code": "unauthorized",'
                        ' "message": "Could not authenticate you"}}')
    body = client.post("/api/v1/system/x", json={"patch": {}}).json()
    assert body["ok"] is False
    assert "Could not authenticate you" in body["note"]


def test_a_session_that_is_not_configured_is_a_sentence_and_no_process(system_file, client, x_cli):
    """The fixture's file has `ct0: null`. Nothing is run, because there is nothing to run with,
    and the sentence names the variable the file would have to set."""
    body = client.post("/api/v1/system/x", json={"patch": {}}).json()
    assert body["ok"] is False
    assert "CH_X_CT0" in body["note"]
    assert x_cli.seen == []


def test_the_x_probe_reads_the_draft_session_not_the_file(system_file, client, x_cli):
    """The same rule as the gateway's probes: a cookie typed but not saved is the cookie
    this button spends, or the operator would have to store it in order to test it."""
    x_cli.box.stdout = WHOAMI
    body = client.post("/api/v1/system/x",
                       json={"patch": {"x.auth_token": "typed", "x.ct0": "typed-csrf"}}).json()
    assert body["ok"] is True
    assert x_cli.seen[0]["env"]["TWITTER_AUTH_TOKEN"] == "typed"
    assert x_cli.seen[0]["env"]["TWITTER_CT0"] == "typed-csrf"


def test_the_x_probe_reads_every_session_the_page_holds(system_file, client, x_cli):
    """Two cookies are two questions: the answer names the row it came from, and the button
    spends each session's own cookie instead of always the first one's."""
    x_cli.box.stdout = WHOAMI
    body = client.post("/api/v1/system/x", json={"sessions": [
        {"label": "one", "auth_token": "a1", "ct0": "c1"},
        {"label": "two", "auth_token": "a2", "ct0": "c2"},
    ]}).json()
    assert [line["label"] for line in body["lines"]] == ["one", "two"]
    assert body["ok"] is True
    assert sorted(call["env"]["TWITTER_AUTH_TOKEN"] for call in x_cli.seen) == ["a1", "a2"]


def test_a_session_with_no_cookie_is_not_probed(system_file, client, x_cli):
    """A half-filled row is not a session yet, and the button says so instead of spending the
    stored cookie under the row's label."""
    body = client.post("/api/v1/system/x",
                       json={"sessions": [{"label": "one"}]}).json()
    assert body["ok"] is False
    assert body["lines"] == []
    assert "没有可测试的会话" in body["note"]
    assert x_cli.seen == []


def test_renaming_a_session_keeps_the_cookies_its_blanks_stand_for(system_file, client):
    """A rename sends the label the row was loaded under, so the blank boxes still find their
    cookie. Matching on the new label alone answered every rename with "needs auth_token and
    ct0", which is the one thing a rename cannot be asked to supply."""
    _with_session(system_file)
    answer = client.put("/api/v1/system", json={"patch": {}, "sessions": [
        {"label": "renamed", "was_label": "main", "auth_token": "", "ct0": "", "enabled": True},
    ]})
    assert answer.status_code == 200, answer.text
    assert [row["label"] for row in answer.json()["sessions"]] == ["renamed"]
    text = system_file.read_text(encoding="utf-8")
    assert "auth_token: cookie" in text
    assert "ct0: csrf" in text


def test_a_typo_in_the_x_patch_is_refused_before_anything_is_called(system_file, client, x_cli):
    answer = client.post("/api/v1/system/x", json={"patch": {"x.nope": "1"}})
    assert answer.status_code == 422
    assert "x.nope" in answer.json()["detail"]
    assert x_cli.seen == []


def test_a_client_that_is_not_installed_is_a_sentence(system_file, client, monkeypatch):
    """The button's answer when `twitter` is not on PATH is that sentence, not a traceback."""
    _with_session(system_file)

    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr("app.search.engines.xsearch.subprocess.run", missing)
    body = client.post("/api/v1/system/x", json={"patch": {}}).json()
    assert body["ok"] is False
    assert "twitter" in body["note"]


def test_a_client_that_hangs_past_its_budget_is_a_sentence(system_file, client, monkeypatch):
    """The probe wears PROBE_TIMEOUT, not the round's 120s backstop: a button has to fail."""
    _with_session(system_file)

    def hanging(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr("app.search.engines.xsearch.subprocess.run", hanging)
    body = client.post("/api/v1/system/x", json={"patch": {}}).json()
    assert body["ok"] is False
    assert "30" in body["note"]
