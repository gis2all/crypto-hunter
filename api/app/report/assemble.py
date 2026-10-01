"""Read one round out of the database and build the page payload.

One call builds everything the report page draws - the batch strip, the funnel, the
change list, the five cards and the落选 table - so the SPA never stitches requests
together. Reads only, plus the one write at the end: a card on the shelf belongs to a
token rather than to a round, so a round that wrote one of those tokens again hands the
shelf its newer card (`refresh_kept`). Nothing here re-fetches or recomputes a score.
"""

from __future__ import annotations

import json

from .. import config
from ..api.v1 import forms
from ..store import db
from . import cards as card_builder

SETTINGS_SECTIONS = ("schedule", "pipeline", "report", "alerts")

# Rounds that predate the round-level facts table have no push state at all. That is a
# reason for the reader of the API, not a delivery status, so it travels in its own
# field and the status stays empty.
MISSING_BATCH = "本轮早于总览表，没有落库推送状态"


def _rows(conn, sql: str, args: tuple) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _load(conn, run_id: str) -> dict:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(run_id)
    summary = conn.execute(
        "SELECT payload_json FROM run_summary WHERE run_id = ?", (run_id,)
    ).fetchone()
    snapshot = _rows(
        conn,
        "SELECT ca, metrics_json, safety_json, social_json FROM snapshots WHERE run_id = ?",
        (run_id,),
    )
    items = _rows(
        conn,
        "SELECT ca, decision, score, rank, drop_reason, breakdown_json FROM run_items"
        " WHERE run_id = ?",
        (run_id,),
    )
    reports = _rows(
        conn,
        "SELECT ca, rank, verdict, status, payload_json FROM reports WHERE run_id = ? ORDER BY rank",
        (run_id,),
    )
    return {
        "run": dict(run),
        "summary": json.loads(summary["payload_json"]) if summary else {},
        # The project's own pages live in their own column, and the card's footer is the one
        # place that prints them. Merged here so the card and its links read one map instead of
        # each deciding where to look for a URL.
        "metrics": {r["ca"]: {**json.loads(r["metrics_json"]),
                              **(json.loads(r["social_json"] or "{}"))}
                    for r in snapshot},
        "safety": {r["ca"]: json.loads(r["safety_json"] or "{}") for r in snapshot},
        "items": {r["ca"]: r for r in items},
        "reports": reports,
    }


def cards(run_id: str) -> list[dict]:
    """The five cards, in rank order. Empty until the round has written them."""
    conn = db.connect()
    try:
        data = _load(conn, run_id)
    finally:
        conn.close()
    weights = _weights(data["summary"])
    new_syms = {
        c.get("sym") for c in data["summary"].get("changes", []) if c.get("direction") == "in"
    }
    out = []
    for report in data["reports"]:
        ca = report["ca"]
        metrics = data["metrics"].get(ca)
        if metrics is None:
            continue
        item = data["items"].get(ca) or {}
        breakdown = json.loads(item.get("breakdown_json") or "{}")
        written = json.loads(report["payload_json"] or "{}")
        if report.get("verdict") and not written.get("verdict"):
            written["verdict"] = report["verdict"]
        out.append(
            card_builder.card(
                rank=report["rank"] or (item.get("rank") or len(out) + 1),
                metrics=metrics,
                safety=data["safety"].get(ca) or {},
                written=written,
                breakdown=breakdown,
                is_new=metrics.get("symbol") in new_syms,
                onchain_weight=weights[0],
                offchain_weight=weights[1],
            )
        )
    return out


def _stamp(value: str) -> str:
    """A moment, in the shape the shelf prints it: to the minute and without the offset. The
    offset is real, but it is the same +08:00 on every row of one machine, so on a card it
    buys the reader nothing and costs the line its width."""
    return str(value or "")[:16].replace("T", " ")


def _kept_label(run: dict) -> str:
    """When a round ran, as the card's foot prints it."""
    return _stamp(run.get("started_at") or run.get("scheduled_at") or "")


def kept_row(run: dict, card: dict) -> dict:
    """One of a round's cards as a row for the shelf: the only place that shape is written.

    Both writers go through here - the reader keeping a card (`api/v1/favorites.py`) and a
    round refreshing the cards it superseded - so the two can never disagree about what a
    kept card says. `saved_at` is not part of it: that stamp is when the reader kept the
    token, and no round is the reader.
    """
    return {
        "chain_id": run["chain_id"],
        "ca": card["ca"],
        "symbol": card.get("sym"),
        "origin": "round",
        "origin_run_id": run["id"],
        "origin_label": _kept_label(run),
        "score": card.get("score"),
        "rank": card.get("rank"),
        "config_id": run["config_id"],
        "payload_json": json.dumps(card, ensure_ascii=False),
    }


def refresh_kept(run_id: str) -> int:
    """Hand the shelf the newer cards this round wrote, and answer how many it took.

    A kept card is somebody following a *token*, not a moment: the star is the same whether
    a newer card exists, so the shelf answers with the round that ran last. Two things this
    deliberately does not do - a card for a token nobody kept is not added, and a token this
    round did not write keeps the card it has (a token that fell out of the report has not
    become a worse token, it has stopped being reported on).
    """
    conn = db.connect()
    try:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if run is None:
        return 0
    run = dict(run)
    return db.favorite_refresh([kept_row(run, card) for card in cards(run_id)])


def lookup_row(chain_id: str, ca: str, symbol: str | None, card: dict, at: str) -> dict:
    """One card a CA search wrote, as a row for the table the shelf reads.

    Deliberately not a `kept_row`: there is no round here to take a settings id, a run id or
    a label from, and it says so with `origin = 'search'` and `kept = 0` - the row is a record
    of what the reader looked up, not something they follow. `kept_row` stays the single
    shape for round rows; merging the two would mean one function guessing which of its
    callers it is serving. `at` is the search's own clock, and it becomes the card's
    生成时间 - which is the honest reading of a card nothing else will refresh.
    """
    return {
        "chain_id": chain_id,
        "ca": ca,
        "symbol": symbol,
        "origin": "search",
        "origin_run_id": None,
        "origin_label": _stamp(at),
        "score": card.get("score"),
        "rank": None,
        "config_id": None,
        "kept": 0,
        "payload_json": json.dumps(card, ensure_ascii=False),
    }


def row_card(row: dict) -> dict:
    """A stored row as the API answers it: the payload comes back as the card it was.

    One function because the shelf and 最近搜索 answer the same shape - the same row, in fact,
    differing only in `kept` - and a card decoded differently in the two places would be the
    same card twice.
    """
    row = dict(row)
    row["card"] = json.loads(row.pop("payload_json") or "{}")
    return row


def decline(run_id: str, per_round: int = 5) -> list[dict]:
    """The candidates that did not make the cut, best score first.

    候选 is what the safety gate let through and 入选 is the cut taken out of it, so
    this table is the difference between the two numbers in its own heading. A candidate
    that was ranked and lost belongs here, and so does one that never got a number:
    dropping the second kind would leave the heading counting candidates that the rows
    below it do not show, which reads as a broken count. A survivor the gate never
    actually saw has no snapshot at all and is not here - it was never a candidate.

    A candidate with no number did not lose a race, so why it has no number is the whole
    explanation; the fact-level reasons would only restate the same gap three times.
    """
    conn = db.connect()
    try:
        data = _load(conn, run_id)
        floor = _market_cap_floor(_effective(conn, data["run"]))
    finally:
        conn.close()
    rows = []
    for ca, item in data["items"].items():
        if item["decision"] != "candidate":
            continue
        metrics = data["metrics"].get(ca)
        if metrics is None:
            continue
        if item.get("score") is None:
            reasons = _unranked_reasons(json.loads(item.get("breakdown_json") or "{}"))
        else:
            reasons = _decline_reasons(metrics, data["safety"].get(ca) or {}, floor)
        rows.append(
            card_builder.decline_row(
                {
                    "ca": ca,
                    "score": item.get("score"),
                    "reasons": reasons,
                    "per_round": per_round,
                },
                metrics,
            )
        )
    rows.sort(key=lambda row: -(row["score"] or 0))
    return rows


def _unranked_reasons(entry: dict) -> list[str]:
    """Why a candidate came out of the gate with no score.

    Two different things blank a total, and they must not read the same: the lexicon has
    no entry for the token, or the token has no numbers to score. `pending_review` on the
    stored breakdown is the first; everything else that can empty a total is the second.
    """
    if entry.get("pending_review"):
        return ["未建档（本轮不参与排名）"]
    return ["数据缺失，本轮不参与排名"]


def _effective(conn, run: dict) -> dict:
    """The settings this round actually ran under.

    A round is archived under the口径 of its time: the pinned version decides, not
    whatever happens to be in force when somebody opens the page. Every reader that
    explains a round - the口径 line, the落选 reasons - goes through here, so the page
    can never contradict itself.
    """
    try:
        cfg = config.chain(run["chain_id"], run.get("config_id"))
    except KeyError:
        return {}
    payload = {key: cfg.get(key) for key in SETTINGS_SECTIONS}
    version_id = run.get("settings_version_id")
    if version_id is not None:
        row = conn.execute(
            "SELECT payload_json FROM settings_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is not None:
            payload = config.merge_patch(payload, json.loads(row["payload_json"]))
    return {**cfg, **payload}


def _caliber(conn, run: dict, summary: dict) -> list[str]:
    """The口径 line rebuilt from the settings version this round points at."""
    settings = _effective(conn, run)
    if not settings:
        return summary.get("caliber", [])
    return forms.caliber_text(settings)


def _market_cap_floor(settings: dict) -> float | None:
    prefilter = ((settings.get("pipeline") or {}).get("prefilter") or {})
    floor = prefilter.get("market_cap_min_usd")
    return float(floor) if floor else None


def batch(run_id: str, per_round: int = 5) -> dict:
    """Everything the report page needs, in one payload."""
    conn = db.connect()
    try:
        data = _load(conn, run_id)
        caliber = _caliber(conn, data["run"], data["summary"])
    finally:
        conn.close()
    run = data["run"]
    summary = data["summary"]
    return {
        "run": run,
        "batch_label": summary.get("batch_label") or run.get("scheduled_at") or "",
        "funnel": {
            "scanned": run["scanned"] or 0,
            "dropped": run["dropped"] or 0,
            "candidates": run["candidates"] or 0,
            "selected": run["selected"] or 0,
        },
        "dropped_hard": summary.get("dropped_hard"),
        "gate": summary.get("gate") or {"judged": 0, "unjudged": 0},
        "rule_counts": summary.get("rule_counts", []),
        "changes": summary.get("changes", []),
        "caliber": caliber,
        "sources": summary.get("sources", []),
        "push": summary.get("push") or {"status": "", "delay": "", "unavailable": MISSING_BATCH},
        "note": summary.get("note", ""),
        # Per-phase wall clock, written while the round runs so the panel's timing column
        # has something to draw before the round is over. Empty for rounds written before
        # the stopwatch was stored.
        "phase_timings": summary.get("phase_timings", {}),
        "cards": cards(run_id),
        "decline": decline(run_id, per_round=per_round),
    }


def _decline_reasons(metrics: dict, safety: dict, floor: float | None) -> list[str]:
    """Why this one did not make the cut. Derived from facts on every read, never
    stored, so a round can always be re-read and re-explained.

    `floor` is this round's own市值 lower bound, so the reason and the口径 line can
    never quote two different numbers.
    """
    reasons = []
    if not metrics.get("holders"):  # no holder count at all is a gap, not a zero
        reasons.append("数据缺失")
    if metrics.get("honeypot_pct") is None:
        reasons.append("陷阱未知")
    lp = safety.get("lp_locked")
    if lp is not True:
        # Two states, and the third word is only for "neither source answered".
        reasons.append("LP 未锁定" if lp is False else "LP 未判定")
    cap = metrics.get("market_cap_usd")
    if floor is not None and cap is not None and cap < floor:
        reasons.append("市值 <%s" % forms._usd(floor))
    return reasons


def _weights(summary: dict) -> tuple[float, float]:
    scoring = summary.get("scoring") or {}
    onchain = float(scoring.get("onchain_weight", 55))
    product = float(scoring.get("offchain_weight", 45))
    return onchain, product
