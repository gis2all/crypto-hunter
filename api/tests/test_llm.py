"""The LLM layer without a network.

Two things here belong to this repo and neither needs a model: where the endpoint settings
come from, and which failures are worth another attempt. A test that called a real model
would be a test of somebody else uptime.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import cancel, config, llm
from app.llm import client as llm_client
from app.store import db

ENV_KEYS = (
    "CH_LLM_BASE_URL",
    "CH_LLM_API_KEY",
    "CH_LLM_MODEL",
    "CH_LLM_TIMEOUT",
    "CH_LLM_MAX_TOKENS",
    "CH_LLM_TRIES",
    "CH_LLM_EXTRA_HEADERS",
)


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """A stand-in for config/llm.env plus a clean environment around it.

    The real file holds a real key, so a test that let `settings()` read it would leak the
    operator's credentials into the test process and would depend on their gateway.
    """
    path = tmp_path / "llm.env"
    monkeypatch.setattr(config, "LLM_ENV_PATH", path)
    monkeypatch.setattr(config, "_FROM_FILE", {})
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return path


def _write(path, **values):
    path.write_text("".join("%s=%s\n" % item for item in values.items()), encoding="utf-8")


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
    raises `StreamConsumed` on a response that was never sent). So the fake hands the same
    body out in two chunks, and `on_chunk` is what a test hooks to act between them.
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


def test_the_gateways_errors_are_retried_and_a_dead_key_is_not(env_file, calls, isolated_db):
    _write(env_file, CH_LLM_BASE_URL="https://e/v1", CH_LLM_API_KEY="k", CH_LLM_MODEL="m")
    calls.replies.extend([_error(503), _ok("second try")])
    assert llm.complete([{"role": "user", "content": "hi"}], purpose="probe") == "second try"
    assert len(calls.sent) == 2

    calls.sent.clear()
    calls.replies.append(_error(401))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "hi"}], purpose="probe")
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
