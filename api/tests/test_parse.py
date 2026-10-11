"""One reader per column shape, and the distinction the merge had to keep.

Every adapter used to carry its own copy of the same three readers, and the copies had drifted
where it mattered: `0` is a gap in a bulk registry's market cap - nothing was measured - and an
answer in a pair's 24h volume, where nobody traded. Merging them by name would have moved a
token from one side of the volume floor to the other without touching the floor.
"""

from app.core.parse import as_float, positive_float, utc_from_iso_text, utc_from_unix


def test_a_zero_in_a_market_column_reads_as_no_reading():
    assert positive_float(0) is None
    assert positive_float("0") is None
    assert positive_float(None) is None
    assert positive_float("") is None
    assert positive_float("n/a") is None
    assert positive_float(-3) is None
    assert positive_float("12500.5") == 12500.5


def test_a_zero_is_a_reading_where_the_answer_itself_is_zero():
    assert as_float(0) == 0
    assert as_float("0") == 0
    assert as_float(None) is None
    assert as_float("n/a") is None


def test_a_zero_volume_survives_only_where_the_floor_has_to_see_it():
    assert positive_float(0, zero_ok=True) == 0
    assert positive_float("0", zero_ok=True) == 0
    assert positive_float(0) is None


def test_the_epoch_is_not_a_launch_date():
    assert utc_from_unix(0, require_positive=True) is None
    assert utc_from_unix(None, require_positive=True) is None
    assert utc_from_unix("yesterday", require_positive=True) is None
    assert utc_from_unix(0, unit="ms", require_positive=True) is None


def test_milliseconds_and_seconds_land_on_the_same_instant():
    assert utc_from_unix(1700000000000, unit="ms") == utc_from_unix(1700000000)
    assert utc_from_unix("1700000000000", unit="ms").year == 2023


def test_iso_text_arrives_utc_whatever_the_offset_said():
    assert utc_from_iso_text("2026-09-14T16:56:15.000Z").hour == 16
    assert utc_from_iso_text("2026-09-14T16:56:15+08:00").hour == 8
    assert utc_from_iso_text("not a date") is None
