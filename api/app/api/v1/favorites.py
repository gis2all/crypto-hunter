"""Kept cards: the shelf, and the one write that puts a round card on it.

A round report row lives and dies with its round, and a whole report library can be dropped,
so the shelf keeps a copy of the assembled card rather than a pointer to it. That is also
what lets a card come from somewhere that is not a round at all later - a chain this app does
not screen, searched by CA - without the shelf changing shape. The row is keyed by
(chain_id, ca); see store/schema.sql.

The card is *copied* the moment it is kept, deltas and all: re-deriving it on read would mean
reading it against a newer round, and a copy has to keep reading after that round and its
whole report library are gone. What it is not is frozen for good - a card on the shelf stands
for the token, not for a moment, so a round that writes that token again hands the shelf its
newer card (report/assemble.py `refresh_kept`) and the reader finds the current answer
without having to come back and ask for it.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...report import assemble as report_assemble
from ...store import db
from .schemas import FavoriteOut, RemovedOut

router = APIRouter(prefix="/favorites", tags=["favorites"])


class FavoriteIn(BaseModel):
    """A card to keep: the round it came from, and which of that round cards."""

    run_id: str
    ca: str


def _card(row: dict) -> dict:
    """The row as the API answers it: the stored payload comes back as the card it was."""
    row = dict(row)
    row["card"] = json.loads(row.pop("payload_json") or "{}")
    return row


def _run(run_id: str) -> sqlite3.Row | None:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()


@router.get("", response_model=list[FavoriteOut])
def list_favorites() -> list[dict]:
    """The shelf, newest kept first. Every row carries its card: the page draws cards."""
    return [_card(row) for row in db.favorites()]


@router.get("/{chain_id}/{ca}", response_model=FavoriteOut)
def get_favorite(chain_id: str, ca: str) -> dict:
    row = db.favorite(chain_id, ca)
    if row is None:
        raise HTTPException(status_code=404, detail="没有收藏这个币：%s" % ca)
    return _card(row)


@router.post("", response_model=FavoriteOut, status_code=201)
def keep_favorite(body: FavoriteIn) -> dict:
    """Keep the card this round wrote for this token, as the report page drew it.

    Taken from report.assemble.cards rather than from the raw report payload: the page is
    already drawn from that assembly (the new/continuing flag, the deltas against the round
    before, the two weights), and freezing anything less would mean re-deriving those on
    read - against a later round. The row itself is shaped by that same module, so the card a
    reader keeps and the card a later round refreshes it with are one shape.
    """
    run = _run(body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="没有这一轮：%s" % body.run_id)
    card = next((c for c in report_assemble.cards(body.run_id) if c["ca"] == body.ca), None)
    if card is None:
        raise HTTPException(status_code=404,
                            detail="这一轮没有写出这个币的报告卡：%s" % body.ca)
    return _card(db.favorite_put(report_assemble.kept_row(dict(run), card)))


@router.delete("/{chain_id}/{ca}", response_model=RemovedOut)
def forget_favorite(chain_id: str, ca: str) -> dict:
    """Take one card off the shelf. A card that was already gone is not an error."""
    return {"removed": db.favorite_remove(chain_id, ca)}
