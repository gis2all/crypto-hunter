"""Per-chain settings: versioned, immutable, and the source of the settings page.

A report always points at the settings version it ran with, so re-running never
rewrites history and "history is archived under the口径 of its time, never recomputed"
holds by construction.

`GET .../form` returns the page as a widget model (`api/v1/forms.py`); the SPA renders
it without knowing what a filter is. `PUT` takes a partial patch of dotted-path leaves
and coerces every value against the current config type, so the browser can never turn
a number into a string by accident.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import config
from ...pipeline import run as pipeline_run
from ...pipeline import schedule
from ...report import assemble as report_assemble
from ...store import db
from . import forms
from .schemas import SettingsFormOut, SettingsOut

router = APIRouter(prefix="/chains/{chain_id}/settings", tags=["settings"])

SECTIONS = ("schedule", "pipeline", "report", "alerts", "sources")


class SettingsIn(BaseModel):
    """A partial patch of dotted-path leaves; empty means "nothing changed"."""

    patch: dict = Field(default_factory=dict)
    note: str | None = None


def _chain_or_404(chain_id: str) -> dict:
    try:
        return config.chain(chain_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown chain: {chain_id}")


def _yaml_defaults(chain_id: str) -> dict:
    """The config file's own values, before any saved snapshot is laid over them.

    Two readers want the file rather than the merge: the page, which overlays its own
    saved version on top, and `_resolve_blanks`, which has to ask whether a knob shipped
    empty - a question the saved snapshot has already answered differently."""
    cfg = _chain_or_404(chain_id)
    return {key: cfg.get(key) for key in SECTIONS}


def _reject_unknown_paths(current: dict, patch: dict, prefix: str = "") -> None:
    """Refuse a patch that does not land on a knob the config actually has.

    Without this, a typo (`scoring.onchain_weight` for `pipeline.scoring.onchain_weight`)
    is stored happily and changes nothing - an edit that silently does not apply is
    worse than a rejected one, because the page would claim it took effect.
    """
    for key, value in (patch or {}).items():
        head, _, rest = str(key).partition(".")
        path = prefix + head
        if head not in current:
            raise HTTPException(status_code=422, detail="unknown setting: %s" % path)
        child = current[head]
        if rest:
            if not isinstance(child, dict):
                raise HTTPException(status_code=422, detail="%s is not a group" % path)
            _reject_unknown_paths(child, {rest: value})
        elif isinstance(child, dict) != isinstance(value, dict):
            raise HTTPException(status_code=422, detail="%s is a group" % path)
        elif isinstance(value, dict):
            _reject_unknown_paths(child, value)


def _resolve_blanks(current: dict, patch: dict, prefix: str = "") -> dict:
    """Read a blank box: no value where that is a legal answer, a refusal where it is not.

    holders_max ships blank because no ceiling is a real answer, and a band's upper box
    has to be able to go back to blank. Nothing else has a blank: clearing 流动性's floor
    turns a rule the round reads as a number into one it cannot compare at all, and that
    used to surface as a 500 an hour later. So the YAML default decides - a numeric knob
    that ships with a number refuses a blank, and a knob that ships empty takes one. Text
    and list knobs are left alone: an empty credential is a legitimate thing to save.
    """
    out: dict = {}
    for key, value in (patch or {}).items():
        head, _dot, rest = str(key).partition(".")
        path = prefix + head
        here = current.get(head)
        if rest:
            child = _resolve_blanks(here if isinstance(here, dict) else {}, {rest: value},
                                   path + ".")
            merged = out.get(head)
            out[head] = {**(merged if isinstance(merged, dict) else {}), **child}
            continue
        numeric = isinstance(here, (int, float)) and not isinstance(here, bool)
        if value in (None, "") and numeric:
            raise HTTPException(status_code=422, detail="%s 不能留空" % path)
        elif value in (None, "") and here is None:
            out[head] = None
        elif isinstance(value, dict) and isinstance(here, dict):
            out[head] = _resolve_blanks(here, value, path + ".")
        else:
            out[head] = value
    return out


def _validate(payload: dict) -> dict:
    scoring = (payload.get("pipeline") or {}).get("scoring") or {}
    on_weight = scoring.get("onchain_weight")
    prod_weight = scoring.get("offchain_weight")
    if on_weight is not None and prod_weight is not None:
        if float(on_weight) + float(prod_weight) != 100:
            raise HTTPException(status_code=422, detail="onchain_weight + offchain_weight must equal 100")
    weights = scoring.get("onchain_factor_weights")
    factors = scoring.get("onchain_factors")
    if factors is not None and len(factors) != 5:
        raise HTTPException(status_code=422, detail="onchain_factors must list exactly 5 factors")
    if weights is not None and sum(float(v) for v in weights.values()) != 100:
        raise HTTPException(status_code=422, detail="onchain factor weights must sum to 100")
    return payload


def effective_payload(chain_id: str) -> tuple[dict, dict]:
    """The settings in force right now, plus where they came from.

    The stored snapshot is overlaid on the YAML rather than replacing it, so a knob
    added to the config after a snapshot was saved still exists (at its YAML default)
    instead of vanishing from the page and being rejected on save.
    """
    base = _yaml_defaults(chain_id)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, version, payload_json, note, created_at FROM settings_versions"
            " WHERE chain_id = ? ORDER BY version DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return base, {
            "version": 0, "source": "config/chains.yaml", "settings_version_id": None,
            "created_at": None, "note": None,
        }
    return config.merge_patch(base, json.loads(row["payload_json"])), {
        "version": row["version"], "source": "db", "settings_version_id": row["id"],
        "created_at": row["created_at"], "note": row["note"],
    }


@router.get("", response_model=SettingsOut)
def get_settings(chain_id: str) -> dict:
    _chain_or_404(chain_id)
    payload, meta = effective_payload(chain_id)
    return {"chain_id": chain_id, "payload": payload, **meta}


@router.put("", response_model=SettingsOut)
def put_settings(chain_id: str, body: SettingsIn) -> dict:
    _chain_or_404(chain_id)
    current, _meta = effective_payload(chain_id)
    _reject_unknown_paths(current, body.patch)
    patch = _resolve_blanks(_yaml_defaults(chain_id), body.patch)
    merged = config.sync_schedule(config.merge_patch(current, patch))
    _validate(merged)
    conn = db.connect()
    try:
        latest = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM settings_versions WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO settings_versions (chain_id, version, payload_json, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (chain_id, int(latest) + 1, json.dumps(merged, ensure_ascii=False), body.note,
             db.now_iso()),
        )
    finally:
        conn.close()
    return get_settings(chain_id)


def _latest_run(chain_id: str) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE chain_id = ? ORDER BY COALESCE(scheduled_at, started_at)"
            " DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _latest_batch(chain_id: str) -> dict | None:
    run = _latest_run(chain_id)
    if run is None:
        return None
    return report_assemble.batch(run["id"], per_round=5)


@router.get("/form", response_model=SettingsFormOut)
def get_form(chain_id: str) -> dict:
    chain = _chain_or_404(chain_id)
    payload, meta = effective_payload(chain_id)
    batch = _latest_batch(chain_id)
    run = _latest_run(chain_id)

    rule_counts = {r["key"]: r["count"] for r in ((batch or {}).get("rule_counts") or [])}
    scores = [c for c in ((batch or {}).get("cards") or [])]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    conn = db.connect()
    try:
        # The measured averages come off the scored items, not off the cards. Card numbers
        # are already rounded for display, and averaging rounded numbers rounds twice: the
        # five real totals sum to 278.7389, so the mean is 55.7 - not the 55.8 you get from
        # averaging 60.1 / 56.5 / 54.9 / 54.0 / 53.3.
        measured = [
            json.loads(row["breakdown_json"])
            for row in (
                conn.execute(
                    "SELECT breakdown_json FROM run_items WHERE run_id = ? AND"
                    " decision = ? ORDER BY score DESC",
                    ((run or {}).get("id"), "selected"),
                ).fetchall()
                if run
                else []
            )
            if row["breakdown_json"]
        ]
        if not measured:
            measured = [
                {"onchain": c.get("onchain"), "product": c.get("product"), "total": c.get("score")}
                for c in scores
            ]
    finally:
        conn.close()

    lp_unknown = [c["sym"] for c in scores if "未判定" in (c["safety"]["lp"]["label"])]
    totals = {
        "universe": (run or {}).get("scanned") or 0,
        "candidates": (run or {}).get("candidates") or 0,
        "selected": (run or {}).get("selected") or 0,
        "onchain_avg": mean(
            [m["onchain"] for m in measured if m.get("onchain") is not None]
        ),
        "product_avg": mean(
            [m["product"] for m in measured if m.get("product") is not None]
        ),
        "total_avg": mean([m["total"] for m in measured if m.get("total") is not None]),
        "last_batch": _last_batch_text(batch),
        "sources": (batch or {}).get("sources") or [],
        "run_status": _run_status(run, chain),
        "lp_unjudged_warn": ("入选未判定 %d" % len(lp_unknown)) if lp_unknown else "",
        "safety_warn": (
            "本轮违规：入选标的 LP 未判定 —— " + " · ".join(lp_unknown)
            if lp_unknown else "本轮违规：无"
        ),
    }

    built = forms.build({**chain, **payload}, totals, rule_counts)
    scoring = ((chain.get("pipeline") or {}).get("scoring") or {})
    # The note bar is the one settings sentence that carries emphasis in the frozen
    # prototype (both weights and the three measured averages are bold), so it ships as
    # runs instead of a flat string.
    note_bar = [
        {"kind": "text", "text": "综合分 = "},
        {"kind": "bold", "text": f"链上质量 {scoring.get('onchain_weight', 55)}%"},
        {"kind": "text", "text": " + "},
        {"kind": "bold", "text": f"链下调研 {scoring.get('offchain_weight', 45)}%"},
        {
            "kind": "text",
            "text": f"，下面按这两块分。 本轮入选 {totals['selected']} 个的实测：链上质量均值 ",
        },
        {"kind": "bold", "text": _fmt(totals["onchain_avg"])},
        {"kind": "text", "text": " · 链下调研均值 "},
        {"kind": "bold", "text": _fmt(totals["product_avg"])},
        {"kind": "text", "text": " → 综合分均值 "},
        {"kind": "bold", "text": _fmt(totals["total_avg"])},
        {"kind": "text", "text": "。"},
    ]
    return {
        "chain_id": chain_id,
        "label": chain.get("label") or chain_id,
        "version": meta["version"],
        "note_bar": note_bar,
        # The bar keeps the weights and this round's measured averages - numbers a reader
        # came for. How a save takes effect, and that old batches are never recomputed, is
        # a rule: it opens from the 说明 icon beside them.
        "note_detail": (
            "改完点右上「立即运行」重跑本轮，或「保存并生效」等下一个自动时刻；"
            "历史批次按当时口径归档、不追溯重算。"
        ),
        "dirty_hint": (
            "淘汰数要重跑这一轮才会刷新。点右上「立即运行」就用当前这套参数真跑一轮，"
            "跑完即视为保存生效；只想存参数、等下一个自动时刻，按「保存并生效」。"
        ),
        "blocks": built["blocks"],
        "totals": totals,
        # The two口径 this list used to carry are decided and implemented - the percentile
        # denominator in core/scoring.py (the round's hard survivors) and the elimination
        # counts in core/funnel.py (cumulative, in the settings page's own order). What is
        # still missing is listed instead, rather than left to be discovered.
        "blocked": [
            "预演：按新参数重算淘汰数，需要本轮 universe 快照，尚未落库",
        ],
    }


def _run_status(run: dict | None, chain: dict) -> str:
    """What the scheduler is doing, told to the operator rather than configured by them."""
    if run and run.get("status") == "failed":
        return "最近一轮失败 · %s" % (run.get("error") or "未记录原因")
    slots = forms.interval_slots(int((chain.get("schedule") or {}).get("interval_hours") or 3))
    now = datetime.now()
    upcoming = [h for h in slots if h > now.hour] or slots[:1]
    return "扫描器正常 · 下轮 %02d:00" % upcoming[0]




def _fmt(value: float | None) -> str:
    return "—" if value is None else "%.1f" % value


def _last_batch_text(batch: dict | None) -> str:
    """When the last round ran. The date lives on the report header, this row is the clock."""
    if not batch:
        return "还没有跑过一轮"
    label = str(batch["batch_label"])
    clock = re.search(r"\d{1,2}:\d{2}", label)
    return "最近一次 %s · 入选 %d" % (
        clock.group(0) if clock else label, batch["funnel"]["selected"])


@router.post("/run", response_model=None)
def run_now(chain_id: str) -> dict:
    """Run this round immediately with the settings in force.

    Creating the row is not starting it. The ticker looks at the clock, so it would never
    pick a manual round up - the button has to hand the round to the worker itself, or the
    row sits at `pending` looking like it is still waiting for a slot.
    """
    _chain_or_404(chain_id)
    row = pipeline_run.create_run(chain_id, trigger="manual")
    schedule.submit(chain_id, row["id"])
    return row


@router.post("/recompute", response_model=None)
def recompute(chain_id: str, body: SettingsIn) -> JSONResponse:
    """Replay the latest snapshots against a candidate patch: who moves, and why.

    Blocked on purpose, but not on the口径: those are decided and implemented. A
    recompute would have to re-apply a patched payload to the round's own universe, and
    the universe is not stored - only the candidates are. Re-scoring the candidates alone
    would produce a diff against a smaller pool than the round actually ran, which is a
    fabricated answer, and worse than no answer.
    """
    _chain_or_404(chain_id)
    current, _meta = effective_payload(chain_id)
    _reject_unknown_paths(current, body.patch)
    _validate(config.merge_patch(current, _resolve_blanks(_yaml_defaults(chain_id),
                                                          body.patch)))
    return JSONResponse(
        status_code=501,
        content={
            "detail": "recompute is not implemented yet",
            "blocked_by": [
                "the round's universe is not stored, so a patched payload cannot be "
                "re-applied to the same tokens"
            ],
            "see": "CLAUDE.md",
        },
    )
