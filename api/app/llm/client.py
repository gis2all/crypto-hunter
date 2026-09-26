"""OpenAI-compatible chat client, synchronous on purpose.

The pipeline is a sequence of blocking network calls - five sources, then per-token
questions, then the writing stage - and the HTTP layer above it is synchronous too.
Making this async would buy nothing and cost the pipeline a thread hop at every stage
boundary, so the client is a plain blocking call and `pipeline/schedule.py` keeps it off
the request thread.

Every call is recorded in `llm_calls` with the model, latency and token counts, so the
cost of a round is answerable after the fact.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from .. import cancel, config
from ..store import db

#: How many times one completion is attempted. Reasoning models sit behind gateways that
#: answer 503 when no upstream worker is free, and a round that spent minutes collecting
#: facts should not lose every written word to one unavailable minute.
DEFAULT_TRIES = 2


def settings() -> dict:
    """The endpoint, read fresh on every call.

    Per call rather than at import: that is what lets the env file be edited and picked up
    by the next round instead of the next restart. `config` owns the file, and the rule
    that a real environment variable beats it.
    """
    config.load_env_file()
    headers: dict[str, str] = {}
    raw = os.environ.get("CH_LLM_EXTRA_HEADERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            headers = {str(key): str(value) for key, value in parsed.items()}
    return {
        "base_url": os.environ.get("CH_LLM_BASE_URL", "").strip().rstrip("/"),
        "api_key": os.environ.get("CH_LLM_API_KEY", "").strip(),
        "model": os.environ.get("CH_LLM_MODEL", "").strip(),
        "timeout": float(os.environ.get("CH_LLM_TIMEOUT", "180")),
        "max_tokens": int(os.environ.get("CH_LLM_MAX_TOKENS", "0") or 0),
        "tries": max(1, int(os.environ.get("CH_LLM_TRIES", str(DEFAULT_TRIES)) or DEFAULT_TRIES)),
        "headers": headers,
    }


def _configured(current: dict) -> bool:
    return bool(current["base_url"] and current["api_key"] and current["model"])


class LLMUnavailable(RuntimeError):
    """No endpoint configured, or the endpoint refused. Never a reason to invent text."""


def configured() -> bool:
    return _configured(settings())


def _post(current: dict, headers: dict, body: dict, timeout: float | None,
          run_id: str | None = None) -> dict:
    """One POST, retried only while the failure looks transient.

    A 4xx is the request's own fault - a dead key, an unknown model - and retrying it just
    bills the operator twice. A 5xx or a transport error is the gateway's, and that is
    worth another attempt inside a round that is already minutes long.

    The answer is read in chunks rather than in one call, and that is the only reason this
    goes through `stream`: the longest thing in a round is one model answer, and a round the
    operator has stopped must not sit waiting out an answer nobody will read. A stop check
    per chunk turns the abandon into a closed connection, and `RoundCancelled` - not
    `LLMUnavailable` - is what travels back up, so the writing stages can tell "stopped" from
    "the endpoint refused". `stream` changes nothing about the request: the body is a plain
    chat completion, the response is read as it arrives.
    """
    tries = current["tries"]
    last: Exception | None = None
    for attempt in range(tries):
        try:
            if cancel.requested(run_id):
                raise cancel.RoundCancelled("stopped before attempt %d" % (attempt + 1))
            with httpx.Client(timeout=timeout or current["timeout"]) as client:
                with client.stream(
                    "POST", "%s/chat/completions" % current["base_url"],
                    headers=headers, json=body,
                ) as resp:
                    if resp.status_code >= 400:
                        resp.read()  # the reason travels in the body, and the record keeps it
                        raise httpx.HTTPStatusError(
                            "upstream %d: %s" % (resp.status_code, resp.text[:200]),
                            request=resp.request, response=resp,
                        )
                    chunks = bytearray()
                    for chunk in resp.iter_raw():
                        if cancel.requested(run_id):
                            raise cancel.RoundCancelled(
                                "stopped while %s was answering" % run_id
                            )
                        chunks.extend(chunk)
                    return json.loads(bytes(chunks))
        except cancel.RoundCancelled:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise
            last = exc
        except Exception as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(1.5 * (attempt + 1))
    raise last if last is not None else RuntimeError("no attempt was made")


def _record(*, purpose: str, run_id: str | None, model: str, prompt_version: str | None,
            latency_ms: int, prompt_tokens: int | None, completion_tokens: int | None,
            ok: bool, error: str | None) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO llm_calls (run_id, purpose, model, prompt_version, prompt_tokens,"
            " completion_tokens, latency_ms, ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, purpose, model, prompt_version, prompt_tokens, completion_tokens,
             latency_ms, 1 if ok else 0, error, db.now_iso()),
        )
    finally:
        conn.close()


def complete(
    messages: list[dict],
    *,
    purpose: str,
    run_id: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    temperature: float = 0.3,
    timeout: float | None = None,
    json_mode: bool = False,
) -> str:
    """One completion, or `LLMUnavailable`. The caller decides what a missing text means."""
    current = settings()
    if not _configured(current):
        raise LLMUnavailable("CH_LLM_BASE_URL / CH_LLM_API_KEY / CH_LLM_MODEL are not all set")
    model = model or current["model"]
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    # Reasoning models bill their thinking against this ceiling, so a tight one returns an
    # empty message with `finish_reason = length` instead of an error.
    if current["max_tokens"]:
        body["max_tokens"] = current["max_tokens"]
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": "Bearer %s" % current["api_key"],
        "Content-Type": "application/json",
    }
    headers.update(current["headers"])

    started = time.time()
    try:
        payload = _post(current, headers, body, timeout, run_id)
        text = payload["choices"][0]["message"].get("content")
        if not text or not str(text).strip():
            raise RuntimeError("the endpoint returned an empty message")
        usage = payload.get("usage") or {}
    except cancel.RoundCancelled as exc:
        # Recorded like any other call - a round that was stopped still spent what it spent -
        # then passed on as itself. Degrading here would be the bug: an abandoned answer must
        # not look like a model that refused, or the round would publish without its words.
        _record(purpose=purpose, run_id=run_id, model=model, prompt_version=prompt_version,
                latency_ms=int((time.time() - started) * 1000), prompt_tokens=None,
                completion_tokens=None, ok=False, error=repr(exc)[:400])
        raise
    except Exception as exc:
        _record(purpose=purpose, run_id=run_id, model=model, prompt_version=prompt_version,
                latency_ms=int((time.time() - started) * 1000), prompt_tokens=None,
                completion_tokens=None, ok=False, error=repr(exc)[:400])
        raise LLMUnavailable("%s: %r" % (purpose, exc)) from exc

    _record(purpose=purpose, run_id=run_id, model=model, prompt_version=prompt_version,
            latency_ms=int((time.time() - started) * 1000),
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            ok=True, error=None)
    return text


def complete_json(messages: list[dict], **kw: Any) -> Any:
    """A completion that must parse. A fenced code block around the JSON is tolerated;
    anything else is a failure, because guessing at a broken draft is how a report ends up
    saying something nobody decided."""
    text = complete(messages, json_mode=True, **kw).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except ValueError as exc:
        raise LLMUnavailable("draft was not JSON: %r" % (text[:120],)) from exc
