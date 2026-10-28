"""Re-read stored cards and say what each one is - kind and category.

Started as a repair of the rows parked in `其他`; it now also answers the other question the
old pipeline got wrong (2026-10-27, user): a 候补名单 is a 白名单, staking is 挖矿, a 提案 is
治理, and a card filed as an activity that is really a project has to move kind as well as
category. It never creates cards and never rewrites the title or summary.

`其他` is not an answer: a card the model cannot place comes back as `not_opportunity` so the
caller can retire it, and an unreadable answer leaves the row alone. Re-running is safe.
"""

from __future__ import annotations

import json

from ..llm import client as llm
from ..store import db
from . import model, schema

RECATEGORIZE_VERSION = "recategorize.md@3"
#: One call carries every row; the model answers slug -> category for each. 400 rows of title +
#: truncated summary is still a small prompt, and splitting it would cost more than it saves.
BATCH = 400


def run(limit: int = BATCH, tell=None, every: bool = False) -> dict:
    """Ask once, write the valid answers, leave the rest alone.

    `every=True` re-reads every active card instead of only the ones parked in 其他; that is
    the one-off pass that fixed kind and category for the whole shelf.
    """
    rows = db.opportunity_items(limit=limit)
    if every:
        rows = [row for row in rows if row.get("user_state") == "active"]
    else:
        rows = [row for row in rows if str(row.get("category") or "") == "其他"]
    if not rows:
        return {"checked": 0, "updated": 0, "left": 0, "unknown": []}
    if tell:
        tell("重新归类 %d 张「其他」卡…" % len(rows))
    payload = [{"slug": row["slug"], "kind": row["kind"], "title": row.get("title") or "",
                "summary": str(row.get("summary") or "")[:300]} for row in rows]
    messages = [
        {"role": "system", "content": model.read_prompt("recategorize.md")},
        {"role": "user", "content": json.dumps({"cards": payload}, ensure_ascii=False)},
    ]
    raw = llm.complete_json(messages, purpose="opportunity_recategorize",
                            prompt_version=RECATEGORIZE_VERSION)
    by_slug = {row["slug"]: row for row in rows}
    unknown: list[str] = []
    not_opportunity: list[str] = []
    updated = 0
    for item in (raw or {}).get("items") or []:
        slug = str((item or {}).get("slug") or "")
        category = str((item or {}).get("category") or "").strip()
        kind = str((item or {}).get("kind") or "").strip()
        row = by_slug.pop(slug, None)
        if row is None:
            unknown.append(slug)
            continue
        if not category:
            not_opportunity.append(slug)
            continue
        if not kind:
            kind = ("activity" if category in schema.ACTIVITY_CATEGORIES else "project")
        allowed = (schema.ACTIVITY_CATEGORIES if kind == "activity"
                   else schema.PROJECT_CATEGORIES)
        if category not in allowed or category == "其他":
            continue
        if db.opportunity_set_category(slug, category, kind):
            updated += 1
    return {"checked": len(rows), "updated": updated,
            "left": len(rows) - updated - len(not_opportunity), "unknown": unknown,
            "not_opportunity": not_opportunity}
