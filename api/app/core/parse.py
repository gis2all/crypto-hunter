"""Reading the same column shape the same way, in every adapter.

Sources hand over three shapes over and over: a number in a text field, an epoch stamp in
whichever unit the source prefers, and a link column that is sometimes a handle and sometimes a
URL. Each adapter used to carry its own reader, and the readers had drifted. The drift was
load-bearing, so what is shared here is grouped by meaning rather than by the name it used to
carry:

- positive_float - the bulk registries write a missing column as 0 instead of omitting it, so 0
  has to read as "no answer". Tolly is the exception: its 24h volume uses zero_ok.
- as_float - 0 is a reading. A pair that did not move is an answer (6.6.2).

Pure: no IO, no clock, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal


def as_float(value: Any) -> float | None:
    """The number as written, or None when it is not a number. Zero and negatives survive."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def positive_float(value: Any, *, zero_ok: bool = False) -> float | None:
    """A number that counts only above zero: a market cap of nothing and a pool with no
    reserves are the same as no reading at all.

    zero_ok=True is for the one column where 0 is an answer - Tolly's 24h volume, where a token
    nobody traded has to be rejectable by the volume floor instead of filing under unknown and
    sliding through the filter. A zero market cap or price carries no such meaning, so those
    still collapse to None.
    """
    number = as_float(value)
    if number is None:
        return None
    if number > 0:
        return number
    return 0.0 if zero_ok and number == 0 else None


def utc_from_unix(
    value: Any, *, unit: Literal["s", "ms"] = "s", require_positive: bool = False
) -> datetime | None:
    """An epoch stamp as an aware UTC datetime, or None when it will not read as one.

    Sources disagree about the unit (unit="ms" divides by 1000) and about whether 0 is a stamp
    at all. require_positive=True makes it a gap, so that a missing timestamp cannot become a
    timestamp of the epoch while it is read as a launch date.
    """
    seconds = as_float(value)
    if seconds is None:
        return None
    if unit == "ms":
        seconds = seconds / 1000.0
    if require_positive and seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def utc_from_iso_text(text: Any) -> datetime | None:
    """An ISO-8601 stamp as an aware UTC datetime, or None when it will not parse."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
