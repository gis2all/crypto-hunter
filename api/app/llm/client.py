"""OpenAI-compatible model client, synchronous on purpose.

The pipeline is a sequence of blocking network calls - five sources, then per-token
questions, then the writing stage - and the HTTP layer above it is synchronous too.
Making this async would buy nothing and cost the pipeline a thread hop at every stage
boundary, so the client is a plain blocking call and `pipeline/schedule.py` keeps it off
the request thread.

Every call is recorded in `llm_calls` with the model, latency and token counts, so the
cost of a round is answerable after the fact.

The gateway serves two request shapes and a model answers only one of them, so which
shape to ask in is a dimension this client walks the way it walks the key list - see
`PROTOCOLS`.
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

#: The two request shapes this gateway serves. Everything here spoke chat completions until
#: a model family arrived that answers only on the Responses API: asked for a chat completion
#: it answers 400 ``ModelProtocolUnsupported``, which is about the shape of the request and
#: not about the key, the quota or the model being wrong. So the shape is walked the way the
#: key list is, and the shape that answered is remembered per model. Measured 2026-10-02:
#: ``muse-spark-1.3-contributor`` serves ``/responses`` and refuses ``/chat/completions`` with
#: that error, on a key whose ``/models`` list includes it.
PROTOCOLS = ("chat", "responses")

#: What each shape is called on the wire. A shape and its path are one decision, so they are
#: written down together.
_PATHS = {"chat": "/chat/completions", "responses": "/responses"}

#: The word a 400 carries when the model does not speak the shape that was asked. The body is
#: ``{"type":"error","error":{"type":"ModelProtocolUnsupported","message":"Model does not
#: support this protocol."}}``; matching the word rather than the envelope survives the
#: gateway renaming its error type.
_PROTOCOL_MARKER = "protocol"

#: What a 400 says when the refusal is about the *account* and not about the request: the
#: model trains on request data and the workspace has not allowed that. A workspace belongs to
#: a key, so this is the one 400 a standby key does fix. Measured 2026-10-02: the same model on
#: the same endpoint was refused for the primary key with this text, and answered 200 for the
#: standby key.
_ACCOUNT_MARKERS = ("trains on request data", "privacy settings")

#: base_url + model -> the shape that answered it. Process-local, and successes only: a wrong
#: guess costs one refused call per process, and a gateway that starts serving the other shape
#: tomorrow is not held to today answer.
_ANSWERED_BY: dict[str, str] = {}


class _ProtocolRefused(Exception):
    """The model does not answer in the shape that was asked.

    Not a failed call - a reason to ask in the other shape - so it is neither recorded nor
    counted against a key.
    """


def _model_key(current: dict, model: str) -> str:
    return "%s|%s" % (current["base_url"], model)


def _protocols_for(current: dict, model: str) -> tuple[str, ...]:
    """The shapes to try, the one that last answered first.

    Chat first for a model nobody has asked yet: every model here spoke it before this client
    learned the other shape, so the first guess is free for all of them and costs one refused
    call for a model that does not.
    """
    remembered = _ANSWERED_BY.get(_model_key(current, model))
    if remembered:
        return (remembered,) + tuple(name for name in PROTOCOLS if name != remembered)
    return PROTOCOLS


def _endpoint(current: dict, protocol: str) -> str:
    return "%s%s" % (current["base_url"], _PATHS[protocol])


def _refusal_text(exc: httpx.HTTPStatusError) -> str:
    """The body of a refused call, in full.

    ``_post`` keeps 200 characters of it in its own message, and the words that say *whose*
    fault a 400 is can sit past that cut - the privacy refusal is a 160 character sentence
    inside a JSON envelope. So the shape and the account are read off the response itself.
    """
    response = exc.response
    if response is None:
        return ""
    try:
        return response.text
    except Exception:
        return ""


def _is_protocol_refusal(status: int | None, text: str) -> bool:
    return status == 400 and _PROTOCOL_MARKER in text.lower()


def _is_account_refusal(status: int | None, text: str) -> bool:
    if status != 400:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _ACCOUNT_MARKERS)


def _body(protocol: str, current: dict, model: str, messages: list[dict],
          temperature: float, json_mode: bool) -> dict:
    """One question in the shape the protocol expects.

    The two shapes are not a rename of each other, and the differences below were measured
    against the gateway on 2026-10-02 rather than read off a specification:

    - The system prompt is ``instructions`` here and a message there. This matters: the
      research prompt is a system message (6.4), so a shape that dropped it would ask the
      model to judge a token with no instructions at all.
    - The thinking budget is ``reasoning.effort``, nested. The flat ``reasoning_effort`` the
      chat shape takes answers 400 ``unknown parameter``.
    - ``none`` is not in this family vocabulary: it answers 400 ``reasoning_effort 'none' is
      not supported ... Supported values: [minimal, low, medium, high, xhigh, max]``. So "do
      not think" travels as the smallest effort the family accepts. Omitting the field is not
      the same thing - left alone the model spent 253 of a 256 token ceiling thinking and
      returned an empty message, which is the trap the research prompt already paid for once.
    - The ceiling is ``max_output_tokens``, and the demand for JSON is ``text.format``.
    """
    if protocol == "chat":
        body: dict = {"model": model, "messages": messages, "temperature": temperature}
        if current["max_tokens"]:
            body["max_tokens"] = current["max_tokens"]
        if current["reasoning"]:
            body["reasoning_effort"] = current["reasoning"]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    instructions: list[str] = []
    turns: list[dict] = []
    for message in messages:
        if message.get("role") == "system":
            instructions.append(str(message.get("content") or ""))
        else:
            turns.append({"role": message.get("role"), "content": message.get("content")})
    body = {"model": model, "input": turns, "temperature": temperature}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if current["max_tokens"]:
        body["max_output_tokens"] = current["max_tokens"]
    if current["reasoning"]:
        effort = current["reasoning"]
        body["reasoning"] = {"effort": "minimal" if effort == "none" else effort}
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    return body


def _read(protocol: str, payload: dict) -> dict:
    """The text, the finish reason and the bill, out of either shape.

    Strict about the container and quiet about the contents: a shape that arrives without its
    own output field is a broken answer, not an empty one, and the caller error has to be able
    to say which. Both shapes are read into one dict so nothing above this line has to know
    which endpoint wrote the words.
    """
    if protocol == "chat":
        choice = payload["choices"][0]
        message = choice["message"]
        usage = payload.get("usage") or {}
        return {
            "text": str(message.get("content") or ""),
            "finish_reason": str(choice.get("finish_reason") or ""),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
    output = payload["output"]
    usage = payload.get("usage") or {}
    incomplete = payload.get("incomplete_details") or {}
    return {
        "text": "".join(
            str(part.get("text") or "")
            for item in output
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("type") == "output_text"
        ),
        "finish_reason": str(incomplete.get("reason") or payload.get("status") or ""),
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
    }


#: Headers a gateway needs that no operator should have to type. The opencode gateway
#: tags traffic with a session header; bringing it is a fixed action of talking to that
#: gateway, not a per-operator decision, so the client brings it and what the operator
#: wrote (file or environment) still wins key by key.
_GATEWAY_PRESETS = (("opencode.ai", {"x-opencode-session": "crypto-hunter"}),)


def preset_headers(base_url: str, headers: dict[str, str]) -> dict[str, str]:
    """Fill in the headers the gateway at `base_url` requires but the operator did not write."""
    for host, preset in _GATEWAY_PRESETS:
        if host in base_url:
            for key, value in preset.items():
                headers.setdefault(key, value)
    return headers


def settings() -> dict:
    """The endpoint, read fresh on every call.

    Per call rather than at import: that is what lets the env file be edited and picked up
    by the next round instead of the next restart. `config` owns the file, and the rule
    that a real environment variable beats it.
    """
    config.load_env_file()
    base_url = os.environ.get("CH_LLM_BASE_URL", "").strip().rstrip("/")
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
    preset_headers(base_url, headers)
    return {
        "base_url": base_url,
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
    would act as if the 备用 Key row did not exist.
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


#: How long a probe waits. A person is watching a button, so this stays short - but not so
#: short that the button lies: the Responses shape of `muse-spark-1.3-contributor` answered in
#: 14.4s and 33.1s on two consecutive calls (measured 2026-10-02), and the 20.0s this used to
#: be outran the model and called a served endpoint unreachable.
PROBE_TIMEOUT = 60.0

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
    thinking ate the ceiling - a form that only checked reachability would call that
    configuration healthy. So this asks for one word and reports whether text came back.

    Never raises for the gateway's own answers (a refusal is a reading, not a crash) and never
    records: `llm_calls` is a round's bill, and a button is not a round. `key` is the credential
    to spend, the primary when the caller does not say; asking a key neither reads nor writes
    `_DOWN`, so a key resting from a round is still the key this button tests.

    Both shapes are asked, because the shape a model speaks is not in the
    `/models` list: a model that refuses chat completions is asked on the Responses
    endpoint before this button calls it broken.
    """
    out: dict = {"status": None, "latency_ms": 0, "chars": 0, "finish_reason": "",
                 "error": None, "protocol": ""}
    if not _configured(current):
        out["error"] = "网关地址、API Key、模型三样都要填上"
        return out
    trial = dict(current, max_tokens=PROBE_MAX_TOKENS)
    for protocol in _protocols_for(current, current["model"]):
        out, refused = _probe_shape(trial, key, protocol, out)
        if out["error"] is None or not refused:
            return out
    return out


def _probe_shape(current: dict, key: str | None, protocol: str,
                 out: dict) -> tuple[dict, bool]:
    """One probe call in one shape, read into `out`, plus whether the shape was refused.

    The button body, one shape at a time, so `probe` can walk the shapes the way `complete`
    does. `out` is filled in place and handed back: a caller sees the last attempt, and a
    refusal of the shape is the only reading worth overwriting with another attempt.
    """
    out["protocol"] = protocol
    out["status"] = None
    out["chars"] = 0
    out["finish_reason"] = ""
    out["error"] = None
    body = _body(protocol, current, current["model"],
                 [{"role": "user", "content": "ping"}], 0, False)
    started = time.time()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            resp = client.post(_endpoint(current, protocol),
                               headers=_probe_headers(current, key), json=body)
    except httpx.HTTPError as exc:
        out["latency_ms"] = int((time.time() - started) * 1000)
        out["error"] = "连不上 %s：%s" % (current["base_url"], exc)
        return out, False
    out["status"] = resp.status_code
    out["latency_ms"] = int((time.time() - started) * 1000)
    if resp.status_code >= 400:
        out["error"] = "网关返回 %d：%s" % (resp.status_code, resp.text[:200])
        return out, _is_protocol_refusal(resp.status_code, resp.text)
    try:
        payload = resp.json()
    except ValueError:
        out["error"] = "网关回的不是 JSON"
        return out, False
    try:
        reading = _read(protocol, payload)
    except (KeyError, IndexError, TypeError, AttributeError):
        out["error"] = "回包里没有 choices[0].message.content"
        return out, False
    out["chars"] = len(reading["text"].strip())
    out["finish_reason"] = reading["finish_reason"]
    if not out["chars"]:
        out["error"] = ("模型答了，但正文是空的（finish_reason=%s）—— 思考吃光了额度，"
                        "要么把「回答上限」调大，要么把「推理档位」设成不思考"
                        % (out["finish_reason"] or "?"))
    return out, False


def probe_keys(current: dict) -> list[dict]:
    """One minimal call per configured key, in the order a round would try them.

    The standby is the key nobody has ever watched answer: a round meets it for the first time
    at the moment the primary is refused, which is the worst moment to learn it is bad too.
    `api_keys` is a round's order, and the walking stops at `PROBE_MAX_KEYS` calls.
    """
    return [probe(current, key) for key in current["api_keys"][:PROBE_MAX_KEYS]]


def _post(current: dict, endpoint: str, headers: dict, body: dict,
          timeout: float | None, run_id: str | None = None) -> dict:
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
                    "POST", endpoint,
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


def _call_payload(current: dict, endpoint: str, body: dict, timeout: float | None,
                  run_id: str | None) -> dict:
    """One POST, walked across the key list until one answers.

    Three kinds of failure meet here, and they are not the same question:

    - About the key: a dead credential, an exhausted quota (401/403/429), and the one 400 that
      is about the account rather than the request - a workspace that has not allowed a model
      which trains on request data. That key goes into cooldown and the next one gets the same
      request, because a standby in another workspace answers it.
    - About the shape: a 400 that names the protocol. No key fixes a model that does not speak
      this shape, so it is raised as `_ProtocolRefused` for the caller to ask in the other one.
    - About the request: every other 4xx, a 5xx the gateway already had its retries for, a
      transport error, a stop. Raised as it is, because a second key would answer the same way.
    """
    ordered = _usable(current["api_keys"], current["key_cooldown"])
    for index, api_key in enumerate(ordered):
        headers = dict(current["headers"])
        headers["Authorization"] = "Bearer %s" % api_key
        headers["Content-Type"] = "application/json"
        try:
            return _post(current, endpoint, headers, body, timeout, run_id)
        except cancel.RoundCancelled:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            text = _refusal_text(exc)
            if _is_protocol_refusal(status, text):
                raise _ProtocolRefused("%s was refused with %s" % (endpoint, status)) from exc
            if status not in KEY_STATUSES and not _is_account_refusal(status, text):
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


def _answer(current: dict, model: str, messages: list[dict], temperature: float,
            json_mode: bool, timeout: float | None,
            run_id: str | None) -> tuple[str, dict]:
    """One answer in whichever shape the model speaks, and the shape that wrote it.

    A model that refuses the shape is not a failed call: the other shape costs one more request
    and the round keeps its words. Which shape answered is remembered, so the walk happens once
    per process rather than once per prompt - and the memory is per model, because one gateway
    serves both kinds here.

    The walk stops at the first failure that is not about the shape - a refused key, a refused
    account, a malformed request - because those answer the same way in either shape, and
    asking twice would only spend the request twice.
    """
    refused: str | None = None
    for protocol in _protocols_for(current, model):
        body = _body(protocol, current, model, messages, temperature, json_mode)
        try:
            payload = _call_payload(current, _endpoint(current, protocol), body, timeout,
                                    run_id)
        except _ProtocolRefused as exc:
            refused = refused or str(exc)
            log.warning("llm: %s does not answer %s, asking in the other shape",
                        model, protocol)
            continue
        _ANSWERED_BY[_model_key(current, model)] = protocol
        return protocol, payload
    raise _ProtocolRefused(refused or "every shape was refused")


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
    started = time.time()
    try:
        protocol, payload = _answer(current, model, messages, temperature, json_mode,
                                    timeout, run_id)
        reading = _read(protocol, payload)
        text = reading["text"]
        if not text.strip():
            raise RuntimeError("the endpoint returned an empty message")
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
            prompt_tokens=reading["prompt_tokens"],
            completion_tokens=reading["completion_tokens"], ok=True, error=None)
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
