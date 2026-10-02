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

import hashlib
import json
import logging
import os
import time
from typing import Any

import httpx

from .. import cancel, config
from ..store import db

log = logging.getLogger("crypto-hunter.llm")

#: How many times one completion is attempted. Reasoning models sit behind gateways that
#: answer 503 when no upstream worker is free, and a round that spent minutes collecting
#: facts should not lose every written word to one unavailable minute.
DEFAULT_TRIES = 2

#: For how long a key that refused is skipped. The refusal to plan for is a dead or
#: exhausted credential, and paying a rejected request at the head of every step of a
#: round is how a key list turns a five minute round into a ten minute one.
DEFAULT_KEY_COOLDOWN = 600.0

#: The statuses that are about the key rather than about the request. A dead credential,
#: an exhausted quota and an account that ran out of funds answer the same way however
#: many times they are asked, and a standby key is the fix. A 401/403/429/402 is the
#: account’s balance, not the request’s shape. Every other 4xx - an unknown model, a
#: malformed body - fails the same way on every key, so walking the list would only spend
#: the standbys too.
KEY_STATUSES = frozenset({401, 402, 403, 429})

#: Key fingerprint -> the monotonic time it may be used again. Process-local on purpose:
#: a quota comes back, and a restart is the operator saying try the primary once more.
_DOWN: dict[str, float] = {}


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
    keys = _api_keys()
    return {
        "base_url": os.environ.get("CH_LLM_BASE_URL", "").strip().rstrip("/"),
        "api_key": keys[0] if keys else "",
        "api_keys": keys,
        "model": os.environ.get("CH_LLM_MODEL", "").strip(),
        "timeout": float(os.environ.get("CH_LLM_TIMEOUT", "180")),
        "max_tokens": int(os.environ.get("CH_LLM_MAX_TOKENS", "0") or 0),
        # How hard the model is allowed to think before it answers. Unset leaves the
        # endpoint at its own default, which is what every call before this knob did.
        "reasoning": os.environ.get("CH_LLM_REASONING", "").strip(),
        "tries": max(1, int(os.environ.get("CH_LLM_TRIES", str(DEFAULT_TRIES)) or DEFAULT_TRIES)),
        "key_cooldown": float(
            os.environ.get("CH_LLM_KEY_COOLDOWN", str(DEFAULT_KEY_COOLDOWN)) or 0
        ),
        "headers": headers,
    }


def keys_in(*values: str) -> list[str]:
    """The keys these fields name, the primary first, in the order they should be tried.

    Comma-separated on one line, because that is the shape an env file makes easy: a standby
    appended to `CH_LLM_API_KEY` needs no second variable. Duplicates are dropped - the same key
    twice is one key and one wasted refusal. The environment is not the only place a list of
    keys comes from: the settings page's two rows are read through here as well, or the page
    would act as if the 备用 Key row did not exist (8.9.7).
    """
    keys: list[str] = []
    for value in values:
        for part in (value or "").split(","):
            part = part.strip()
            if part and part not in keys:
                keys.append(part)
    return keys


def _api_keys() -> list[str]:
    """Every configured key, read from the environment (the file has already been applied)."""
    return keys_in(os.environ.get("CH_LLM_API_KEY", ""),
                   os.environ.get("CH_LLM_API_KEY_FALLBACK", ""))


def _fingerprint(api_key: str) -> str:
    """A key's callsign for the log and the call record - never the key itself.

    The log is the thing an operator pastes into a bug report, so it gets something that
    identifies a key across two lines without being a credential.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]


def _mark_down(api_key: str, cooldown: float) -> None:
    if cooldown > 0:
        _DOWN[_fingerprint(api_key)] = time.monotonic() + cooldown


def _usable(keys: list[str], cooldown: float) -> list[str]:
    """The keys worth a request right now, in order.

    If every key is cooling the cooldown is ignored: a round has to try something, and the
    operator may have topped the quota up since the last refusal.
    """
    if cooldown <= 0:
        return list(keys)
    now = time.monotonic()
    warm = [key for key in keys if _DOWN.get(_fingerprint(key), 0.0) <= now]
    return warm or list(keys)


def _configured(current: dict) -> bool:
    return bool(current["base_url"] and current["api_keys"] and current["model"])


class LLMUnavailable(RuntimeError):
    """No endpoint configured, or the endpoint refused. Never a reason to invent text."""


def configured() -> bool:
    return _configured(settings())


#: How long a probe waits. Short because a person is watching a button, and an endpoint that
#: needs longer than this is something to learn from a round rather than from a form.
PROBE_TIMEOUT = 20.0

#: The most a probe may spend. It answers one word; the ceiling is what keeps an endpoint left
#: on its own (high) reasoning default from billing a form visit like a research call.
PROBE_MAX_TOKENS = 256

#: How many keys one press of 测试连接 spends a call on. The page has two rows and a row may
#: hold a comma list, so this forgives a third and stops counting there.
PROBE_MAX_KEYS = 3


def _probe_headers(current: dict, key: str | None = None) -> dict:
    headers = dict(current["headers"])
    headers["Authorization"] = "Bearer %s" % (key or current["api_key"])
    return headers


def list_models(current: dict) -> list[str]:
    """The model ids the endpoint says it serves, or `LLMUnavailable` in its own words.

    One GET, the primary key, no retries and no cooldown: this answers a button. A key the
    gateway refuses is refused *for the probe* - a round has its own opinion and its own
    bookkeeping - so nothing here touches `_DOWN` or `llm_calls`.
    """
    if not current["base_url"]:
        raise LLMUnavailable("还没有填网关地址")
    if not current["api_key"]:
        raise LLMUnavailable("还没有填 API Key")
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            resp = client.get("%s/models" % current["base_url"], headers=_probe_headers(current))
    except httpx.HTTPError as exc:
        raise LLMUnavailable("连不上 %s：%s" % (current["base_url"], exc)) from exc
    if resp.status_code >= 400:
        raise LLMUnavailable("网关返回 %d：%s" % (resp.status_code, resp.text[:200]))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LLMUnavailable("网关回的不是 JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise LLMUnavailable("这个端点没有 /models 列表，模型名请手填")
    ids = sorted({str(item["id"]) for item in data
                  if isinstance(item, dict) and item.get("id")})
    if not ids:
        raise LLMUnavailable("端点返回 0 个模型")
    return ids


def probe(current: dict, key: str | None = None) -> dict:
    """One really small real call, and the readings it takes to judge the answer.

    `/models` proves a door opens; it does not prove the model answers. The trap this repo
    already paid for is an endpoint that answers 200 with an *empty* message because the
    thinking ate the ceiling (6.4.8) - a form that only checked reachability would call that
    configuration healthy. So this asks for one word and reports whether text came back.

    Never raises for the gateway's own answers (a refusal is a reading, not a crash) and never
    records: `llm_calls` is a round's bill, and a button is not a round. `key` is the credential
    to spend, the primary when the caller does not say; asking a key neither reads nor writes
    `_DOWN`, so a key resting from a round is still the key this button tests.
    """
    out: dict = {"status": None, "latency_ms": 0, "chars": 0, "finish_reason": "", "error": None}
    if not _configured(current):
        out["error"] = "网关地址、API Key、模型三样都要填上"
        return out
    body: dict = {"model": current["model"], "temperature": 0, "max_tokens": PROBE_MAX_TOKENS,
                  "messages": [{"role": "user", "content": "ping"}]}
    if current["reasoning"]:
        body["reasoning_effort"] = current["reasoning"]
    started = time.time()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            resp = client.post("%s/chat/completions" % current["base_url"],
                               headers=_probe_headers(current, key), json=body)
    except httpx.HTTPError as exc:
        out["latency_ms"] = int((time.time() - started) * 1000)
        out["error"] = "连不上 %s：%s" % (current["base_url"], exc)
        return out
    out["status"] = resp.status_code
    out["latency_ms"] = int((time.time() - started) * 1000)
    if resp.status_code >= 400:
        out["error"] = "网关返回 %d：%s" % (resp.status_code, resp.text[:200])
        return out
    try:
        payload = resp.json()
    except ValueError:
        out["error"] = "网关回的不是 JSON"
        return out
    try:
        choice = payload["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        out["error"] = "回包里没有 choices[0].message.content"
        return out
    out["chars"] = len(str(text).strip())
    out["finish_reason"] = str(choice.get("finish_reason") or "")
    if not out["chars"]:
        out["error"] = ("模型答了，但正文是空的（finish_reason=%s）—— 思考吃光了额度，"
                        "要么把「回答上限」调大，要么把「推理档位」设成不思考"
                        % (out["finish_reason"] or "?"))
    return out


def probe_keys(current: dict) -> list[dict]:
    """One minimal call per configured key, in the order a round would try them.

    The standby is the key nobody has ever watched answer: a round meets it for the first time
    at the moment the primary is refused, which is the worst moment to learn it is bad too.
    `api_keys` is a round's order, and the walking stops at `PROBE_MAX_KEYS` calls.
    """
    return [probe(current, key) for key in current["api_keys"][:PROBE_MAX_KEYS]]


def _post(current: dict, headers: dict, body: dict, timeout: float | None,
          run_id: str | None = None) -> dict:
    """One POST to the gateway, retried only while the failure looks transient.

    A 4xx is the request's own fault - a dead key, an unknown model - and retrying it just
    bills the operator twice. A 5xx or a transport error is the gateway's, and that is
    worth another attempt inside a round that is already minutes long.

    The answer is read in decoded chunks rather than in one call, and that is the only
    reason this goes through `stream`: the longest thing in a round is one model answer,
    and a round the operator has stopped must not sit waiting out an answer nobody will
    read. The chunks are `iter_bytes`, not `iter_raw`: the gateway answers gzipped and a
    raw chunk is not JSON. A stop check per chunk turns the abandon into a closed
    connection, and `RoundCancelled` - not `LLMUnavailable` - is what travels back up, so
    the writing stages can tell "stopped" from "the endpoint refused". `stream` changes
    nothing about the request: the body is a plain chat completion, the response is read
    as it arrives.
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
                    for chunk in resp.iter_bytes():
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


def _call_payload(current: dict, body: dict, timeout: float | None,
                  run_id: str | None) -> dict:
    """One POST, walked across the key list until one answers.

    The only failure a standby key fixes is a refusal that is about the key: a dead
    credential or an exhausted quota (401/403/429). That key is put in cooldown and the
    next one gets the same request. Everything else - a 5xx the gateway already had its
    retries for, a transport error, a 4xx that is the request's own fault, a stop - is
    raised as it is, because a different key would answer exactly the same way.
    """
    ordered = _usable(current["api_keys"], current["key_cooldown"])
    for index, api_key in enumerate(ordered):
        headers = dict(current["headers"])
        headers["Authorization"] = "Bearer %s" % api_key
        headers["Content-Type"] = "application/json"
        try:
            return _post(current, headers, body, timeout, run_id)
        except cancel.RoundCancelled:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in KEY_STATUSES:
                raise
            _mark_down(api_key, current["key_cooldown"])
            if index + 1 == len(ordered):
                raise
            log.warning(
                "llm: key %s refused with %s (%d of %d), trying the next",
                _fingerprint(api_key), status, index + 1, len(ordered),
            )
    raise RuntimeError("no key was tried")


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
        raise LLMUnavailable(
            "CH_LLM_BASE_URL / CH_LLM_API_KEY / CH_LLM_MODEL are not all set"
        )
    model = model or current["model"]
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    # Reasoning models bill their thinking against this ceiling, so a tight one returns an
    # empty message with `finish_reason = length` instead of an error.
    if current["max_tokens"]:
        body["max_tokens"] = current["max_tokens"]
    # The thinking budget, when the operator set one. This endpoint answers at a high
    # effort by default and bills the thinking as completion tokens: on the real
    # research path, five tokens took 340.1s against 94.9s with `reasoning_effort:
    # "none"`, and only the thinking-on arm lost a card. `minimal` and `low` are not
    # switches - both still spent 680-899 thinking tokens where `none` spent 0.
    # The field is the gateway own, and an unset `CH_LLM_REASONING` sends nothing.
    if current["reasoning"]:
        body["reasoning_effort"] = current["reasoning"]
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    started = time.time()
    try:
        payload = _call_payload(current, body, timeout, run_id)
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
