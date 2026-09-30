"""Chain catalogue, read from config/chains/*.yaml - and the writing half of it.

The two GETs are the reading: one chain, and one of its sets of settings with the size of
what it holds. The three writers add, rename and drop a set of settings, which is a request
to edit a file rather than to save a value - which is why they are here and not in
`settings.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import config
from ...pipeline import run as pipeline_run
from ...pipeline import schedule
from ...store import db
from .schemas import (ChainConfigDetailOut, ChainConfigIn, ChainOut, DeleteIn, LabelIn,
                      LibraryOut)

router = APIRouter(prefix="/chains", tags=["chains"])

PUBLIC_FIELDS = ("id", "label", "enabled", "explorer", "sources", "schedule", "report", "pipeline")


def _chain_or_404(chain_id: str) -> dict:
    try:
        return config.chain_entry(chain_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"没有这条链：{chain_id}")


def _entry_or_404(chain_id: str, config_id: str) -> dict:
    _chain_or_404(chain_id)
    try:
        return config.config_entry(chain_id, config_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"没有这套设定：{chain_id}:{config_id}")


def _refused(exc: Exception) -> HTTPException:
    """The status a refusal comes back as, with the sentence the page prints.

    Three kinds, and the codes are the ordinary ones: a name that is empty or already taken
    is a 400, a name nobody has is a 404, and a set of settings that cannot go *now* - the
    default one, or the live one - is a 409. The page does not translate any of them; it
    prints the sentence, so each is written for the person who has to act on it.
    """
    if isinstance(exc, config.ConfigRefused):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"没有这套设定：{exc.args[0]}")
    return HTTPException(status_code=400, detail=str(exc))


def _detail(chain_id: str, entry: dict) -> dict:
    """One set of settings, with the size of what deleting it would destroy."""
    return {"id": entry["id"], "label": entry["label"],
            "enabled": bool(entry.get("enabled", True)),
            "locked": bool(entry.get("locked")),
            "library": db.library_size(chain_id, entry["id"])}


def _public(chain_id: str) -> ChainOut:
    """One chain plus the list of its sets of settings.

    The settings fields are read through the default entry, and `configs` is the menu. A
    round is always run under one named entry, so a client that wants the others asks for
    them by name rather than being handed a merged view of several.
    """
    entry = _chain_or_404(chain_id)
    cfg = config.chain(chain_id)
    values = {k: cfg.get(k) for k in PUBLIC_FIELDS}
    values["name"] = config.chain_name(cfg)
    values["sources"] = values.get("sources") or []
    values["configs"] = [
        {"id": c["id"], "label": c["label"],
         "enabled": bool(c.get("enabled", True)),
         "locked": bool(c.get("locked"))}
        for c in entry.get("configs") or []
    ]
    values["warning"] = entry.get("warning")
    return ChainOut(**values)


@router.get("", response_model=list[ChainOut])
def list_chains() -> list[ChainOut]:
    return [_public(chain_id) for chain_id in config.load_chains()]


@router.get("/{chain_id}", response_model=ChainOut)
def get_chain(chain_id: str) -> ChainOut:
    return _public(chain_id)


@router.post("/{chain_id}/configs", response_model=ChainConfigDetailOut)
def add_chain_config(chain_id: str, body: ChainConfigIn) -> dict:
    """Add a set of settings to a chain: a fresh one, or a copy of one that exists.

    Both are seeded from an entry's five sections and neither brings a report library - a
    new set of settings has earned nothing yet. The new one is born switched off, so adding
    one never moves which set of settings the chain is running.
    """
    _chain_or_404(chain_id)
    try:
        entry = config.create_config(chain_id, body.mode, body.from_config_id, body.label)
    except (config.ConfigRefused, KeyError, ValueError) as exc:
        raise _refused(exc)
    return _detail(chain_id, entry)


@router.get("/{chain_id}/configs/{config_id}", response_model=ChainConfigDetailOut)
def get_chain_config(chain_id: str, config_id: str) -> dict:
    return _detail(chain_id, _entry_or_404(chain_id, config_id))


@router.patch("/{chain_id}/configs/{config_id}", response_model=ChainConfigDetailOut)
def rename_chain_config(chain_id: str, config_id: str, body: LabelIn) -> dict:
    """Rename a set of settings. The id does not move with the label.

    The id is in five primary keys and in every run key the chain has written, so moving it
    would be a migration of the whole report library to change what a row reads. The label
    is the part a human reads; this is the only part a rename touches.
    """
    _entry_or_404(chain_id, config_id)
    try:
        entry = config.rename_config(chain_id, config_id, body.label)
    except (config.ConfigRefused, KeyError, ValueError) as exc:
        raise _refused(exc)
    return _detail(chain_id, entry)


@router.delete("/{chain_id}/configs/{config_id}", response_model=LibraryOut)
def delete_chain_config(chain_id: str, config_id: str, body: DeleteIn) -> dict:
    """Delete a set of settings and its whole report library. There is no undo.

    Three gates, in the order a person meets them. The label typed back has to be the one on
    the entry, because a double confirmation that a click can pass is not a confirmation.
    The entry has to be one that may go at all, which `config.delete_config` refuses for the
    default and for the live one. And no round of it may be in flight, because that round is
    a worker reading rows this would delete out from under it.
    """
    entry = _entry_or_404(chain_id, config_id)
    if " ".join(body.label.split()) != entry["label"]:
        raise HTTPException(
            status_code=409,
            detail=f"要删的是「{entry['label']}」，把名字原样打一遍才会删")
    if entry["id"] in schedule.live_configs(chain_id):
        raise HTTPException(
            status_code=409,
            detail=f"「{entry['label']}」有一轮正在跑，等这一轮结束再删")
    try:
        config.delete_config(chain_id, entry["id"])
    except (config.ConfigRefused, KeyError, ValueError) as exc:
        raise _refused(exc)
    return config.forget_library(chain_id, entry["id"])
