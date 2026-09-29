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
import math
import re
from datetime import datetime
from typing import Any

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


def _chain_or_404(chain_id: str, config_id: str | None = None) -> dict:
    """The chain read through one of its sets of settings. 404s on either name."""
    try:
        return config.chain(chain_id, config_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"没有这条链：{chain_id if not config_id else chain_id + ':' + config_id}",
        )


def _yaml_defaults(chain_id: str, config_id: str | None = None) -> dict:
    """The config file's own values, before any saved snapshot is laid over them.

    Two readers want the file rather than the merge: the page, which overlays its own
    saved version on top, and `_resolve_blanks`, which has to ask whether a knob shipped
    empty - a question the saved snapshot has already answered differently."""
    cfg = _chain_or_404(chain_id, config_id)
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
            raise HTTPException(status_code=422, detail="设定里没有「%s」这一项" % path)
        child = current[head]
        if rest:
            if not isinstance(child, dict):
                raise HTTPException(status_code=422, detail="「%s」不是一个分组" % path)
            _reject_unknown_paths(child, {rest: value})
        elif isinstance(child, dict) != isinstance(value, dict):
            raise HTTPException(status_code=422, detail="「%s」是个分组，不能整组换掉" % path)
        elif isinstance(value, dict):
            _reject_unknown_paths(child, value)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge two already-resolved subtrees structurally, not by key.

    `{**base, **patch}` replaces a whole group the moment the group's name repeats, so
    patching `...factor_weights.liquidity` and `...factor_weights.bundling` in one request
    used to keep only the last one. Deep merge is the only honest reading of two leaves
    that land in the same group.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_blanks(current: dict, patch: dict, prefix: str = "",
                    labels: dict[str, str] | None = None) -> dict:
    """Read a blank box: no value where that is a legal answer, a refusal where it is not.

    holders_max ships blank because no ceiling is a real answer, and a band's upper box
    has to be able to go back to blank. Nothing else has a blank: clearing 流动性's floor
    turns a rule the round reads as a number into one it cannot compare at all, and that
    used to surface as a 500 an hour later. So the YAML default decides - a numeric knob
    that ships with a number refuses a blank, and a knob that ships empty takes one. Text
    and list knobs are left alone: an empty credential is a legitimate thing to save.
    `labels` is the page's own wording for each key (`forms.field_labels`), so the refusal
    names the box ("流动性") instead of the path this function happens to walk.
    """
    out: dict = {}
    for key, value in (patch or {}).items():
        head, _dot, rest = str(key).partition(".")
        path = prefix + head
        here = current.get(head)
        if rest:
            child = _resolve_blanks(here if isinstance(here, dict) else {}, {rest: value},
                                   path + ".", labels)
            merged = out.get(head)
            out[head] = _deep_merge(merged if isinstance(merged, dict) else {}, child)
            continue
        numeric = isinstance(here, (int, float)) and not isinstance(here, bool)
        if value in (None, "") and numeric:
            raise HTTPException(status_code=422,
                                detail="「%s」不能留空：这个格子必须有个数"
                                       % (labels or {}).get(path, path))
        elif value in (None, "") and here is None:
            out[head] = None
        elif isinstance(value, dict) and isinstance(here, dict):
            out[head] = _resolve_blanks(here, value, path + ".", labels)
        else:
            out[head] = value
    return out


def _pct(value: Any) -> str:
    """A weight the way the page prints it: 20, not 20.0."""
    number = float(value)
    return str(int(number)) if number == int(number) else "%.1f" % number


def _boxes(weights: dict, path: str, labels: dict[str, str]) -> str:
    """The five boxes as they stand, named the way the page names them.

    A 422 that only says "must sum to 100" leaves the operator adding five numbers by
    hand; naming the rows and printing each one turns it into "move this box". The labels
    come from the page's own model (`forms.field_labels`), so the error and the row above it
    cannot read differently (CLAUDE.md 2.3.3).
    """
    return " ｜ ".join("%s %s%%" % (labels.get(path + key, key), _pct(value))
                      for key, value in weights.items())


def _validate(payload: dict, labels: dict[str, str]) -> dict:
    scoring = (payload.get("pipeline") or {}).get("scoring") or {}
    on_weight = scoring.get("onchain_weight")
    prod_weight = scoring.get("offchain_weight")
    if on_weight is not None and prod_weight is not None:
        total = float(on_weight) + float(prod_weight)
        if total != 100:
            raise HTTPException(
                status_code=422,
                detail="链上质量权重 ＋ 链下调研权重必须等于 100%%，当前 %s%%"
                       "（链上质量 %s%% ＋ 链下调研 %s%%）"
                       % (_pct(total), _pct(on_weight), _pct(prod_weight)),
            )
    weights = scoring.get("onchain_factor_weights")
    factors = scoring.get("onchain_factors")
    if factors is not None and len(factors) != 5:
        raise HTTPException(status_code=422,
                            detail="链上质量的因子要正好 5 个，当前 %d 个" % len(factors))
    if weights is not None and sum(float(v) for v in weights.values()) != 100:
        raise HTTPException(
            status_code=422,
            detail="链上质量五项权重合计要等于 100%%，当前 %s%%（%s）"
                   % (_pct(sum(float(v) for v in weights.values())),
                      _boxes(weights, "pipeline.scoring.onchain_factor_weights.", labels)),
        )
    off_weights = scoring.get("offchain_factor_weights")
    if off_weights is not None and sum(float(v) for v in off_weights.values()) != 100:
        raise HTTPException(
            status_code=422,
            detail="链下调研五项权重合计要等于 100%%，当前 %s%%（%s）"
                   % (_pct(sum(float(v) for v in off_weights.values())),
                      _boxes(off_weights, "pipeline.scoring.offchain_factor_weights.", labels)),
        )
    return payload


def effective_payload(chain_id: str, config_id: str | None = None) -> tuple[dict, dict]:
    """The settings in force right now, plus where they came from.

    The stored snapshot is overlaid on the YAML rather than replacing it, so a knob
    added to the config after a snapshot was saved still exists (at its YAML default)
    instead of vanishing from the page and being rejected on save.

    One set of settings at a time: a chain's second set has its own chain of versions,
    and the first set's latest is not a version of this one.
    """
    base = _yaml_defaults(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, version, payload_json, note, created_at FROM settings_versions"
            " WHERE chain_id = ? AND config_id = ? ORDER BY version DESC LIMIT 1",
            (chain_id, entry["id"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return base, {
            "version": 0, "source": config.chain_entry(chain_id)["path"],
            "settings_version_id": None, "created_at": None, "note": None,
        }
    # The snapshot is read against the YAML's shape, so a knob the config dropped is not
    # a known path here: `_reject_unknown_paths` would otherwise accept a patch aimed at
    # one and the save path would prune it away again - a save that says it worked and
    # changed nothing, which is the one outcome that guard exists to prevent.
    return config.merge_patch(
        base, config.prune_unknown(base, json.loads(row["payload_json"]))), {
        "version": row["version"], "source": "db", "settings_version_id": row["id"],
        "created_at": row["created_at"], "note": row["note"],
    }


@router.get("", response_model=SettingsOut)
def get_settings(chain_id: str, config_id: str | None = None) -> dict:
    _chain_or_404(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    payload, meta = effective_payload(chain_id, entry["id"])
    return {"chain_id": chain_id, "config_id": entry["id"], "payload": payload, **meta}


@router.put("", response_model=SettingsOut)
def put_settings(chain_id: str, body: SettingsIn,
                 config_id: str | None = None) -> dict:
    """Save a version, and make this set of settings the chain's live one.

    Saving is where the page's dropdown commits: it shows which set of settings is being
    edited, so this is the one write that can mean "and use it". A save that left the
    previous one running would be a save whose effect the operator cannot see, which is the
    one thing a settings page cannot afford to be.
    """
    chain = _chain_or_404(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    current, _meta = effective_payload(chain_id, entry["id"])
    _reject_unknown_paths(current, body.patch)
    labels = forms.field_labels(chain)
    base = _yaml_defaults(chain_id, entry["id"])
    patch = _resolve_blanks(base, body.patch, labels=labels)
    # A version is the YAML plus every version saved before it, and a merge only ever
    # adds: a knob the config dropped would otherwise be copied forward for ever. The
    # shape this version claims to have is the YAML's, so the orphans stop here.
    merged = config.sync_schedule(
        config.prune_unknown(base, config.merge_patch(current, patch)))
    _validate(merged, labels)
    conn = db.connect()
    try:
        latest = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM settings_versions WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO settings_versions (chain_id, config_id, version, payload_json,"
            " note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chain_id, entry["id"], int(latest) + 1,
             json.dumps(merged, ensure_ascii=False), body.note, db.now_iso()),
        )
    finally:
        conn.close()
    config.set_active_config(chain_id, entry["id"])
    return get_settings(chain_id, entry["id"])


def _latest_run(chain_id: str, config_id: str | None = None) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE chain_id = ? AND config_id = ?"
            " ORDER BY COALESCE(scheduled_at, started_at) DESC LIMIT 1",
            (chain_id, config.config_entry(chain_id, config_id)["id"]),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _latest_batch(chain_id: str, config_id: str | None = None) -> dict | None:
    run = _latest_run(chain_id, config_id)
    if run is None:
        return None
    return report_assemble.batch(run["id"], per_round=5)


@router.get("/form", response_model=SettingsFormOut)
def get_form(chain_id: str, config_id: str | None = None) -> dict:
    chain = _chain_or_404(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    payload, meta = effective_payload(chain_id, entry["id"])
    batch = _latest_batch(chain_id, entry["id"])
    run = _latest_run(chain_id, entry["id"])

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
    """A measured average, as the settings page prints it: a whole number.

    `totals` keeps the float - only the sentence rounds - and it rounds half up, the
    rule the card scores follow, so a mean printed here and the same number printed on
    a card can never disagree by a half."""
    return "—" if value is None else "%d" % math.floor(value + 0.5)


def _last_batch_text(batch: dict | None) -> str:
    """When the last round ran. The date lives on the report header, this row is the clock."""
    if not batch:
        return "还没有跑过一轮"
    label = str(batch["batch_label"])
    clock = re.search(r"\d{1,2}:\d{2}", label)
    return "最近一次 %s · 入选 %d" % (
        clock.group(0) if clock else label, batch["funnel"]["selected"])


@router.post("/run", response_model=None)
def run_now(chain_id: str, config_id: str | None = None) -> dict:
    """Run this round immediately with the settings in force.

    Creating the row is not starting it. The ticker looks at the clock, so it would never
    pick a manual round up - the button has to hand the round to the worker itself, or the
    row sits at `pending` looking like it is still waiting for a slot.
    """
    _chain_or_404(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    row = pipeline_run.create_run(chain_id, trigger="manual", config_id=entry["id"])
    schedule.submit(chain_id, row["id"], entry["id"])
    return row


@router.post("/recompute", response_model=None)
def recompute(chain_id: str, body: SettingsIn,
              config_id: str | None = None) -> JSONResponse:
    """Replay the latest snapshots against a candidate patch: who moves, and why.

    Blocked on purpose, but not on the口径: those are decided and implemented. A
    recompute would have to re-apply a patched payload to the round's own universe, and
    the universe is not stored - only the candidates are. Re-scoring the candidates alone
    would produce a diff against a smaller pool than the round actually ran, which is a
    fabricated answer, and worse than no answer.
    """
    chain = _chain_or_404(chain_id, config_id)
    entry = config.config_entry(chain_id, config_id)
    current, _meta = effective_payload(chain_id, entry["id"])
    _reject_unknown_paths(current, body.patch)
    labels = forms.field_labels(chain)
    _validate(config.merge_patch(current,
                                 _resolve_blanks(_yaml_defaults(chain_id, entry["id"]),
                                                 body.patch, labels=labels)),
              labels)
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
