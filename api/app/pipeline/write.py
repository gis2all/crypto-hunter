"""Persist a finished round. The only writer for runs, run_items, snapshots and run_summary.

Two rules shape this module.

**Numbers come from the snapshot, words come from the report.** The card derives every
metric from `snapshots.metrics_json`; `reports.payload_json` carries only what a writer
produced - the tag, the verdict, the sections, the note. Recording a second copy of a
number is how the two copies start to disagree.

**A round is written once.** Everything here is an upsert keyed by (run_id, ca), so a
re-driven round overwrites its own rows instead of doubling them.
"""

from __future__ import annotations

import json

from ..api.v1 import forms
from .. import out
from ..core import filters
from ..report import lexicon
from .decide import RoundResult

#: The rules, in the order the settings page and the overview draw them. The order is the
#: attribution order (core/filters.py) and it is what makes the column add up.
RULE_ROWS: tuple[tuple[str, str], ...] = (
    ("market_cap", "市值"),
    ("liquidity", "流动性"),
    ("holders", "持有人"),
    ("top10", "Top10 集中度"),
    ("bundling", "捆绑率"),
    ("honeypot", "貔貅"),
    ("volume_24h", "24h 成交"),
    ("source_unverified", "源码已验证"),
    ("lp_unlocked", "LP 已锁"),
    ("dangerous_perm", "无危险权限"),
    ("entrapment", "陷阱占比"),
    ("empty_shell", "空壳淘汰"),
    ("missing_data", "零数据淘汰"),
    ("market_cap_drawdown", "距高回撤"),
)
RULE_LABELS = dict(RULE_ROWS)

#: The same guard as the rule that reads the column: a high no band on this chain can
#: explain is a parsing artefact, and recording one would make every later round read that
#: token as down 100% (`core/filters.HIGH_SANITY_MULTIPLE`).
PEAK_SANITY_MULTIPLE = filters.HIGH_SANITY_MULTIPLE


#: Push is out of scope for now, and the round says so rather than claiming a delivery.
#: The push row is the outlet module's business, not the writer's: what the page shows and
#: what decides whether a round can finish have to be the same string.
PUSH_STATUS = out.status()

#: The four numbers the strip draws, which live in `runs`. Every other keyword
#: `store_funnel` takes is a key of the run_summary payload. The split is named here so a
#: stage cannot write a summary key the page never reads and call it done.
STRIP_FIELDS = ("scanned", "dropped", "candidates", "selected")


def store_funnel(run_id: str, **fields) -> None:
    """The strip's four numbers and the panel's rows, written while the round runs.

    `store_round` writes them all at the end, which is why a running round used to draw a
    panel of nothing but dashes: the page reads `runs`, and nothing had put the numbers
    there yet. This writes the same fields, only earlier, and only the ones a stage has
    actually produced - an absent key means "not yet", never "zero".

    Summary keys merge into what is already stored instead of replacing the payload, so the
    sources counted at the pull survive the gate reporting its own numbers. A replacement
    would make the panel flicker as every stage blanked the stage before it.
    """
    from ..store import db

    fields = {key: value for key, value in fields.items() if value is not None}
    if not fields:
        return
    if "rule_counts" in fields:
        # The round carries the pairs; the page wants the rows `store_round` writes, with
        # the labels and the zero-fill. Building them here keeps both writers quoting one
        # rule list instead of two that drift.
        fields["rule_counts"] = _rule_count_rows(fields["rule_counts"])
    strip = {key: fields.pop(key) for key in STRIP_FIELDS if key in fields}
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        if strip:
            conn.execute(
                "UPDATE runs SET scanned = COALESCE(?, scanned), dropped = COALESCE(?, dropped),"
                " candidates = COALESCE(?, candidates), selected = COALESCE(?, selected)"
                " WHERE id = ?",
                (strip.get("scanned"), strip.get("dropped"), strip.get("candidates"),
                 strip.get("selected"), run_id),
            )
        if fields:
            _merge_summary(conn, run_id, fields)
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def store_span(run_id: str, phase: str, seconds: float) -> None:
    """One phase's wall clock, stored the moment the phase ends.

    The round already keeps this stopwatch (`progress.Timer`) and already prints the split
    to the log; this is what puts the same numbers on the page, where the run panel's timing
    column has been a hard-coded dash since the panel was written.
    """
    from ..store import db

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        payload = _summary_payload(conn, run_id)
        timings = dict(payload.get("phase_timings") or {})
        timings[phase] = round(float(seconds), 1)
        _merge_summary(conn, run_id, {"phase_timings": timings})
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _summary_payload(conn, run_id: str) -> dict:
    row = conn.execute(
        "SELECT payload_json FROM run_summary WHERE run_id = ?", (run_id,)
    ).fetchone()
    return json.loads(row["payload_json"]) if row and row["payload_json"] else {}


def _merge_summary(conn, run_id: str, fields: dict) -> None:
    """Add keys to the round's payload without dropping the ones already in it."""
    payload = _summary_payload(conn, run_id)
    payload.update(fields)
    conn.execute(
        "INSERT OR REPLACE INTO run_summary (run_id, payload_json, created_at) VALUES (?, ?, ?)",
        (run_id, json.dumps(payload, ensure_ascii=False), _now()),
    )


def _rule_count_rows(counts) -> list[dict]:
    """The rows `store_round` writes, built from the pairs a running round has so far."""
    given = dict(counts)
    return [
        {"key": key, "label": RULE_LABELS.get(key, key), "count": given.get(key, 0)}
        for key, _label in RULE_ROWS
    ]


def store_round(conn, result: RoundResult) -> None:
    """Runs, run_items, snapshots, run_summary and the fact-only report rows."""
    settings = result.settings
    counts = dict(result.rule_counts)
    ceiling = ((settings.get("pipeline") or {}).get("prefilter") or {}).get("market_cap_max_usd")
    # The strip's four numbers all come from the items' own decisions, in one pass, so
    # 扫描 is by construction 淘汰 + 候选 + 入选. They used to come from four different
    # places: 淘汰 was the prefilter subtotal, 候选 was what the gate scored, and the
    # strip quietly stopped adding up.
    funnel_summary = result.funnel_summary
    conn.execute(
        "UPDATE runs SET scanned = ?, dropped = ?, candidates = ?, selected = ? WHERE id = ?",
        (
            funnel_summary.scanned,
            funnel_summary.dropped,
            funnel_summary.candidates,
            funnel_summary.selected,
            result.run_id,
        ),
    )

    for item in result.items.values():
        # Every token in the universe gets a row, not only the survivors: the row is where
        # "why is this one not here" is answerable, and the answer is worth having for the
        # 4000 tokens that never reached a card.
        #
        # The breakdown goes in whole. The card draws 链上质量 / 链下调研 / 总分 and the
        # factor bars from this row, so leaving the numbers out here would blank them.
        payload = dict(item.breakdown.model_dump()) if item.breakdown else {}
        payload.update(
            {
                "failed": item.failed,
                "unknown": item.unknown,
                "pending_review": bool(payload.get("pending_review")),
                "notes": payload.get("notes") or [],
            }
        )
        conn.execute(
            "INSERT OR REPLACE INTO run_items (run_id, ca, decision, drop_reason, score, rank,"
            " breakdown_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result.run_id, item.ca, item.decision, (item.failed or [None])[0], item.score,
             item.rank, json.dumps(payload, ensure_ascii=False)),
        )
        # The tokens row is written for every scanned token, not only the ones that reach a
        # card. 距高回撤 reads its high out of `peak_market_cap_usd`, and the fall that rule
        # is about happens to tokens that ran above this chain band and came back down - a
        # row written only for the candidates would cap the high at the band itself.
        peak = item.metrics.market_cap_usd
        # The high a source measured is not a reading of today price: it is the number
        # 距高回撤 compares against, and writing it back is what turns that rule free half
        # into a real high instead of whatever our own rounds happened to sample. Once
        # written it stays - the upsert below only ever raises this column - so one round
        # that saw the high is enough for every round after it.
        high = item.metrics.high_market_cap_usd
        if high is not None and (peak is None or high > peak):
            peak = high
        if peak is not None and ceiling and peak > ceiling * PEAK_SANITY_MULTIPLE:
            # Not this token high, and not this token: a number no band on this chain can
            # explain. Left unwritten rather than written low, so the row keeps the high
            # the earlier rounds measured instead of losing it to a bad read.
            peak = None
        stamp = _now()
        conn.execute(
            "INSERT INTO tokens (ca, chain_id, symbol, decimals, launchpad, created_at,"
            " first_seen_run, updated_at, peak_market_cap_usd, peak_market_cap_at)"
            " VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)"
            " ON CONFLICT(ca) DO UPDATE SET"
            " symbol=excluded.symbol, launchpad=excluded.launchpad,"
            " first_seen_run=excluded.first_seen_run, updated_at=excluded.updated_at,"
            " peak_market_cap_usd=MAX(tokens.peak_market_cap_usd, excluded.peak_market_cap_usd),"
            " peak_market_cap_at=CASE WHEN excluded.peak_market_cap_usd IS NOT NULL"
            " AND (tokens.peak_market_cap_usd IS NULL"
            " OR excluded.peak_market_cap_usd > tokens.peak_market_cap_usd)"
            " THEN excluded.peak_market_cap_at ELSE tokens.peak_market_cap_at END",
            (item.ca, result.chain_id, item.symbol, item.metrics.launchpad, result.run_id,
             stamp, peak, stamp if peak is not None else None),
        )
        if item.decision not in ("candidate", "selected") and not item.enriched:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (result.run_id, item.ca,
             json.dumps(item.metrics.model_dump(mode="json"), ensure_ascii=False),
             json.dumps(item.facts.model_dump(mode="json"), ensure_ascii=False),
             json.dumps({"socials": item.metrics.socials, "websites": item.metrics.websites},
                        ensure_ascii=False),
             _now()),
        )

    push_delay = int((settings.get("schedule") or {}).get("push_delay_minutes") or 0)
    scoring = (settings.get("pipeline") or {}).get("scoring") or {}
    summary = {
        "batch_label": _batch_label(result.run_id, settings),
        "rule_counts": _rule_count_rows(counts),
        "changes": result.changes,
        "caliber": forms.caliber_text({**settings, "id": result.chain_id}),
        "sources": result.sources,
        # The run panel's 淘汰 row is the strip's own number: the filter rejects a token
        # before the round spends a token on it, so there is no third column to subtract.
        # Written here, where the items are, rather than derived on the page.
        "dropped_hard": result.filter_dropped,
        "gate": {
            "judged": len(result.survivors) - result.unjudged,
            "unjudged": result.unjudged,
        },
        "push": {"status": PUSH_STATUS, "delay": "扫描 +%d 分钟" % push_delay},
        "scoring": {
            "onchain_weight": scoring.get("onchain_weight", 55),
            "offchain_weight": scoring.get("offchain_weight", 45),
        },
        "note": " ".join(result.notes),
        "source_reports": [report.model_dump() for report in result.reports],
    }
    # Merged, not replaced: since the pull this round has been writing its own keys here
    # (the live funnel, the phase timings) and a whole-payload write would throw them away
    # at the finish line - which is the moment someone actually reads them.
    _merge_summary(conn, result.run_id, summary)


def store_reports(conn, result: RoundResult) -> None:
    """One report row per pick: the numbers from the snapshot, the words from its entry.

    The entry is already final when this runs - the reading pass wrote it before the score -
    so there is no first version to flag and no second version to land.
    """
    for item in result.picked:
        payload = lexicon.report_payload(item.entry, item.deltas)
        conn.execute(
            "INSERT OR REPLACE INTO reports (run_id, ca, rank, verdict, status, payload_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result.run_id, item.ca, item.rank or 0, payload["verdict"], "written",
             json.dumps(payload, ensure_ascii=False), _now()),
        )


def _now() -> str:
    from ..store import db

    return db.now_iso()


def _batch_label(run_id: str, settings: dict) -> str:
    """The batch's own clock, in the chain's timezone, the way the header shows it."""
    from datetime import datetime

    from ..store import db

    conn = db.connect()
    try:
        row = conn.execute("SELECT scheduled_at, started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    stamp = (row["scheduled_at"] if row else None) or (row["started_at"] if row else None)
    try:
        moment = datetime.fromisoformat(stamp) if stamp else datetime.now().astimezone()
    except ValueError:
        moment = datetime.now().astimezone()
    del settings
    return moment.strftime("%Y-%m-%d %H:%M +08")
