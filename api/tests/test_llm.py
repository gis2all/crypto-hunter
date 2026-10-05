"""The LLM layer without a network.

Two things here belong to this repo and neither needs a model: where the endpoint settings
come from, and which failures are worth another attempt. A test that called a real model
would be a test of somebody else uptime.
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from app import cancel, config, llm
from app.llm import client as llm_client
from app.store import db

ENV_KEYS = (
    "CH_LLM_BASE_URL",
    "CH_LLM_API_KEY",
    "CH_LLM_API_KEY_FALLBACK",
    "CH_LLM_MODEL",
    "CH_LLM_TIMEOUT",
    "CH_LLM_MAX_TOKENS",
    "CH_LLM_REASONING",
    "CH_LLM_TRIES",
    "CH_LLM_KEY_COOLDOWN",
    "CH_LLM_EXTRA_HEADERS",
)


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """A stand-in for config/system.yaml plus a clean environment around it.

    The real file holds a real key, so a test that let `settings()` read it would leak the
    operator's credentials into the test process and would depend on their gateway.
    """
    path = tmp_path / "system.yaml"
    monkeypatch.setattr(config, "SYSTEM_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    # The cooldown of a refused key is process-local state, so a test that left one behind
    # would silently skip a key in the next test. A fresh map per test is the whole fix.
    monkeypatch.setattr(llm_client, "_DOWN", {})
    monkeypatch.setattr(llm_client, "_ANSWERED_BY", {})
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return path


def _write(path, **values):
    """Write the variables the tests below name into a `system.yaml`.

    The call sites talk in the variables `settings()` reads, because that is the level they are
    testing; mapping them back into the file's own paths happens here, off the one table.
    """
    by_env = {env: key for key, env, _kind in config.SYSTEM_KEYS}
    document: dict = {}
    for name, value in values.items():
        parts = by_env[name].split(".")
        node = document
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def _ok(text):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 1}},
        request=httpx.Request("POST", "https://endpoint/v1/chat/completions"),
    )


def _error(status):
    return httpx.Response(
        status,
        json={"error": {"message": "nope"}},
        request=httpx.Request("POST", "https://endpoint/v1/chat/completions"),
    )


class _Streamed:
    """One answer, as `httpx.Client.stream` opens it.

    The client reads the body in chunks so a stop can abandon an answer that is still being
    written, and a synthetic `httpx.Response` refuses to be streamed at all (`iter_raw`
    raises `StreamConsumed` on a response that was never sent). So the fake hands the body
    out in two chunks, and `on_chunk` is what a test hooks to act between them.

    The two chunk readers are deliberately different: `iter_raw` gzips, because the gateway
    does, and `iter_bytes` decodes, because httpx does. A client that reads the socket bytes
    and calls them JSON fails every test in this file instead of only in production.
    """

    def __init__(self, response, on_chunk=None):
        self._response = response
        self._on_chunk = on_chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def status_code(self):
        return self._response.status_code

    @property
    def request(self):
        return self._response.request

    @property
    def text(self):
        return self._response.text

    def read(self):
        return self._response.read()

    def iter_raw(self):
        """The socket's bytes: the gateway gzips every answer, so this is not JSON."""
        body = gzip.compress(self._response.read())
        half = max(1, len(body) // 2)
        yield body[:half]
        if self._on_chunk:
            self._on_chunk()
        yield body[half:]

    def iter_bytes(self):
        """The decoded bytes, which is what httpx undoes the content encoding into."""
        body = self._response.read()
        half = max(1, len(body) // 2)
        yield body[:half]
        if self._on_chunk:
            self._on_chunk()
        yield body[half:]


@pytest.fixture()
def calls(monkeypatch):
    """Keep every line of `complete` real except the socket."""
    sent: list[dict] = []
    replies: list[httpx.Response] = []
    ns = SimpleNamespace(sent=sent, replies=replies, on_chunk=None)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            sent.append({"url": url, "headers": headers or {}, "body": json or {}})
            reply = replies.pop(0) if replies else _ok("ok")
            return _Streamed(reply, ns.on_chunk)

        def post(self, url, headers=None, json=None):
            sent.append({"url": url, "headers": headers or {}, "body": json or {}})
            return replies.pop(0) if replies else _ok("ok")

    monkeypatch.setattr(llm_client.httpx, "Client", FakeClient)
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)
    return ns


def test_the_env_file_is_the_install_step_and_the_real_environment_wins(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://file/v1", CH_LLM_API_KEY="file-key",
           CH_LLM_MODEL="file-model", CH_LLM_MAX_TOKENS="1234")
    assert llm.configured()
    assert llm.settings()["base_url"] == "https://file/v1"
    assert llm.settings()["max_tokens"] == 1234

    env_file.unlink()
    assert not llm.configured()


def test_a_real_environment_variable_beats_the_file_even_on_a_second_read(env_file, monkeypatch):
    """The file is re-read on every call so an edit lands without a restart - which is
    exactly why the winning rule has to be the environment, not "first writer"."""
    _write(env_file, CH_LLM_BASE_URL="https://file/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    assert llm.settings()["base_url"] == "https://file/v1"

    _write(env_file, CH_LLM_BASE_URL="https://edited/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    assert llm.settings()["base_url"] == "https://edited/v1"

    monkeypatch.setenv("CH_LLM_BASE_URL", "https://real/v1")
    assert llm.settings()["base_url"] == "https://real/v1"


def test_extra_headers_are_json_and_a_broken_value_is_ignored(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m",
           CH_LLM_EXTRA_HEADERS='{"x-opencode-session": "crypto-hunter"}')
    assert llm.settings()["headers"] == {"x-opencode-session": "crypto-hunter"}

    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m",
           CH_LLM_EXTRA_HEADERS="not json")
    assert llm.settings()["headers"] == {}


def test_opencode_gateway_brings_its_own_session_header(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://opencode.ai/zen/go/v1", CH_LLM_API_KEY="k",
           CH_LLM_MODEL="m")
    assert llm.settings()["headers"] == {"x-opencode-session": "crypto-hunter"}


def test_an_explicit_header_beats_the_gateway_preset(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://opencode.ai/zen/go/v1", CH_LLM_API_KEY="k",
           CH_LLM_MODEL="m", CH_LLM_EXTRA_HEADERS='{"x-opencode-session": "mine"}')
    assert llm.settings()["headers"] == {"x-opencode-session": "mine"}


def test_other_gateways_get_no_preset_headers(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    assert llm.settings()["headers"] == {}


def test_the_gateways_errors_are_retried_and_a_dead_key_is_not(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    calls.replies.extend([_error(503), _ok("second try")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "second try"
    assert len(calls.sent) == 2

    calls.sent.clear()
    calls.replies.append(_error(401))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe",
                     attempt_tag="dead-key")
    assert len(calls.sent) == 1


def test_an_empty_message_is_a_failure_not_an_answer(env_file, calls, isolated_db):
    """A reasoning model that runs out of room answers with nothing at all. Storing that
    would put an empty verdict on a card and call it written."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    calls.replies.append(_ok(""))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")


def test_a_stop_abandons_an_answer_that_is_still_being_written(env_file, calls, isolated_db):
    """The longest single thing in a round is one model answer, so a stop has to be able to
    give up on it - and the abandoned call must not be filed as a model that refused, or the
    round would quietly publish without its words."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    calls.replies.append(_ok("half a sentence"))
    calls.on_chunk = lambda: cancel.request("run-1")
    try:
        with pytest.raises(cancel.RoundCancelled):
            llm.complete([{"role": "user", "content": "hi"}], purpose="probe", run_id="run-1")
    finally:
        cancel.clear("run-1")
    # One attempt: a stopped call is not a transient failure worth retrying.
    assert len(calls.sent) == 1
    conn = db.connect()
    try:
        row = conn.execute("SELECT ok, error FROM llm_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["ok"] == 0
    assert "RoundCancelled" in row["error"]


def test_the_request_carries_the_configured_ceiling_and_headers(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1/", CH_LLM_API_KEY="k", CH_LLM_MODEL="m",
           CH_LLM_MAX_TOKENS="8000", CH_LLM_EXTRA_HEADERS='{"x-opencode-session": "s"}')
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", json_mode=True)
    request = calls.sent[0]
    assert request["url"] == "https://e/v1/chat/completions"
    assert request["body"]["max_tokens"] == 8000
    assert request["body"]["model"] == "m"
    assert request["body"]["response_format"] == {"type": "json_object"}
    assert request["headers"]["Authorization"] == "Bearer k"
    assert request["headers"]["x-opencode-session"] == "s"


def test_a_gzipped_answer_is_decoded_not_read_off_the_socket(env_file, calls, isolated_db):
    """The gateway gzips every answer, so the socket's bytes are not JSON.

    Reading the raw chunks cost a UnicodeDecodeError on every real call while the fakes -
    which used to hand out the plain body - stayed green. The fake compresses now, and this
    asserts on the text, so the two can only agree on the decoded path.
    """
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    calls.replies.append(_ok("decoded on the way in"))
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == \
        "decoded on the way in"


def test_the_key_list_is_comma_separated_primary_first_and_deduplicated(env_file):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="  primary , standby ,primary", CH_LLM_API_KEY_FALLBACK="third, standby")
    keys = llm.settings()["api_keys"]
    assert keys == ["primary", "standby", "third"]
    assert llm.settings()["api_key"] == "primary"


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_a_key_error_hands_the_request_to_the_next_key(env_file, calls, isolated_db, status):
    """A dead credential, an exhausted quota and an account out of funds are the failures a
    standby key fixes, and they are the one failure this list exists for. 402 is here
    because a drained account answers it and the balance is the key's, not the request's."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    calls.replies.extend([_error(status), _ok("from the standby")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == \
        "from the standby"
    assert [call["headers"]["Authorization"] for call in calls.sent] == \
        ["Bearer primary", "Bearer standby"]


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_request_that_is_wrong_is_not_paid_for_on_another_key(env_file, calls, isolated_db,
                                                                 status):
    """An unknown model refuses every key the same way, so walking the list would only
    spend the standbys too."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    calls.replies.extend([_error(status)] * 4)
    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert len(calls.sent) == 1
    assert calls.sent[0]["headers"]["Authorization"] == "Bearer primary"


def test_every_key_refusing_is_llmunavailable_not_a_partial_answer(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby,third")
    calls.replies.extend([_error(401), _error(429), _error(403)])
    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert len(calls.sent) == 3


def test_a_refused_key_is_skipped_on_the_next_call(env_file, calls, isolated_db):
    """Without the cooldown a dead primary costs one refused request at every step of a
    round. With it, the refusal is paid once and the standby answers from then on."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    calls.replies.extend([_error(401), _ok("first")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "first"
    calls.replies.append(_ok("second"))
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe",
                        attempt_tag="second") == "second"
    assert len(calls.sent) == 3
    assert calls.sent[2]["headers"]["Authorization"] == "Bearer standby"


def test_the_cooldown_can_be_turned_off(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby", CH_LLM_KEY_COOLDOWN="0")
    calls.replies.extend([_error(401), _ok("first")])
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", attempt_tag="first")
    calls.replies.extend([_error(401), _ok("second")])
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", attempt_tag="second")
    assert [call["headers"]["Authorization"] for call in calls.sent] == \
        ["Bearer primary", "Bearer standby", "Bearer primary", "Bearer standby"]


def test_a_stop_is_not_a_reason_to_try_the_next_key(env_file, calls, isolated_db):
    """A stopped round must not look like a refused key, or the standby would write an
    answer to a question the operator already abandoned."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    calls.replies.append(_ok("half a sentence"))
    calls.on_chunk = lambda: cancel.request("run-keys")
    try:
        with pytest.raises(cancel.RoundCancelled):
            llm.complete([{"role": "user", "content": "hi"}], purpose="probe",
                         run_id="run-keys")
    finally:
        cancel.clear("run-keys")
    assert len(calls.sent) == 1


def test_the_thinking_budget_is_sent_only_when_the_operator_set_one(env_file, calls,
                                                                 isolated_db):
    """`CH_LLM_REASONING` is the A/B knob on this endpoint own thinking budget.

    This gateway answers at a high effort by default and bills the thinking as output:
    measured 2026-09-28, one research prompt spent 889 of its 1255 output tokens thinking.
    An unset knob has to send nothing at all - the request every call made before the knob
    existed - and a set one has to travel as `reasoning_effort`, which is the field the
    gateway read (measured: `enable_thinking: false` also works, `reasoning_effort: "none"`
    drops reasoning tokens to 0).
    """
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", attempt_tag="unset")
    assert "reasoning_effort" not in calls.sent[0]["body"]

    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m",
           CH_LLM_REASONING="none")
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", attempt_tag="set")
    assert calls.sent[1]["body"]["reasoning_effort"] == "none"

def _ok_responses(text):
    """One answer in the Responses shape, as the gateway writes it: the text is a part of
    type `output_text` inside an item of type `message`, and the bill counts input/output."""
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {"type": "reasoning", "status": "completed"},
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": text, "annotations": []}]},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
        request=httpx.Request("POST", "https://endpoint/v1/responses"),
    )


def _protocol_error():
    """The 400 a model that answers only on the other endpoint gives."""
    return httpx.Response(
        400,
        json={"type": "error", "error": {"type": "ModelProtocolUnsupported",
                                         "message": "Model does not support this protocol."}},
        request=httpx.Request("POST", "https://endpoint/v1/chat/completions"),
    )


def _workspace_error():
    """The 400 that is about the account: the workspace has not allowed a model which trains
    on request data. The envelope is a server_error and the words are inside the message."""
    return httpx.Response(
        400,
        json={"error": {"type": "server_error",
                        "message": "Upstream request failed: This Go model trains on request "
                                   "data. Allow paid endpoints that train on request data in "
                                   "your workspace Privacy settings to use it."}},
        request=httpx.Request("POST", "https://endpoint/v1/chat/completions"),
    )


def test_a_model_that_refuses_chat_is_asked_in_the_responses_shape(env_file, calls,
                                                                   isolated_db):
    """The gateway serves two shapes and a model answers only one of them. Measured
    2026-10-02: `muse-spark-1.3-contributor` answers 400 ModelProtocolUnsupported to a chat
    completion and 200 to the same question on /responses, so a client that knew only the
    first shape reported a working model as broken."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.extend([_protocol_error(), _ok_responses("from the other shape")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == \
        "from the other shape"
    assert [call["url"] for call in calls.sent] == \
        ["https://e/v1/chat/completions", "https://e/v1/responses"]


def test_the_shape_that_answered_is_remembered_so_the_guess_is_paid_once(env_file, calls,
                                                                        isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.extend([_protocol_error(), _ok_responses("first")])
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    calls.replies.append(_ok_responses("second"))
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe",
                        attempt_tag="second") == "second"
    assert [call["url"] for call in calls.sent] == \
        ["https://e/v1/chat/completions", "https://e/v1/responses", "https://e/v1/responses"]


def test_the_second_shape_carries_the_system_prompt_and_the_nested_effort(env_file, calls,
                                                                         isolated_db):
    """Two translations that are not cosmetic. The system prompt is `instructions` in this
    shape; dropping it would ask a model to judge a token with no instructions at all. And the
    thinking budget is nested `reasoning.effort` - the flat `reasoning_effort` the chat shape
    takes answers 400 unknown parameter (measured 2026-10-02) - where `none` is not in the
    family vocabulary and travels as the smallest effort it does accept."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k",
           CH_LLM_REASONING="none")
    calls.replies.extend([_protocol_error(), _ok_responses("ok")])
    llm.complete([{"role": "system", "content": "be terse"},
                  {"role": "user", "content": "hi"}], purpose="probe")
    body = calls.sent[1]["body"]
    assert body["instructions"] == "be terse"
    assert body["input"] == [{"role": "user", "content": "hi"}]
    assert body["reasoning"] == {"effort": "minimal"}
    assert "reasoning_effort" not in body


def test_a_workspace_refusal_is_a_key_problem_and_the_standby_answers(env_file, calls,
                                                                     isolated_db):
    """The 400 that says the workspace has not allowed a model which trains on request data is
    about the account, not the request - and an account belongs to a key. Measured 2026-10-02:
    the same call was refused for the primary key with this text and answered 200 for the
    standby, so treating it as the request fault is what kept a working key unused."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    calls.replies.extend([_workspace_error(), _protocol_error(), _ok_responses("from standby")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "from standby"
    assert [call["headers"]["Authorization"] for call in calls.sent] == \
        ["Bearer primary", "Bearer standby", "Bearer standby"]


def test_a_probe_reports_the_shape_that_answered(env_file, calls, isolated_db):
    """The button an operator presses must not call a served model broken."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.extend([_protocol_error(), _ok_responses("ok")])
    out = llm_client.probe(llm.settings())
    assert out["error"] is None
    assert out["protocol"] == "responses"
    assert out["chars"] == 2
    assert out["finish_reason"] == "completed"


def test_a_content_refusal_is_its_own_failure_not_a_key_problem(env_file, calls, isolated_db):
    """The material, not the credential (AIHOT's `isContentFilter`). Rotating to the standby
    would buy the same refusal twice, and the writing stages need to tell this apart from "the
    endpoint is broken": it is the one failure they may answer with the cheaper tier."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m",
           CH_LLM_API_KEY="primary,standby")
    body = {"error": {"code": "1301", "message": "contentFilter"}}
    calls.replies.extend([httpx.Response(
        400, json=body, request=httpx.Request("POST", "https://e/v1/chat/completions"))] * 4)
    with pytest.raises(llm.ContentRefused):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert len(calls.sent) == 1
    assert calls.sent[0]["headers"]["Authorization"] == "Bearer primary"


def test_a_filtered_finish_reason_is_content_refused_too(env_file, calls, isolated_db):
    """Some gateways answer 200 with an empty message and `finish_reason=content_filter`.
    Reading that as a generic empty answer would hide the one case the caller handles."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(httpx.Response(
        200,
        json={"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]},
        request=httpx.Request("POST", "https://e/v1/chat/completions")))
    with pytest.raises(llm.ContentRefused):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")


def test_a_generic_empty_message_is_still_not_a_content_refusal(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(httpx.Response(
        200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        request=httpx.Request("POST", "https://e/v1/chat/completions")))
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert not isinstance(caught.value, llm.ContentRefused)


@pytest.mark.parametrize("answer,want", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ("好的, 这是结果:\n{\"a\": 1}\n希望有用", {"a": 1}),
    ('{"a": "line one\nline two"}', {"a": "line one\nline two"}),
    ('{"a": "tab\there"}', {"a": "tab\there"}),
])
def test_the_json_tolerance_matches_aihot(env_file, calls, isolated_db, answer, want):
    """AIHOT's `extractJson`: a fence, prose around the object, and raw control characters
    inside strings - multi-line posts are the material, not an exotic case."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(_ok(answer))
    assert llm.complete_json([{"role": "user", "content": "hi"}], purpose="probe") == want


def test_something_that_is_not_json_at_all_is_still_a_failure(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(_ok("no braces here"))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json([{"role": "user", "content": "hi"}], purpose="probe")


def test_a_call_ceiling_beats_the_configured_one(env_file, calls, isolated_db):
    """The per-stage ceiling wins over the operator's global number (AIHOT's call table): one
    understanding answer writes three fields, and a global 512 would cut it in half."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k",
           CH_LLM_MAX_TOKENS="512")
    calls.replies.append(_ok("hi"))
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe", max_tokens=16384)
    assert calls.sent[0]["body"]["max_tokens"] == 16384
    calls.replies.append(_ok("hi"))
    llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert calls.sent[1]["body"]["max_tokens"] == 512

def test_a_request_the_model_already_answered_is_not_paid_twice(env_file, calls, isolated_db):
    """Receipts: re-running a round replays the saved answer instead of buying it again."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(_ok("first"))
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "first"
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "first"
    assert len(calls.sent) == 1, "同一个逻辑请求只该付一次钱"


def test_an_attempt_tag_buys_the_second_scoring_call(env_file, calls, isolated_db):
    """Two independent scores of one material: without their own tags the second would be the
    first answer replayed, and the mean would be one number twice."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(_ok('{"attentionScore": 80}'))
    calls.replies.append(_ok('{"attentionScore": 90}'))
    first = llm.complete_json([{"role": "user", "content": "hi"}], purpose="score",
                              attempt_tag="score-1")
    second = llm.complete_json([{"role": "user", "content": "hi"}], purpose="score",
                               attempt_tag="score-2")
    assert (first, second) == ({"attentionScore": 80}, {"attentionScore": 90})
    assert len(calls.sent) == 2


def test_a_draft_that_will_not_parse_is_released_for_a_later_attempt(env_file, calls,
                                                                     isolated_db):
    """The call was paid for, but its answer is unusable: the receipt must not replay it."""
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_MODEL="m", CH_LLM_API_KEY="k")
    calls.replies.append(_ok("no braces here"))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json([{"role": "user", "content": "hi"}], purpose="probe")

    calls.replies.append(_ok('{"attentionScore": 77}'))
    assert llm.complete_json([{"role": "user", "content": "hi"}],
                             purpose="probe") == {"attentionScore": 77}
    assert len(calls.sent) == 2, "用不了的答案放行, 而不是重放"
