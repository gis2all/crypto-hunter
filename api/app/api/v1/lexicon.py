"""The lexicon: what each token is, why it matters, what it risks.

The LLM drafts entries, a human confirms them, and only confirmed entries score.
This is the one asset in the system that compounds.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ... import config
from ...core import lexicon as lexicon_rules
from ...report import research
from ...store import db
from .schemas import LexiconOut

router = APIRouter(prefix="/lexicon", tags=["lexicon"])

FIELDS = (
    "ca", "symbol", "tag", "narrative", "product", "social", "concl", "narr_text",
    "prod_text", "mech_text", "pros", "cons", "watch", "scenario", "review_state",
    "evidence_json", "updated_at",
)


class LexiconIn(BaseModel):
    symbol: str | None = None
    tag: str | None = None
    narrative: float | None = None
    product: float | None = None
    social: str | None = None
    concl: str | None = None
    narr_text: str | None = None
    prod_text: str | None = None
    mech_text: str | None = None
    pros: str | None = None
    cons: str | None = None
    watch: str | None = None
    scenario: str | None = None
    review_state: str = "pending"
    evidence_json: str | None = None


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        return _decorate([dict(r) for r in conn.execute(sql, args).fetchall()])
    finally:
        conn.close()


def _decorate(rows: list[dict]) -> list[dict]:
    """Add what a row cannot say about itself: how old the entry is, whether today rules
    would rewrite it, and the pages the deep pass read.

    Computed here rather than in the page on purpose. The refresh rules are one function in
    `core.lexicon`; a frontend that re-derived "is this stale" from `updated_at` would be a
    second copy of the rule, and the copy that goes wrong is always the one nobody tests.
    """
    now = datetime.now().astimezone()
    cfgs: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        entry = dict(row)
        chain_id = str(entry.get("chain_id") or "arc")
        if chain_id not in cfgs:
            try:
                pipeline = config.chain(chain_id).get("pipeline") or {}
                cfgs[chain_id] = pipeline.get("lexicon") or {}
            except KeyError:  # a row for a chain this build does not know
                cfgs[chain_id] = {}
        verdict = lexicon_rules.due(entry, {}, cfgs[chain_id], now)
        entry["age_hours"] = None if verdict.age_hours is None else round(verdict.age_hours, 1)
        entry["stale"] = verdict.action == "rewrite"
        entry["sources"] = research.sources_for(str(entry["ca"]))
        out.append(entry)
    return out


@router.get("", response_model=list[LexiconOut])
def list_lexicon(
    review_state: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    if review_state:
        return _rows(
            "SELECT * FROM lexicon WHERE review_state = ? ORDER BY updated_at DESC LIMIT ?",
            (review_state, limit),
        )
    return _rows("SELECT * FROM lexicon ORDER BY updated_at DESC LIMIT ?", (limit,))


@router.get("/pending", response_model=list[LexiconOut])
def pending() -> list[dict]:
    return _rows("SELECT * FROM lexicon WHERE review_state = 'pending' ORDER BY updated_at DESC")


@router.put("/{ca}", response_model=LexiconOut)
def upsert(ca: str, body: LexiconIn) -> dict:
    values = body.model_dump()
    values["ca"] = ca
    values["updated_at"] = db.now_iso()
    columns = ", ".join(FIELDS)
    placeholders = ", ".join("?" for _ in FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in FIELDS if f != "ca")
    conn = db.connect()
    try:
        conn.execute(
            f"INSERT INTO lexicon ({columns}) VALUES ({placeholders})"
            f" ON CONFLICT(ca) DO UPDATE SET {updates}",
            tuple(values.get(f) for f in FIELDS),
        )
        row = conn.execute("SELECT * FROM lexicon WHERE ca = ?", (ca,)).fetchone()
    finally:
        conn.close()
    if row is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="upsert failed")
    return _decorate([dict(row)])[0]
