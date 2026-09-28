"""The lexicon: one row per token this round judged, and the words that go on its card.

**Why it is a table and not a cache.** The entry is 45% of a token's score as well as the
prose on its card, so the scoring stage reads it back out of here. `run_id` is what keeps
that honest: a row belongs to exactly one round and `forget_other_rounds` deletes the rest,
so the numbers on a card and the words beside them were decided together.

**Where the words come from.** `report/research.py` writes them: it finds the token's pages,
reads them, and asks the model once. Nothing here invents a judgement - a token whose research
did not land has no entry, and a token with no entry has no score and is not selected.
"""

from __future__ import annotations

from pathlib import Path

from ..store import db
from .schema import LexiconDraft, validate_lexicon  # noqa: F401  (re-exported for callers)

PROMPT_DIR = Path(__file__).with_name("prompts")


def read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def entry_payload(entry, symbol: str | None = None) -> dict:
    """The model's own fields, plus the ticker it was never asked to invent.

    The symbol comes off the round's own metrics, not out of the model: a lexicon row keyed
    by nothing but its CA is a row nobody can read. It stays None when the source never gave
    one, which the page renders as a dash.
    """
    payload = entry.model_dump()
    payload["symbol"] = symbol or None
    return payload


def store_entries(chain_id: str, run_id: str, entries: dict[str, dict],
                  observed_at: str | None = None) -> set[str]:
    """Write the round's entries - five scores and the prose beside them - and return the
    CAs that went in.

    The row carries the round that produced it, and `forget_other_rounds` deletes every other
    round's rows, so an entry is true for exactly one round. That is why there is no
    `confirmed` guard: a judgement made against last round's facts is not evidence about
    this one, and the numbers it was attached to are gone.
    """
    now = observed_at or db.now_iso()
    written: set[str] = set()
    conn = db.connect()
    try:
        for ca, entry in entries.items():
            conn.execute(
                "INSERT INTO lexicon (ca, chain_id, run_id, symbol, tag,"
                " f_completeness, f_substance, f_team, f_narrative, f_community,"
                " social, concl, narr_text, prod_text, substance_text, team_text,"
                " updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(ca) DO UPDATE SET chain_id=excluded.chain_id,"
                " run_id=excluded.run_id, symbol=COALESCE(excluded.symbol, lexicon.symbol),"
                " tag=excluded.tag,"
                " f_completeness=excluded.f_completeness,"
                " f_substance=excluded.f_substance, f_team=excluded.f_team,"
                " f_narrative=excluded.f_narrative, f_community=excluded.f_community,"
                " social=excluded.social, concl=excluded.concl,"
                " narr_text=excluded.narr_text, prod_text=excluded.prod_text,"
                " substance_text=excluded.substance_text, team_text=excluded.team_text,"
                " updated_at=excluded.updated_at",
                (ca, chain_id, run_id, entry.get("symbol"), entry.get("tag"),
                 entry.get("f_completeness"), entry.get("f_substance"), entry.get("f_team"),
                 entry.get("f_narrative"), entry.get("f_community"),
                 entry.get("social"), entry.get("concl"), entry.get("narr_text"),
                 entry.get("prod_text"), entry.get("substance_text"),
                 entry.get("team_text"), now),
            )
            written.add(ca)
    finally:
        conn.close()
    return written


def forget_other_rounds(run_id: str) -> int:
    """Delete every entry that does not belong to this round, and say how many went.

    The lexicon holds one round's worth of rows - the ones this round judged - and this is
    what keeps that true. Without it a token judged last round and not this one stays
    readable, and the next round reads a dead round's opinion while claiming it asked.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ca FROM lexicon WHERE COALESCE(run_id, '') <> ?", (run_id,),
        ).fetchall()
        doomed = [row["ca"] for row in rows]
        if not doomed:
            return 0
        marks = ",".join("?" for _ in doomed)
        conn.execute("DELETE FROM lexicon_sources WHERE ca IN (%s)" % marks, doomed)
        conn.execute("DELETE FROM lexicon WHERE ca IN (%s)" % marks, doomed)
        return len(doomed)
    finally:
        conn.close()


#: The card's 链下调研 fold: block title -> the entry field that fills it. This is the one place
#: the card and the lexicon agree on what a block is called, and the names are the five the
#: settings page and the `f_*` scores use, so a reader who opens a block is reading the reason
#: for the score above it.
BLOCKS = (("产品完成度", "prod_text"),
          ("项目实质与亮点", "substance_text"),
          ("团队与 dev 可信度", "team_text"),
          ("叙事与官方关系", "narr_text"),
          ("社区活跃度", "social"))

#: The block an official endorsement is reported under. The badge is display-only - it never
#: reaches the score - so it travels beside the block that argues the relationship rather
#: than as a field of its own on the card.
BADGE_TITLE = "叙事与官方关系"


def report_payload(entry: dict | None, deltas: list[dict]) -> dict:
    """The words on one card. Everything numeric comes from the snapshot, not from here.

    `rows` and `safety` are deliberately left out: the card derives them from the round's
    own facts, and recording a second copy of a number is how the two copies start to
    disagree.
    """
    entry = entry or {}
    sections = {title: entry.get(field) for title, field in BLOCKS if entry.get(field)}
    badge = str(entry.get("badge") or "").strip()
    return {
        "tag": entry.get("tag") or "",
        "verdict": entry.get("concl") or "",
        "deltas": deltas,
        "sections": sections,
        "badges": {BADGE_TITLE: badge} if badge else {},
    }
