"""The fixture source: the industry's shape without the industry's cost.

It is a collector and nothing else - it answers with exactly what a real one will: raw records
carrying a source, a url, a timestamp and text. It carries no kind, no party list, no score and
no summary. A fixture that arrived pre-analysed would let the pipeline skip the two stations
that decide everything downstream (DESIGN.md 5.2), and a pipeline that skipped them is one that
cannot survive its first real source.

What it does carry is relative timestamps (`-2h`), which no real source does. A fixture written
with dates in it stops being "today" the day after it is written, and an empty edition then
looks exactly like a pipeline that never worked - which is the one thing this fixture is for
ruling out.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import htmlbody
from ..models import Discovery

FIXTURE = Path(__file__).resolve().parent.parent / "mock" / "records.json"

#: `-90m` / `+2h` / `-3d`. One number and one unit: the fixture is a fixture, not a parser test.
_OFFSET = re.compile(r"^([+-])(\d+)([mhd])$")

_UNIT = {"m": "minutes", "h": "hours", "d": "days"}


def _stamp(value: str, now: datetime) -> str:
    """An absolute ISO 8601 timestamp, from an absolute value or a relative one."""
    match = _OFFSET.match(value.strip())
    if match is None:
        return value
    sign, amount, unit = match.groups()
    delta = timedelta(**{_UNIT[unit]: int(amount)})
    return (now + delta if sign == "+" else now - delta).isoformat(timespec="seconds")


def read(now: datetime | None = None) -> list[Discovery]:
    """Every record the fixture holds, with its relative times made absolute.

    `now` is a parameter because the fixture's offsets are relative to whatever read it, and a
    test that could not pin the clock would be a test that fails after lunch.
    """
    moment = now or datetime.now(timezone.utc)
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        Discovery(
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            external_id=row["external_id"],
            title=row["title"],
            url=row["url"],
            published_at=_stamp(row["published_at"], moment),
            body=htmlbody.sanitize(row.get("body", "")),
            author=row.get("author", ""),
            media=list(row.get("media", [])),
        )
        for row in rows
    ]
