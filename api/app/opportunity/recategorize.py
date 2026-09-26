"""Re-read the cards parked in 其他, once, after the category vocabulary grew.

`其他` was a gap, not a category: the extraction pass had no word for 节点 / 治理 / 交互, so a
node sale, a governance vote and a plain protocol swap all landed there. This module asks the
model one question about the rows already stored - which closed category fits - and writes only
`category`. It never creates cards and never rewrites the title or summary. `其他` is no longer
an answer (2026-10-27, user): a row the model cannot place is reported back as
`not_opportunity` so the caller can retire it, and an unreadable answer leaves it alone.

Deliberately not part of a scan: this is a one-off repair of rows written under the old
vocabulary. Re-running it is safe (it only reads rows still in 其他).
"""

from __future__ import annotations

import json

from ..llm import client as llm
from ..store import db
from . import model, schema

RECATEGORIZE_VERSION = "recategorize.md@2"
#: One call carries every row; the model answers slug -> category for each. 400 rows of title +
#: truncated summary is still a small prompt, and splitting it would cost more than it saves.
BATCH = 400


def run(limit: int = BATCH, tell=None) -> dict:
    """Ask once, write the valid answers, leave the rest alone."""
    rows = [row for row in db.opportunity_items(limit=limit)
            if str(row.get("category") or "") == "其他"]
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
        row = by_slug.pop(slug, None)
        if row is None:
            unknown.append(slug)
            continue
        if not category:
            not_opportunity.append(slug)
            continue
        allowed = (schema.ACTIVITY_CATEGORIES if row["kind"] == "activity"
                   else schema.PROJECT_CATEGORIES)
        if category not in allowed or category == "其他":
            continue
        if db.opportunity_set_category(slug, category):
            updated += 1
    return {"checked": len(rows), "updated": updated,
            "left": len(rows) - updated - len(not_opportunity), "unknown": unknown,
            "not_opportunity": not_opportunity}
