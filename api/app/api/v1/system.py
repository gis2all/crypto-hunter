"""系统设置: this machine's own wiring - the LLM gateway, the X session, the log level.

Not a chain's口径 and not a per-chain page: there is one of each per install, and they are what
`config/system.yaml` holds. The file stays the truth - this is a form in front of it, so an
operator does not have to know its shape to change a key, and a row the operating system has
taken over says so instead of pretending a save would change it (see `system_settings`).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import config, system_settings
from ...llm import client as llm
from ...search.engines.xsearch import PROBE_TIMEOUT, XEngine
from . import forms
from .schemas import SystemFormOut, SystemModelsOut, SystemTestOut, SystemXOut

router = APIRouter(prefix="/system", tags=["system"])

#: The row the endpoint's own model list belongs to. It travels in the answer so the page can
#: offer the list on the right row without learning a key name (8.9.1).
_MODEL_ROW = "llm.model"


class SystemIn(BaseModel):
    """A patch keyed by each row's own path in the file (`llm.base_url`)."""

    patch: dict = Field(default_factory=dict)


@router.get("/form", response_model=SystemFormOut)
def get_form() -> dict:
    return _form()


@router.put("", response_model=SystemFormOut)
def put_system(body: SystemIn) -> dict:
    """Save what changed, then answer the file as it now reads.

    The whole form comes back rather than an acknowledgement: a save can leave a row looking
    different from what was typed - a number, a box cleared back to its default, a credential
    standing as a fingerprint - and the page has to draw what was written, not what it sent.
    """
    try:
        system_settings.save(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _form()


@router.post("/models", response_model=SystemModelsOut)
def post_models(body: SystemIn) -> dict:
    """The model ids the endpoint says it serves, read with the page's unsaved rows.

    A refusal is an answer rather than an error: the page prints the sentence beside the button
    that asked for it, so a wrong key or an endpoint with no `/models` leaves the form as it was.
    The list is a convenience either way - the box stays typeable, and a model the gateway does
    not list is still a model it will answer with.
    """
    try:
        current = _probe_current(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        models = llm.list_models(current)
    except llm.LLMUnavailable as exc:
        return {"ok": False, "models": [], "apply_to": _MODEL_ROW, "note": str(exc)}
    return {"ok": True, "models": models, "apply_to": _MODEL_ROW,
            "note": "端点返回 %d 个模型，候选已就绪" % len(models)}


@router.post("/test", response_model=SystemTestOut)
def post_test(body: SystemIn) -> dict:
    """A really small real call per key: the writing path itself, not a reachable endpoint.

    Reachability is not the question a person is asking when they press this - an endpoint
    that answers 200 with an empty message is the failure this repo has already paid for once
    (6.4.8). So the reading that comes back is whether text arrived. Every key is asked, not
    just the first: the standby has never answered anything, and a round only meets it on the
    day the primary is refused (8.9.7).
    """
    try:
        current = _probe_current(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    readings = llm.probe_keys(current)
    lines = [_probe_line(index, out, current["model"]) for index, out in enumerate(readings)]
    left = len(current["api_keys"]) - len(readings)
    if left > 0:
        lines.append({"label": "", "ok": True, "note": "未探测 %d 个 Key" % left,
                      "status": None, "latency_ms": None, "chars": None,
                      "finish_reason": ""})
    return {"ok": all(line["ok"] for line in lines), "lines": lines}


@router.post("/x", response_model=SystemXOut)
def post_x(body: SystemIn) -> dict:
    """Whether the X session on this page still carries a search: one profile read, no query.

    The same rule as the gateway's probes - the rows as they stand on screen, not the file - and
    the same reason: an operator who had to save a cookie before finding out whether it works
    would be storing it in order to test it. Nothing is written, and nothing is recorded: a
    search spends a rate budget a round needs, so this asks the one cheap question that tells a
    session from two strings that look like one.
    """
    try:
        values = system_settings.effective(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    engine = XEngine(token=values["x.auth_token"], ct0=values["x.ct0"],
                     cli=values["x.cli"].strip() or None, timeout=PROBE_TIMEOUT)
    reading = engine.probe()
    return {"ok": reading["ok"], "note": reading["note"]}


def _key_label(index: int) -> str:
    """Which row a key came from, in that row's own words: the first is the one on
    `llm.api_key`, the rest are standbys, and past the second the position is the only name
    left."""
    if index == 0:
        return forms.SYSTEM_LABELS["llm.api_key"]
    label = forms.SYSTEM_LABELS["llm.api_key_fallback"]
    return label if index == 1 else "%s %d" % (label, index)


def _probe_line(index: int, out: dict, model: str) -> dict:
    """One key's reading as the page prints it: which row, and the sentence it earned."""
    note = out["error"] or (
        "连接正常 · %d 毫秒 · %s 返回 %d 字" % (out["latency_ms"], model, out["chars"]))
    return {"label": _key_label(index), "ok": not out["error"], "note": note,
            "status": out["status"], "latency_ms": out["latency_ms"], "chars": out["chars"],
            "finish_reason": out["finish_reason"]}


def _probe_current(patch: Mapping[str, Any]) -> dict:
    """The client's own reading of this machine, with the page's draft laid over it.

    `llm.settings()` is the one place that knows what a call is made of; this only moves the
    values a form can change. A blank secret box is not a blank key: `effective` has already
    answered it with what is stored, which is the rule a save follows too (8.9.3).
    """
    values = system_settings.effective(patch)
    current = llm.settings()
    current["base_url"] = values["llm.base_url"].strip().rstrip("/")
    current["model"] = values["llm.model"].strip()
    keys = llm.keys_in(values.get("llm.api_key") or "", values.get("llm.api_key_fallback") or "")
    current["api_key"] = keys[0] if keys else ""
    current["api_keys"] = keys
    raw = values.get("llm.extra_headers") or ""
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            current["headers"] = {str(key): str(value) for key, value in parsed.items()}
    return current


def _form() -> dict:
    """The page, read fresh: the file's own state is what a row is drawn from."""
    return {"path": str(config.SYSTEM_PATH),
            "blocks": forms.system_blocks(system_settings.state())}
