"""The watchlist: what a scan looks at, and where it comes from.

Two homes, one truth. `seeds/watchlist.yaml` is tracked and is read **once**, on the first
start, into `opportunity_sources`; after that the table is the truth, deletions included. That
is the same split `config/chains/*.example.yaml` and its live copy use, and it is what lets an
operator add an account without editing a file while keeping the factory list reviewable in git.

Every seed row was verified before it went in (`twitter user <handle>` through this project's
own X session, or the project's own site linking the account), and the rows that could not be
verified sit in the seed's `pending:` section instead of here - a guessed handle in the
watchlist is worse than a missing one, because a scan would then report somebody else's posts
as a project's own news.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..store import db

log = logging.getLogger("crypto-hunter.opportunity")

SEED = Path(__file__).with_name("seeds") / "watchlist.yaml"

#: What a watchlist row can be. **X only** (user, 2026-10-18): `official` is one account's
#: timeline, `query` is one X search asked in time slices. The module read pages and GitHub
#: organisations as well until then; those readers are gone and the verified candidate lists sit
#: in the seed's `pending:` section, to be re-enabled if the other sources come back.
#:
#: The kind is also what the trust verdict reads (`trust.py`): an entry that came off an official
#: account's own post is 官方 by construction, and one that a phrase search found is not.
SOURCE_KINDS = ("official", "query")


def seed_rows() -> list[dict]:
    """Every row the seed file carries, in the shape the table takes.

    `pending:` is skipped on purpose: it is the list of things that could **not** be verified,
    and its rows carry a `why` instead of a `kind`. Loading them would put unverified targets
    into a table whose whole promise is that its rows were checked.
    """
    try:
        document = yaml.safe_load(SEED.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("opportunity: the watchlist seed could not be read")
        return []
    rows: list[dict] = []
    for section in ("official", "query"):
        for row in document.get(section) or []:
            target = str(row.get("target") or "").strip()
            kind = str(row.get("kind") or "").strip()
            if not target or kind not in SOURCE_KINDS:
                continue
            rows.append({
                "target": target,
                "kind": kind,
                "ecosystem": str(row.get("ecosystem") or "multi"),
                "cluster": row.get("cluster"),
                "enabled": bool(row.get("enabled", True)),
                # A query row's own window, slice and ceiling; an official row ignores them.
                "window_hours": int(row.get("window_hours") or 24),
                "slice_hours": int(row.get("slice_hours") or 4),
                "max_results": int(row.get("max_results") or 40),
                "note": row.get("note"),
                "added_at": db.now_iso(),
            })
    return rows


def ensure_seeded() -> int:
    """Fill the table from the seed, once. Returns how many rows went in.

    "Once" is the whole point: the check is `is the table empty`, not `is this row missing`.
    A row an operator deleted must not come back on the next restart, or the table would be a
    view of the file rather than the other way round.
    """
    if db.opportunity_sources():
        return 0
    rows = seed_rows()
    if not rows:
        return 0
    written = db.opportunity_source_put(rows)
    log.info("opportunity: watchlist seeded with %d rows", written)
    return written


def rows() -> list[dict]:
    return db.opportunity_sources()


def enabled() -> list[dict]:
    return db.opportunity_sources_enabled()


def queries() -> list[dict]:
    """The rows a scan reads as searches, as opposed to the accounts it follows."""
    return [row for row in enabled() if row["kind"] == "query"]
