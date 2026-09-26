"""The lexicon draft: one call per batch, and a bad batch costs only its own entries.

A draft is written before the round's picks are scored, so a call that dies here is the
difference between "these tokens have no product score" and a round that never lands.
"""

import pytest

from app.core.models import SafetyFacts, TokenMetrics
from app.report import draft
from app.store import db

CA = "0x%040x"


def _gap(index: int) -> dict:
    ca = CA % index
    return {
        "symbol": "T%d" % index,
        "ca": ca,
        "metrics": TokenMetrics.model_validate({"ca": ca}),
        "facts": SafetyFacts.model_validate({}),
    }


def _entry(ca: str) -> dict:
    return {
        "tag": "meme", "narrative": 2, "product": 2, "concl": "x" * 200,
        "narr_text": "a", "prod_text": "b", "mech_text": "c", "pros": "d", "cons": "e",
        "social": "",
    }


def _asked_cas(message: str) -> list[str]:
    """The CAs the batch actually asked about, as the context lists them."""
    return sorted({word for word in message.split() if word.startswith("0x") and len(word) == 42})


@pytest.fixture(autouse=True)
def no_batch_override(monkeypatch):
    monkeypatch.delenv("CH_LLM_DRAFT_BATCH", raising=False)


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    monkeypatch.setattr(draft.llm, "configured", lambda: True)


def _answering_model(monkeypatch, seen: list, fail_on: set[int] | None = None):
    def fake(messages, **_kw):
        cas = _asked_cas(messages[1]["content"])
        seen.append(len(cas))
        if fail_on and len(seen) in fail_on:
            raise draft.llm.LLMUnavailable("500 upstream")
        return {"entries": {ca: _entry(ca) for ca in cas}}

    monkeypatch.setattr(draft.llm, "complete_json", fake)


def test_one_call_per_batch_so_no_answer_runs_out_of_ceiling(monkeypatch, isolated_db):
    gaps = [_gap(i) for i in range(12)]
    seen: list[int] = []
    _answering_model(monkeypatch, seen)

    entries = draft.draft_lexicon({"id": "arc"}, gaps, "run1")

    assert seen == [5, 5, 2]
    assert len(entries) == 12


def test_the_batch_size_is_a_knob(monkeypatch, isolated_db):
    monkeypatch.setenv("CH_LLM_DRAFT_BATCH", "3")
    gaps = [_gap(i) for i in range(7)]
    seen: list[int] = []
    _answering_model(monkeypatch, seen)

    entries = draft.draft_lexicon({"id": "arc"}, gaps, "run1")

    assert seen == [3, 3, 1]
    assert len(entries) == 7


def test_a_broken_batch_costs_only_its_own_entries(monkeypatch, isolated_db):
    gaps = [_gap(i) for i in range(11)]
    seen: list[int] = []
    _answering_model(monkeypatch, seen, fail_on={2})
    notes: list[str] = []

    entries = draft.draft_lexicon({"id": "arc"}, gaps, "run1", notes=notes)

    assert seen == [5, 5, 1]
    assert len(entries) == 6, "the two good batches still land"
    assert notes and "500 upstream" in notes[0], "and the round carries the reason"


def test_a_model_that_answers_nothing_is_still_a_loud_failure(monkeypatch, isolated_db):
    gaps = [_gap(i) for i in range(6)]
    seen: list[int] = []
    _answering_model(monkeypatch, seen, fail_on={1, 2})
    notes: list[str] = []

    with pytest.raises(draft.llm.LLMUnavailable):
        draft.draft_lexicon({"id": "arc"}, gaps, "run1", notes=notes)

    assert notes == [], "the caller's own handler writes the reason, not this path"


def test_a_model_that_is_not_configured_is_not_a_failure(monkeypatch, isolated_db):
    monkeypatch.setattr(draft.llm, "configured", lambda: False)

    assert draft.draft_lexicon({"id": "arc"}, [_gap(0)], "run1") == {}


# --------------------------------------------------------------- 词条库有进有出


def _entry_row(conn, ca: str, state: str = "pending", chain: str = "arc") -> None:
    conn.execute(
        "INSERT INTO lexicon (ca, chain_id, symbol, narrative, product, review_state, updated_at)"
        " VALUES (?, ?, 'T', 2, 2, ?, '2026-09-24T20:00:00+08:00')",
        (ca, chain, state),
    )


def _selected_run(conn, run_id: str, ca: str) -> None:
    """A round that put this token on a report. `run_items` is the record the prune reads."""
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, status, selected, started_at)"
        " VALUES (?, ?, 'arc', 'manual', 'sent', 1, '2026-09-24T20:00:00+08:00')",
        (run_id, run_id),
    )
    conn.execute(
        "INSERT INTO run_items (run_id, ca, decision) VALUES (?, ?, 'selected')",
        (run_id, ca),
    )


def test_prune_keeps_what_earned_a_place(isolated_db):
    """入选过的、人工确认的、本轮还在判定的都留着，其余删掉。"""
    conn = db.connect()
    try:
        for index in range(4):
            _entry_row(conn, CA % index)
        _selected_run(conn, "r1", CA % 1)
        conn.execute("UPDATE lexicon SET review_state = 'confirmed' WHERE ca = ?", (CA % 2,))
        conn.execute(
            "INSERT INTO lexicon_sources (ca, chain_id, kind, url, fetched_at)"
            " VALUES (?, 'arc', 'website', 'https://x.example', '2026-09-24T20:00:00+08:00')",
            (CA % 3,),
        )
    finally:
        conn.close()

    assert draft.prune_lexicon("arc", keep=[CA % 3]) == 1

    conn = db.connect()
    try:
        left = {row["ca"] for row in conn.execute("SELECT ca FROM lexicon").fetchall()}
        sources = conn.execute("SELECT count(*) AS n FROM lexicon_sources").fetchone()["n"]
    finally:
        conn.close()
    assert left == {CA % 1, CA % 2, CA % 3}
    assert sources == 1


def test_prune_takes_the_evidence_links_with_it(isolated_db):
    """A link outliving its entry would be a citation to a dossier nobody has."""
    conn = db.connect()
    try:
        _entry_row(conn, CA % 9)
        conn.execute(
            "INSERT INTO lexicon_sources (ca, chain_id, kind, url, fetched_at)"
            " VALUES (?, 'arc', 'x', 'https://x.example', '2026-09-24T20:00:00+08:00')",
            (CA % 9,),
        )
    finally:
        conn.close()

    assert draft.prune_lexicon("arc") == 1

    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM lexicon_sources").fetchone()["n"] == 0
    finally:
        conn.close()


def test_prune_leaves_the_other_chain_alone(isolated_db):
    conn = db.connect()
    try:
        _entry_row(conn, CA % 7, chain="bsc")
    finally:
        conn.close()

    assert draft.prune_lexicon("arc") == 0

    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM lexicon").fetchone()["n"] == 1
    finally:
        conn.close()


def test_prune_on_a_clean_store_is_a_no_op(isolated_db):
    assert draft.prune_lexicon("arc") == 0


def _item(metrics: dict) -> object:
    return type("Item", (), {"metrics": TokenMetrics.model_validate(metrics)})()


def test_the_social_block_appends_the_links_the_sentence_left_out():
    """The writer reads these pages, so its sentence is the interesting part - but a sentence
    that names X and forgets the Telegram leaves the reader with no way to reach it, and the
    round already resolved that URL."""
    ca = CA % 1
    item = _item({"ca": ca, "socials": {"twitter": "https://x.com/t",
                  "telegram": "https://t.me/g"},
                  "websites": ["https://t.xyz"]})
    entry = {"social": "X https://x.com/t · 916 粉丝"}

    body = draft._social_block(item, entry)

    assert body.startswith("X https://x.com/t · 916 粉丝")
    assert "TG https://t.me/g" in body and "官网 https://t.xyz" in body
    assert body.count("https://x.com/t") == 1, "the link it did mention is not repeated"


def test_a_social_block_with_nothing_to_add_is_left_alone():
    ca = CA % 2
    item = _item({"ca": ca, "socials": {"twitter": "https://x.com/t"}})
    assert draft._social_block(item, {"social": "X https://x.com/t"}) == "X https://x.com/t"
    assert draft._social_block(item, {"social": ""}) == "X https://x.com/t"
