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
from ...core import lexicon as lexicon_rules
from ...pipeline import run as pipeline_run
from ...pipeline import schedule
from ...report import assemble as report_assemble
from ...report import research
from ...report.facts import money
from ...store import db
from . import forms
from .schemas import LexiconRowOut, SettingsFormOut, SettingsOut

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
    prod_weight = scoring.get("product_weight")
    if on_weight is not None and prod_weight is not None:
        if float(on_weight) + float(prod_weight) != 100:
            raise HTTPException(status_code=422, detail="onchain_weight + product_weight must equal 100")
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
        lexicon_size = conn.execute(
            "SELECT COUNT(*) AS n FROM lexicon WHERE chain_id = ?", (chain_id,)
        ).fetchone()["n"]
        lexicon_updated = conn.execute(
            "SELECT MAX(updated_at) AS t FROM lexicon WHERE chain_id = ?", (chain_id,)
        ).fetchone()["t"]
        # Every entry, so the page can say how many are past their refresh rules today. Read
        # against the settings this chain is about to run with, not against the ones it ran
        # with last: the question the row answers is "what would this round rewrite".
        lexicon_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT ca, updated_at, baseline_mcap, baseline_holders FROM lexicon"
                " WHERE chain_id = ?", (chain_id,)
            ).fetchall()
        ]
        lexicon_research = {
            str(r["research_state"]): int(r["n"] or 0)
            for r in conn.execute(
                "SELECT COALESCE(research_state, 'none') AS research_state, COUNT(*) AS n"
                " FROM lexicon WHERE chain_id = ? GROUP BY research_state", (chain_id,)
            ).fetchall()
        }
        if batch:
            selected = [c["ca"] for c in scores]
            placeholders = ",".join("?" for _ in selected) or "''"
            lexicon_hit = (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM lexicon WHERE ca IN (%s)" % placeholders
                    , tuple(selected)
                ).fetchone()["n"]
                if selected
                else 0
            )
        else:
            lexicon_hit = 0
    finally:
        conn.close()

    lp_unknown = [c["sym"] for c in scores if "未判定" in (c["safety"]["lp"]["label"])]
    lex_cfg = (payload.get("pipeline") or {}).get("lexicon") or {}
    now = datetime.now().astimezone()
    stale = sum(
        1 for row in lexicon_rows
        if lexicon_rules.due(row, {}, lex_cfg, now).action == "rewrite"
    )
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
        "lexicon_size": lexicon_size,
        "lexicon_hit": lexicon_hit,
        "lexicon_updated": lexicon_updated,
        "lexicon_stale": stale,
        "lexicon_research": "已重档 %d ／ 待跑 %d ／ 失败 %d" % (
            lexicon_research.get("done", 0),
            lexicon_research.get("queued", 0) + lexicon_research.get("running", 0),
            lexicon_research.get("failed", 0),
        ),
        "last_batch": _last_batch_text(batch),
        "sources": (batch or {}).get("sources") or [],
        "run_status": _run_status(run, chain),
        "copy_from": _copy_from(chain_id),
        "lp_unjudged_warn": ("入选未判定 %d" % len(lp_unknown)) if lp_unknown else "",
        "safety_warn": (
            "本轮违规：入选标的 LP 未判定 —— " + " · ".join(lp_unknown)
            if lp_unknown else "本轮违规：无"
        ),
    }

    built = forms.build({**chain, **payload}, totals, rule_counts)
    # The note bar is the one settings sentence that carries emphasis in the frozen
    # prototype (both weights and the three measured averages are bold), so it ships as
    # runs instead of a flat string.
    note_bar = [
        {"kind": "text", "text": "综合分 = "},
        {"kind": "bold", "text": f"链上质量 {_weight(built, 'onchain')}%"},
        {"kind": "text", "text": " + "},
        {"kind": "bold", "text": f"赛道产品 {_weight(built, 'product')}%"},
        {
            "kind": "text",
            "text": f"，下面按这两块分。 本轮入选 {totals['selected']} 个的实测：链上质量均值 ",
        },
        {"kind": "bold", "text": _fmt(totals["onchain_avg"])},
        {"kind": "text", "text": " · 赛道产品均值 "},
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
            "各卡片的淘汰数已置灰、标「待重算」。点右上「立即运行」就用当前这套参数真跑一轮，"
            "跑完即视为保存生效；只想存参数、等下一个自动时刻，按「保存并生效」。"
        ),
        "blocks": built["blocks"],
        "totals": totals,
        "lexicon": _lexicon_rows(chain_id, [c["ca"] for c in scores]),
        # The reading rules that used to sit under the lexicon table. They name the two
        # score columns, the conversion and the badge, so the weight comes from the same
        # place the block header reads it - one number on the page, one place to change it.
        "lexicon_note": (
            "「本轮」是这次入选里命中它的那几个。两列都是 0–5 的原始判断分；"
            "综合分里 %s%% 的赛道产品分按（叙事贴合 + 产品完整度）÷ 10 × 100 折算，"
            "改了下一轮就按新分算。\n"
            "每行的灰字是档案的新鲜度：写于多久前、按「词条库状态」里的刷新条件今天"
            "该不该重写、以及发布后的重档调研跑到哪一步。标的下的链接是重档时实际"
            "读过的页面。\n"
            "不在库里的新标先是「待入库」，LLM 补写完整后才入库并参与打分。"
        ) % _weight(built, "product"),
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


def _copy_from(chain_id: str) -> str | None:
    """The other radar this chain can copy its settings from.

    A chain that is declared but not collecting yet still carries a full set of
    defaults, so it is a usable copy source - only an undeclared chain is not.
    """
    others = [c["label"] for c in config.load_chains().values() if c["id"] != chain_id]
    return others[0] if others else None


def _weight(built: dict, key: str) -> str:
    for block in built["blocks"]:
        if block["key"] == key:
            return block["weight"].replace("占 ", "").replace("%", "")
    return ""


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


def _last_mcap(chain_id: str) -> dict[str, float]:
    """What each token was worth the last time a round saw it.

    Read from the snapshots rather than the lexicon row: the row only carries its
    baseline, which is the moment the entry was filed, and an entry written a week ago
    would still read at its filing size. Snapshots without a market cap are skipped -
    most of them are the scan pass, which never priced anything - so this is the newest
    snapshot that could answer, not simply the newest snapshot.
    """
    key = "$.market_cap_usd"
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ca, mcap FROM ("
            "  SELECT s.ca AS ca,"
            "         CAST(json_extract(s.metrics_json, ?) AS REAL) AS mcap,"
            "         ROW_NUMBER() OVER (PARTITION BY s.ca ORDER BY s.created_at DESC)"
            "           AS rn"
            "  FROM snapshots s JOIN runs r ON r.id = s.run_id"
            "  WHERE r.chain_id = ?"
            "    AND json_extract(s.metrics_json, ?) IS NOT NULL"
            ") WHERE rn = 1",
            (key, chain_id, key),
        ).fetchall()
    finally:
        conn.close()
    return {str(r["ca"]).lower(): r["mcap"] for r in rows}


def _lexicon_rows(chain_id: str, current: list[str]) -> list[dict]:
    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT ca, symbol, tag, narrative, product, social, concl, review_state,"
                " updated_at, refresh_reason, baseline_at, research_state, research_note"
                " FROM lexicon WHERE chain_id = ?"
                " ORDER BY symbol COLLATE NOCASE",
                (chain_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    evidence = research.sources_by_ca(chain_id)
    caps = _last_mcap(chain_id)
    lex_cfg = (config.chain(chain_id).get("pipeline") or {}).get("lexicon") or {}
    now = datetime.now().astimezone()
    hit = set(current)
    for row in rows:
        row["current"] = row["ca"] in hit
        # Same rule as the card and the API row: one function decides what stale means, and
        # the page only draws its answer.
        verdict = lexicon_rules.due(row, {}, lex_cfg, now)
        row["age_hours"] = None if verdict.age_hours is None else round(verdict.age_hours, 1)
        row["stale"] = verdict.action == "rewrite"
        row["sources"] = evidence.get(str(row["ca"]).lower(), [])
        row["mcap"] = money(caps.get(str(row["ca"]).lower()))
    hit_rank = {ca: index for index, ca in enumerate(current)}
    rows.sort(
        key=lambda r: (
            hit_rank.get(r["ca"], len(hit_rank)),  # this round's picks first, in rank order
            r["review_state"] != "confirmed",      # then the rest: confirmed before drafts
            # "~" sorts after every letter, so a file with no symbol yet ends the
            # table instead of leading it. The frozen prototype orders it the same way.
            (r["symbol"] or "~").lower(),
        )
    )
    return [LexiconRowOut(**row).model_dump() for row in rows]


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
