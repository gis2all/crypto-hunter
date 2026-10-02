"""Macro track reads: items, events, digests, topics, search, event shelf.

One screen is one endpoint: the hotspot pages draw from these and never stitch
several requests together. Writes are the manual collect trigger and the event
shelf; everything else is written by the hourly slot, never by a reader.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...macro import service as macro_service
from ...pipeline import schedule
from ...store import db
from .schemas import (MacroCollectOut, MacroDigestOut, MacroEventDetailOut,
                      MacroEventOut, MacroFavoriteIn, MacroItemOut,
                      MacroTopicOut, RemovedOut)

router = APIRouter(prefix="/macro", tags=["macro"])


def _item(row: dict) -> dict:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "source_name": row["source_name"] if "source_name" in row.keys() else None,
        "url": row["url"],
        "title": row["title"],
        "summary": row["summary"],
        "published_at": row["published_at"],
        "score_avg": row["score_avg"],
        "selected": bool(row["selected"]),
        "title_zh": row["title_zh"],
        "summary_zh": row["summary_zh"],
        "reason": row["reason"],
        "project": row["project"],
        "sector": row["sector"],
        "morph": row["morph"],
    }


def _event(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "heat": row["heat"],
        "sources_count": row["sources_count"],
        "items_count": row["items_count"],
        "is_new": bool(row["is_new"]),
        "is_rising": bool(row["is_rising"]),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


@router.get("/items", response_model=list[MacroItemOut])
def list_items(selected_only: bool = False, limit: int = Query(50, le=200),
               q: str | None = None) -> list[dict]:
    """The feed: everything collected, or the picked ones only."""
    sql = ("SELECT mi.*, ms.name AS source_name FROM macro_items mi"
           " LEFT JOIN macro_sources ms ON ms.id = mi.source_id")
    conds, args = [], []
    if selected_only:
        conds.append("mi.selected = 1")
    if q:
        conds.append("(mi.title LIKE ? OR COALESCE(mi.summary, '') LIKE ?"
                     " OR COALESCE(mi.title_zh, '') LIKE ?)")
        args += ["%%%s%%" % q] * 3
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY mi.score_avg DESC, mi.fetched_at DESC LIMIT ?"
    args.append(limit)
    conn = db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_item(dict(row)) for row in rows]


@router.get("/events", response_model=list[MacroEventOut])
def list_events(sort: str = "heat", limit: int = Query(50, le=200)) -> list[dict]:
    """The hotspot board, hottest first or latest first."""
    order = "last_seen DESC" if sort == "latest" else "heat DESC"
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM macro_events ORDER BY %s LIMIT ?" % order, (limit,)).fetchall()
    finally:
        conn.close()
    return [_event(dict(row)) for row in rows]


@router.get("/events/{event_id}", response_model=MacroEventDetailOut)
def get_event(event_id: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM macro_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such event")
        items = conn.execute(
            "SELECT mi.*, ms.name AS source_name FROM macro_event_items mei"
            " JOIN macro_items mi ON mi.id = mei.item_id"
            " LEFT JOIN macro_sources ms ON ms.id = mi.source_id"
            " WHERE mei.event_id = ? ORDER BY COALESCE(mi.published_at, mi.fetched_at)",
            (event_id,)).fetchall()
    finally:
        conn.close()
    out = _event(dict(row))
    out["items"] = [_item(dict(item)) for item in items]
    return out


@router.get("/digests/latest", response_model=MacroDigestOut)
def latest_digest(kind: str = "daily") -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM macro_digests WHERE kind = ? ORDER BY day DESC LIMIT 1",
            (kind,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such digest")
    import json
    return json.loads(row["payload_json"])


@router.get("/digests/{day}", response_model=MacroDigestOut)
def get_digest(day: str, kind: str = "daily") -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM macro_digests WHERE kind = ? AND day = ?", (kind, day)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such digest")
    import json
    return json.loads(row["payload_json"])


@router.get("/topics", response_model=list[MacroTopicOut])
def list_topics() -> list[dict]:
    """The three label dimensions with counts, over the picked items."""
    out = []
    conn = db.connect()
    try:
        for kind, column in (("project", "project"), ("sector", "sector"),
                             ("morph", "morph")):
            rows = conn.execute(
                "SELECT %s AS name, COUNT(*) AS n FROM macro_items"
                " WHERE selected = 1 AND %s IS NOT NULL GROUP BY %s ORDER BY n DESC"
                % (column, column, column)).fetchall()
            out += [{"kind": kind, "name": row["name"], "count": row["n"]} for row in rows]
    finally:
        conn.close()
    return out


@router.get("/search", response_model=list[MacroItemOut])
def search_items(q: str, limit: int = Query(30, le=100)) -> list[dict]:
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="query too short")
    return list_items(selected_only=False, limit=limit, q=q.strip())


@router.get("/favorites", response_model=list[MacroEventOut])
def list_favorites() -> list[dict]:
    """The event half of the shelf: coin cards stay on /favorites."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT e.* FROM macro_favorites f JOIN macro_events e ON e.id = f.event_id"
            " ORDER BY f.saved_at DESC").fetchall()
    finally:
        conn.close()
    return [_event(dict(row)) for row in rows]


@router.post("/favorites", response_model=MacroEventOut, status_code=201)
def keep_favorite(body: MacroFavoriteIn) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM macro_events WHERE id = ?",
                           (body.event_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such event")
        conn.execute("INSERT OR IGNORE INTO macro_favorites (event_id, saved_at)"
                     " VALUES (?, ?)", (body.event_id, db.now_iso()))
    finally:
        conn.close()
    return _event(dict(row))


@router.delete("/favorites/{event_id}", response_model=RemovedOut)
def forget_favorite(event_id: str) -> dict:
    conn = db.connect()
    try:
        cursor = conn.execute("DELETE FROM macro_favorites WHERE event_id = ?",
                              (event_id,))
    finally:
        conn.close()
    return {"removed": cursor.rowcount > 0}


@router.post("/collect", response_model=MacroCollectOut, status_code=202)
def trigger_collect() -> dict:
    """Run the current hour slot now. Accepted immediately; the slot itself is
    idempotent, so a tick landing in between collects nothing twice."""
    slot = macro_service.current_slot()
    accepted = schedule.submit_macro(slot)
    return {"accepted": accepted, "slot": slot}