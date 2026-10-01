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


def _run(run_id: str) -> sqlite3.Row | None:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()


@router.get("", response_model=list[FavoriteOut])
def list_favorites() -> list[dict]:
    """The shelf, newest kept first. Every row carries its card: the page draws cards."""
    return [report_assemble.row_card(row) for row in db.favorites()]


@router.get("/{chain_id}/{ca}", response_model=FavoriteOut)
def get_favorite(chain_id: str, ca: str) -> dict:
    row = db.favorite(chain_id, ca)
    if row is None:
        raise HTTPException(status_code=404, detail="没有收藏这个币：%s" % ca)
    return report_assemble.row_card(row)


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
    return report_assemble.row_card(db.favorite_put(report_assemble.kept_row(dict(run), card)))


@router.post("/{chain_id}/{ca}", response_model=FavoriteOut, status_code=201)
def collect_favorite(chain_id: str, ca: str) -> dict:
    """Collect a card that is already on file - the star on a searched token.

    A searched card has no round to copy one from: the CA search already wrote it
    (app/lookup.py). So this is the reader's star on a row that exists, and it only ever
    turns that row's `kept` on. 404 when there is no row to collect, which is a stale page
    rather than something worth inventing a card for.
    """
    if not db.favorite_set_kept(chain_id, ca, True):
        raise HTTPException(status_code=404, detail="没有可收藏的卡：%s" % ca)
    return report_assemble.row_card(db.favorite(chain_id, ca))


@router.delete("/{chain_id}/{ca}", response_model=RemovedOut)
def forget_favorite(chain_id: str, ca: str) -> dict:
    """Take one card off the shelf. A card that was already off it is not an error.

    What "off the shelf" means depends on where the card came from. A round's card exists
    only because it was collected, so unstarring it deletes the row. A searched card is also
    the reader's own record of looking that address up - unstarring it puts the row back to
    `kept = 0` instead, so it stays on 最近搜索 and can be collected again without paying for
    the search twice.
    """
    row = db.favorite(chain_id, ca)
    if row is not None and row.get("origin") == "search":
        return {"removed": db.favorite_set_kept(chain_id, ca, False)}
    return {"removed": db.favorite_remove(chain_id, ca)}
