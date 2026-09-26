"""The writing contract: catch a bad draft before it reaches the database."""

import logging

from app.report.schema import validate_commentary, validate_lexicon

GOOD_TEXT = "x" * 200


def _draft(**kw):
    base = {
        "per_token": {"0xabc": GOOD_TEXT},
        "overall": GOOD_TEXT,
        "new_tokens": {},
    }
    base.update(kw)
    return base


def test_clean_draft_has_no_issues():
    assert validate_commentary(_draft(), ["0xabc"]) == []


def test_case_insensitive_token_lookup():
    assert validate_commentary(_draft(), ["0xABC"]) == []


def test_missing_token_is_flagged():
    issues = validate_commentary(_draft(), ["0xabc", "0xdef"])
    assert any("0xdef" in i for i in issues)


def test_short_text_is_flagged():
    issues = validate_commentary(_draft(per_token={"0xabc": "too short"}), ["0xabc"], min_chars=120)
    assert any("too short" in i for i in issues)


def test_bad_shape_is_reported_not_crashed():
    issues = validate_commentary({"per_token": "nope"}, ["0xabc"])
    assert issues and issues[0].startswith("schema:")


def _entry(**kw):
    base = {
        "tag": "meme", "narrative": 3, "product": 2, "concl": "short", "narr_text": "a",
        "prod_text": "b", "mech_text": "c", "pros": "d", "cons": "e",
    }
    base.update(kw)
    return base


def test_per_token_still_needs_a_paragraph():
    """40 characters is a heading, not a 本期点评 - the prompt asks for 200 and the floor
    sits at 160 so an answer that ran short is not thrown away with the whole batch."""
    issues = validate_commentary(_draft(per_token={"0xabc": "x" * 40}), ["0xabc"])
    assert any("too short" in i for i in issues)


def test_new_token_concl_uses_the_lexicon_floor():
    """The same paragraph is accepted on both writing paths, at the same length."""
    ok = _draft(new_tokens={"0xdef": _entry(concl="x" * 160)})
    assert validate_commentary(ok, ["0xabc"]) == []

    short = _draft(new_tokens={"0xdef": _entry(concl="x" * 159)})
    issues = validate_commentary(short, ["0xabc"])
    assert any("new_tokens.concl" in i for i in issues)


def test_new_token_entry_needs_real_content():
    draft = _draft(new_tokens={"0xdef": _entry()})
    issues = validate_commentary(draft, ["0xabc"])
    assert any("new_tokens.concl" in i for i in issues)


def test_new_token_entry_needs_the_two_judgements():
    """The product block is 45% of the score and reads narrative/product, so an entry
    without them cannot be scored: that is a schema failure, not a silent 0."""
    draft = _draft(new_tokens={"0xdef": _entry(narrative=None, product=None, concl=GOOD_TEXT)})
    issues = validate_commentary(draft, ["0xabc"])
    assert any("narrative" in i for i in issues)
    assert any("product" in i for i in issues)


def test_every_schema_error_is_reported_not_only_the_first():
    draft = _draft(new_tokens={"0xdef": _entry(narrative=None, product=None)})
    issues = validate_commentary(draft, ["0xabc"])
    assert len([i for i in issues if i.startswith("schema:")]) >= 2


def test_a_long_field_is_logged_and_never_rejected(caplog):
    """Over-length is a writing problem. One 240-character 产品完成度 must not cost the
    other entries in the same batch, so the cap is a log line and not an issue."""
    draft = _draft(new_tokens={"0xdef": _entry(concl="x" * 200, prod_text="y" * 240)})
    with caplog.at_level(logging.WARNING, logger="app.report.schema"):
        issues = validate_commentary(draft, ["0xabc"])
    assert issues == [], "a cap is not a rejection"
    assert "prod_text over cap" in caplog.text
    assert "concl over cap" not in caplog.text, "200 characters is inside the cap"


def test_the_lexicon_path_caps_the_same_fields(caplog):
    draft = {"entries": {"0xabc": _entry(concl="x" * 200, mech_text="z" * 260)}}
    with caplog.at_level(logging.WARNING, logger="app.report.schema"):
        assert validate_lexicon(draft, ["0xabc"]) == []
    assert "mech_text over cap" in caplog.text


def test_a_long_review_is_logged_and_kept(caplog):
    draft = _draft(per_token={"0xabc": "x" * 320})
    with caplog.at_level(logging.WARNING, logger="app.report.schema"):
        assert validate_commentary(draft, ["0xabc"]) == []
    assert "per_token over cap" in caplog.text
