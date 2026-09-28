"""The writing contract: catch a bad draft before it reaches the database."""

import logging

from app.report.schema import CAPS, NewTokenEntry, validate_lexicon

GOOD_TEXT = "x" * 200


def _entry(**kw):
    base = {
        "tag": "meme", "f_completeness": 3, "f_substance": 2, "f_team": 2,
        "f_narrative": 2, "f_community": 2, "concl": "short", "narr_text": "a",
        "prod_text": "b", "substance_text": "c", "team_text": "d", "social": "e",
    }
    base.update(kw)
    return base


def _draft(entries=None):
    return {"entries": entries or {"0xabc": _entry(concl=GOOD_TEXT)}}


def test_clean_draft_has_no_issues():
    assert validate_lexicon(_draft(), ["0xabc"]) == []


def test_case_insensitive_token_lookup():
    assert validate_lexicon(_draft(), ["0xABC"]) == []


def test_missing_entry_is_flagged():
    issues = validate_lexicon(_draft(), ["0xabc", "0xdef"])
    assert any("0xdef" in i for i in issues)


def test_short_concl_is_flagged():
    """40 characters is a heading, not the card's verdict. The prompt asks for 200-500 and
    the floor sits at 160 so a real answer that ran short is not thrown away with the batch."""
    draft = _draft({"0xabc": _entry(concl="x" * 40)})
    assert any("concl too short" in i for i in validate_lexicon(draft, ["0xabc"]))


def test_an_entry_without_a_tag_is_flagged():
    draft = _draft({"0xabc": _entry(tag="", concl=GOOD_TEXT)})
    assert any("tag missing" in i for i in validate_lexicon(draft, ["0xabc"]))


def test_bad_shape_is_reported_not_crashed():
    issues = validate_lexicon({"entries": "nope"}, ["0xabc"])
    assert issues and issues[0].startswith("schema:")


def test_an_entry_needs_the_five_judgements():
    """The off-chain block is 45% of the score and reads all five, so an entry missing one
    cannot be scored: that is a schema failure, not a silent 0."""
    draft = _draft({"0xabc": _entry(f_team=None, f_community=None, concl=GOOD_TEXT)})
    issues = validate_lexicon(draft, ["0xabc"])
    assert any("f_team" in i for i in issues)
    assert any("f_community" in i for i in issues)


def test_every_schema_error_is_reported_not_only_the_first():
    draft = _draft({"0xabc": _entry(f_team=None, f_community=None)})
    issues = validate_lexicon(draft, ["0xabc"])
    assert len([i for i in issues if i.startswith("schema:")]) >= 2


def test_a_long_field_is_logged_and_never_rejected(caplog):
    """Over-length is a writing problem. One 240-character block must not cost the other
    entries in the same batch, so the cap is a log line and not an issue."""
    draft = _draft({"0xabc": _entry(concl=GOOD_TEXT, prod_text="y" * 240)})
    with caplog.at_level(logging.WARNING, logger="app.report.schema"):
        issues = validate_lexicon(draft, ["0xabc"])
    assert issues == [], "a cap is not a rejection"
    assert "prod_text over cap" in caplog.text
    assert "concl over cap" not in caplog.text, "200 characters is inside the cap"


def test_the_caps_are_the_card_and_nothing_else():
    """`CAPS` is what the card prints: the verdict plus one field per 链下调研 block. A
    prose field added to the entry without a block on the card would run unbounded, and a
    block added without a field would be a heading with nothing under it."""
    from app.report.lexicon import BLOCKS

    judged = {name for name in NewTokenEntry.model_fields
              if name != "tag" and not name.startswith("f_")}
    assert set(CAPS) == judged, "every judgement's prose is capped exactly once"
    assert set(CAPS) - {"badge"} == {"concl"} | {field for _title, field in BLOCKS}
