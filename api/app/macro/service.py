"""One macro slot: collect -> score -> cluster -> heat, plus the daily digest.

Runs on the same single-threaded pool as the screening rounds, so a macro
slot and a round never overlap. Idempotence belongs to the database like it
does for rounds: the hour slot has a unique run_key, so a tick that fires
twice collects once.

The model is not part of the truth here either. Without an endpoint the slot
still collects and lists - items just wait unscored, and the digest still
assembles its sections with an empty intro instead of invented prose.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..llm import client as llm_client
from ..store import db
from . import config as macro_config
from . import fetch as macro_fetch
from . import rules

log = logging.getLogger("crypto-hunter.macro")

PROMPT_VERSION = "macro-v1"
RECENT_DAYS = 14
CLUSTER_LOOKBACK = 200
SCORE_BATCH = 30

SCORE_SYS = (
    "You rate crypto news for professional investors. Reply with JSON only,"
    ' {"score": <0-5 integer>}. 5 = market-moving (major hack, ETF decision,'
    " large funding, top-tier listing); 3 = notable sector news; 1 = noise or"
    " PR with no substance; 0 = not about crypto."
)
WRITE_SYS = (
    "Rewrite one crypto news item for Chinese readers. Reply with JSON only:"
    ' {"title_zh": "<=24 chars, no outlet name>", "summary_zh": "2-3 sentences,'
    ' answer first", "reason": "one line on why it matters", "project": "main'
    ' project or empty", "sector": "one of SECTORS", "morph": "one of MORPHS"}.'
    " Never invent a project the source never named."
)
DIGEST_SYS = (
    "Write a 3-line Chinese morning briefing intro from these crypto headlines."
    " Plain text, no JSON, no markdown, no advice."
)


def now_in_tz(tzname: str) -> datetime:
    return datetime.now(ZoneInfo(tzname))


def current_slot(now: datetime | None = None, tzname: str = "Asia/Shanghai") -> str:
    moment = now or now_in_tz(tzname)
    return moment.strftime("%Y%m%d%H")


def run_key(slot: str) -> str:
    return "macro:" + slot


def slot_done(slot: str) -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status FROM macro_runs WHERE run_key = ?", (run_key(slot),)).fetchone()
    finally:
        conn.close()
    return row is not None and row["status"] == "done"


def run_slot(slot: str | None = None) -> dict:
    """Collect, score, cluster and heat one hour slot. Idempotent per slot."""
    cfg = macro_config.load_macro()
    slot = slot or current_slot()
    key = run_key(slot)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status, counts_json FROM macro_runs WHERE run_key = ?", (key,)).fetchone()
        if row is not None and row["status"] == "done":
            return json.loads(row["counts_json"] or "{}")
        if row is None:
            conn.execute(
                "INSERT INTO macro_runs (id, run_key, slot, status, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (uuid4().hex[:12], key, slot, "running", db.now_iso()),
            )
        else:
            conn.execute(
                "UPDATE macro_runs SET status = ?, started_at = ? WHERE run_key = ?",
                ("running", db.now_iso(), key),
            )
    finally:
        conn.close()
    counts: dict = {"slot": slot, "fetched": 0, "new": 0, "scored": 0,
                    "selected": 0, "events": 0, "errors": []}
    try:
        collected = collect_once(cfg)
        counts["fetched"] = collected["fetched"]
        counts["new"] = collected["new"]
        counts["errors"] = collected["errors"]
        scored = score_pending(cfg)
        counts["scored"] = scored["scored"]
        counts["selected"] = scored["selected"]
        clustered = cluster_unassigned(cfg)
        counts["events"] = clustered["created"] + clustered["attached"]
        counts["heated"] = refresh_all_heat(cfg)
        if _is_digest_slot(slot, cfg):
            digest = build_digest(cfg)
            counts["digest"] = digest.get("day")
        _finish_run(key, "done", counts)
    except Exception as exc:
        log.exception("macro slot %s failed", slot)
        counts["error"] = repr(exc)[:400]
        _finish_run(key, "failed", counts, error=counts["error"])
    return counts


def _finish_run(key: str, status: str, counts: dict, error: str | None = None) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE macro_runs SET status = ?, counts_json = ?, finished_at = ?, error = ?"
            " WHERE run_key = ?",
            (status, json.dumps(counts, ensure_ascii=False), db.now_iso(), error, key),
        )
    finally:
        conn.close()


def _is_digest_slot(slot: str, cfg: dict) -> bool:
    try:
        return int(slot[-2:]) == int(cfg["cadence"]["digest_hour"])
    except (ValueError, KeyError):
        return False


def collect_once(cfg: dict | None = None) -> dict:
    """Fetch every enabled source, insert what is new. Returns counts."""
    cfg = cfg or macro_config.load_macro()
    macro_config.sync_sources(cfg)
    fetched, new, errors = 0, 0, []
    conn = db.connect()
    try:
        for src in cfg["sources"]:
            if not src["enabled"]:
                continue
            try:
                items = macro_fetch.fetch(src)
            except macro_fetch.SourceFetchError as exc:
                errors.append({"source": src["id"], "error": str(exc)[:200]})
                continue
            fetched += len(items)
            for item in items:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                url = str(item.get("url") or "").strip() or None
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO macro_items"
                    " (source_id, fingerprint, url, title, summary, published_at,"
                    " fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (src["id"], rules.fingerprint(url, title), url, title[:300],
                     (str(item.get("summary") or "") or None),
                     item.get("published_at"), db.now_iso(), db.now_iso()),
                )
                new += cursor.rowcount
    finally:
        conn.close()
    return {"fetched": fetched, "new": new, "errors": errors}


def _llm_score(title: str, summary: str | None) -> float:
    data = llm_client.complete_json(
        [{"role": "system", "content": SCORE_SYS},
         {"role": "user", "content": "Title: %s\nSummary: %s" % (title, (summary or "")[:600])}],
        purpose="macro_score", prompt_version=PROMPT_VERSION, temperature=0.2)
    return max(0.0, min(5.0, float(data.get("score", 0))))


def _llm_write(title: str, summary: str | None, cfg: dict) -> dict:
    sectors = ",".join(cfg["topics"]["sectors"])
    morphs = ",".join(cfg["topics"]["morphs"])
    data = llm_client.complete_json(
        [{"role": "system", "content": WRITE_SYS + " SECTORS=%s MORPHS=%s" % (sectors, morphs)},
         {"role": "user", "content": "Title: %s\nSummary: %s" % (title, (summary or "")[:800])}],
        purpose="macro_write", prompt_version=PROMPT_VERSION, temperature=0.3)
    sector = str(data.get("sector") or "") or None
    if sector not in cfg["topics"]["sectors"]:
        sector = None
    morph = str(data.get("morph") or "") or None
    if morph not in cfg["topics"]["morphs"]:
        morph = None
    return {
        "title_zh": str(data.get("title_zh") or "")[:80] or None,
        "summary_zh": str(data.get("summary_zh") or "")[:600] or None,
        "reason": str(data.get("reason") or "")[:300] or None,
        "project": str(data.get("project") or "")[:80] or None,
        "sector": sector,
        "morph": morph,
    }


def score_pending(cfg: dict | None = None, limit: int = SCORE_BATCH) -> dict:
    """Prescreen everything pending, score survivors twice, write the picked."""
    cfg = cfg or macro_config.load_macro()
    sel = cfg["selection"]
    scored, selected, skipped_llm = 0, 0, 0
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM macro_items WHERE score1 IS NULL ORDER BY fetched_at LIMIT ?",
            (limit,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        published = rules.parse_dt(row["published_at"])
        keep, reason = rules.prescreen(
            row["title"], row["summary"], max_age_days=int(sel["prescreen_max_age_days"]),
            published_at=published)
        if not keep:
            _mark(row["id"], 0.0, 0.0, False, reason, None)
            scored += 1
            continue
        if not llm_client.configured():
            skipped_llm += 1
            continue
        try:
            first = _llm_score(row["title"], row["summary"])
            second = _llm_score(row["title"], row["summary"])
        except Exception as exc:
            log.warning("macro score skipped item %s: %r", row["id"], exc)
            skipped_llm += 1
            continue
        avg, picked = rules.combine_scores(
            first, second, threshold=float(sel["score_threshold"]),
            min_single=float(sel["min_single_score"]))
        write = None
        if picked:
            try:
                write = _llm_write(row["title"], row["summary"], cfg)
            except Exception as exc:
                log.warning("macro write skipped item %s: %r", row["id"], exc)
        _mark(row["id"], first, second, picked, "llm", write, avg)
        scored += 1
        selected += 1 if picked else 0
    return {"scored": scored, "selected": selected, "skipped_llm": skipped_llm}


def _mark(item_id: int, first: float, second: float, picked: bool,
          reason: str, write: dict | None, avg: float | None = None) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE macro_items SET score1 = ?, score2 = ?, score_avg = ?, selected = ?,"
            " reason = ?, title_zh = ?, summary_zh = ?, project = ?, sector = ?, morph = ?"
            " WHERE id = ?",
            (first, second, first if avg is None else avg, 1 if picked else 0, reason,
             (write or {}).get("title_zh"), (write or {}).get("summary_zh"),
             (write or {}).get("project"), (write or {}).get("sector"),
             (write or {}).get("morph"), item_id),
        )
    finally:
        conn.close()


def _match_title(row: dict) -> str:
    return row["title_zh"] or row["title"]


def cluster_unassigned(cfg: dict | None = None) -> dict:
    """Attach selected-but-lonely items to recent events, or open new ones."""
    cfg = cfg or macro_config.load_macro()
    cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=RECENT_DAYS))
    conn = db.connect()
    try:
        items = conn.execute(
            "SELECT mi.* FROM macro_items mi LEFT JOIN macro_event_items mei"
            " ON mei.item_id = mi.id WHERE mi.selected = 1 AND mei.item_id IS NULL"
            " ORDER BY COALESCE(mi.published_at, mi.fetched_at) LIMIT 100").fetchall()
        events = conn.execute(
            "SELECT id, title FROM macro_events WHERE locked = 0 AND last_seen >= ?"
            " ORDER BY last_seen DESC LIMIT ?",
            (cutoff.isoformat(timespec="seconds"), CLUSTER_LOOKBACK)).fetchall()
    finally:
        conn.close()
    attached, created = 0, 0
    for item in items:
        title = _match_title(dict(item))
        target = next((ev for ev in events
                       if rules.should_merge(ev["title"], title)), None)
        if target is None:
            event_id = uuid4().hex[:8]
            _open_event(event_id, dict(item), title, cfg)
            events.append({"id": event_id, "title": title})
            created += 1
            target_id = event_id
        else:
            target_id = target["id"]
            _attach(target_id, item["id"], manual=False)
            attached += 1
        _refresh_event(target_id, cfg)
    return {"attached": attached, "created": created}


def _open_event(event_id: str, item: dict, title: str, cfg: dict) -> None:
    del cfg
    seen = item.get("published_at") or item.get("fetched_at") or db.now_iso()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO macro_events (id, title, summary, first_seen, last_seen, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, title[:200], (item.get("summary_zh") or item.get("summary") or "")[:600],
             seen, seen, db.now_iso()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO macro_event_items (event_id, item_id) VALUES (?, ?)",
            (event_id, item["id"]),
        )
    finally:
        conn.close()


def _attach(event_id: str, item_id: int, manual: bool = False) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO macro_event_items (event_id, item_id, manual)"
            " VALUES (?, ?, ?)", (event_id, item_id, 1 if manual else 0),
        )
    finally:
        conn.close()


def attach_manual(event_id: str, item_id: int) -> None:
    """A hand-moved item stays moved: manual rows are never auto-detached and
    locked events are never merge targets."""
    conn = db.connect()
    try:
        conn.execute(
            "DELETE FROM macro_event_items WHERE item_id = ? AND manual = 0", (item_id,))
        conn.execute(
            "INSERT OR REPLACE INTO macro_event_items (event_id, item_id, manual)"
            " VALUES (?, ?, 1)", (event_id, item_id),
        )
        conn.execute("UPDATE macro_events SET locked = 1 WHERE id = ?", (event_id,))
    finally:
        conn.close()
    _refresh_event(event_id, macro_config.load_macro())


def _refresh_event(event_id: str, cfg: dict, now: datetime | None = None) -> None:
    moment = now or datetime.now(timezone.utc)
    window = float(cfg["heat"]["window_hours"])
    half = float(cfg["heat"]["half_life_hours"])
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT mi.source_id, mi.published_at, mi.fetched_at FROM macro_event_items mei"
            " JOIN macro_items mi ON mi.id = mei.item_id WHERE mei.event_id = ?",
            (event_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        conn = db.connect()
        try:
            conn.execute("DELETE FROM macro_events WHERE id = ?", (event_id,))
        finally:
            conn.close()
        return
    earliest: dict[str, datetime] = {}
    stamps: list[datetime] = []
    for row in rows:
        parsed = rules.parse_dt(row["published_at"]) or rules.parse_dt(row["fetched_at"])
        if parsed is None:
            continue
        stamps.append(parsed)
        if row["source_id"] not in earliest or parsed < earliest[row["source_id"]]:
            earliest[row["source_id"]] = parsed
    ages = [(moment - stamp).total_seconds() / 3600.0
            for stamp in earliest.values()
            if (moment - stamp).total_seconds() / 3600.0 <= window]
    value = rules.heat(ages, half_life_hours=half)
    first = min(stamps).isoformat(timespec="seconds")
    last = max(stamps).isoformat(timespec="seconds")
    recent = sum(1 for stamp in earliest.values()
                 if (moment - stamp).total_seconds() / 3600.0 <= 6)
    is_new, is_rising = rules.event_flags(min(stamps), recent, now=moment)
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE macro_events SET heat = ?, sources_count = ?, items_count = ?,"
            " is_new = ?, is_rising = ?, first_seen = ?, last_seen = ?, updated_at = ?"
            " WHERE id = ?",
            (value, len(earliest), len(rows), 1 if is_new else 0, 1 if is_rising else 0,
             first, last, db.now_iso(), event_id),
        )
    finally:
        conn.close()


def refresh_all_heat(cfg: dict | None = None, now: datetime | None = None) -> int:
    cfg = cfg or macro_config.load_macro()
    conn = db.connect()
    try:
        ids = [row["id"] for row in conn.execute("SELECT id FROM macro_events")]
    finally:
        conn.close()
    for event_id in ids:
        _refresh_event(event_id, cfg, now)
    return len(ids)


def build_digest(cfg: dict | None = None, day: str | None = None,
                 kind: str = "daily") -> dict:
    """Deterministic sections plus one model-written intro. The intro may be
    empty when no endpoint answers; the sections never are."""
    cfg = cfg or macro_config.load_macro()
    day = day or now_in_tz(str(cfg["cadence"]["timezone"])).strftime("%Y-%m-%d")
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT mi.*, ms.name AS source_name FROM macro_items mi"
            " LEFT JOIN macro_sources ms ON ms.id = mi.source_id"
            " WHERE mi.selected = 1 AND substr(COALESCE(mi.published_at, mi.fetched_at), 1, 10) = ?"
            " ORDER BY mi.score_avg DESC LIMIT ?",
            (day, int(cfg["selection"]["keep_per_day"]))).fetchall()
        events = conn.execute(
            "SELECT id, title, heat FROM macro_events"
            " WHERE substr(last_seen, 1, 10) = ? ORDER BY heat DESC LIMIT 10",
            (day,)).fetchall()
    finally:
        conn.close()
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["sector"] or "General", []).append(dict(row))
    sections = [{"sector": sector, "items": items[:8]}
                for sector, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))]
    heads = ["%s (%s)" % (row["title_zh"] or row["title"], row["source_name"] or row["source_id"])
             for row in rows[:12]]
    intro = ""
    if heads and llm_client.configured():
        try:
            intro = llm_client.complete(
                [{"role": "system", "content": DIGEST_SYS},
                 {"role": "user", "content": "Day %s:\n- %s" % (day, "\n- ".join(heads))}],
                purpose="macro_digest", prompt_version=PROMPT_VERSION,
                temperature=0.3, timeout=60.0)
        except Exception as exc:
            log.warning("macro digest intro skipped: %r", exc)
    payload = {"kind": kind, "day": day, "intro": intro, "sections": [
        {"sector": sec["sector"], "items": [
            {"id": it["id"], "title": it["title_zh"] or it["title"], "url": it["url"],
             "source": it.get("source_name") or it["source_id"],
             "project": it["project"], "morph": it["morph"],
             "score": it["score_avg"]} for it in sec["items"]]} for sec in sections],
        "events": [{"event_id": ev["id"], "title": ev["title"], "heat": ev["heat"]}
                   for ev in events],
        "generated_at": db.now_iso()}
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO macro_digests (kind, day, payload_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (kind, day, json.dumps(payload, ensure_ascii=False), db.now_iso()),
        )
    finally:
        conn.close()
    return payload