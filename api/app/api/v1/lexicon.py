"""This round's off-chain judgement: what each shortlisted token is, why it matters, what
it risks.

The rows belong to the round that wrote them. The pass writes the whole block again every
round and the previous round's rows are deleted, so this endpoint answers "what did this
round find" - there is no archive to confirm and nothing to carry forward.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...report import research
from ...store import db
from .schemas import LexiconOut

router = APIRouter(prefix="/lexicon", tags=["lexicon"])


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        return _decorate([dict(r) for r in conn.execute(sql, args).fetchall()])
    finally:
        conn.close()


def _decorate(rows: list[dict]) -> list[dict]:
    """Add what a row cannot say about itself: the pages the reading pass kept, so an
    entry can show the evidence its words were written from."""
    return [{**row, "sources": research.sources_for(
        str(row["ca"]), row["chain_id"], row["config_id"])} for row in rows]


@router.get("", response_model=list[LexiconOut])
def list_lexicon(limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    return _rows("SELECT * FROM lexicon ORDER BY updated_at DESC LIMIT ?", (limit,))
