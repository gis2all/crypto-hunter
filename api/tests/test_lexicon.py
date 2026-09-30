"""One round's entries, and what the reading pass makes of them.

The entry is 45% of the score *and* the words on the card. Nothing is carried over from an
earlier round: every entry names the round that wrote it, and the round before is deleted, so
the prose and the score are always the same dossier. The reading pass runs inside the round,
before the score, and writes that round's entry from evidence - there is no second pass and
no card that gets rewritten once it is published.
"""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from app.report import lexicon, research
from app.search import identify as identify_mod
from app.store import db

CA = "0x" + "a" * 40
CA2 = "0x" + "b" * 40
CA3 = "0x" + "c" * 40
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _payload(**over) -> dict:
    base = {"tag": "meme", "f_completeness": 2, "f_substance": 2, "f_team": 2,
            "f_narrative": 2, "f_community": 2, "concl": "x" * 200,
            "narr_text": "a", "prod_text": "b", "substance_text": "c", "team_text": "d",
            "social": ""}
    base.update(over)
    return base


def _row(ca: str) -> dict:
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM lexicon WHERE ca = ?", (ca,)).fetchone())
    finally:
        conn.close()


def _targets(cas) -> list[dict]:
    return [{"ca": ca, "symbol": "T"} for ca in cas]


def _run(run_id: str, status: str = "researched") -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO runs (id, run_key, chain_id, trigger, status, scanned,"
            " dropped, candidates, selected) VALUES (?, ?, 'arc', 'manual', ?, 0, 0, 0, 0)",
            (run_id, run_id, status),
        )
    finally:
        conn.close()


def _snapshot(ca: str, socials: dict | None = None, websites: list | None = None,
              run_id: str = "run1") -> None:
    """The newest facts on file for a token, which is where the reading pass reads its links."""
    _run(run_id)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES (?, ?, ?, '{}', ?, ?)",
            (run_id, ca,
             json.dumps({"ca": ca, "symbol": "T", "market_cap_usd": 100_000.0}),
             json.dumps({"socials": socials or {}, "websites": websites or []}),
             NOW.isoformat()),
        )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    """Every test here is about the reading, not about whether a key is configured."""
    monkeypatch.setattr(research.llm, "configured", lambda: True)


@pytest.fixture(autouse=True)
def offline_search(monkeypatch):
    """No test reaches the search backend unless it says so: the ones that do not care about
    the search get the empty answer rather than a subprocess."""
    monkeypatch.setattr(research, "search_links", lambda *a, **kw: None)


def _model_answering(monkeypatch, seen: dict, payload: dict | None = None):
    def fake(messages, **kw):
        seen["purpose"] = kw.get("purpose")
        seen["prompt"] = messages[0]["content"]
        seen["pages"] = messages[1]["content"]
        return payload or {"entries": {CA: _payload(tag="读过官网")}}

    monkeypatch.setattr(research.llm, "complete_json", fake)


def _page(monkeypatch, body: str = "real copy " * 30):
    monkeypatch.setattr(research.http, "get",
                        lambda url, headers=None, **kw: "<html><body>%s</body></html>" % body)


def _instant_gather(monkeypatch):
    monkeypatch.setattr(
        research, "gather",
        lambda row: {"ca": row["ca"], "links": [], "metrics": {}, "safety": {}, "error": ""},
    )


# ------------------------------------------------------------------ the store


def test_an_entry_carries_the_round_that_wrote_it(isolated_db):
    lexicon.store_entries("arc", "run1", {CA: _payload()}, observed_at=NOW.isoformat())
    row = _row(CA)
    assert row["run_id"] == "run1"
    assert row["f_completeness"] == 2 and row["f_community"] == 2


def test_the_previous_rounds_entries_are_dropped(isolated_db):
    """One round's entries, and only one: a token this round did not judge must not stay
    readable, or the next round reads a dead round's opinion while claiming it asked."""
    lexicon.store_entries("arc", "run1", {CA: _payload()})
    other = "0x" + "b" * 40
    lexicon.store_entries("arc", "run2", {other: _payload()})
    assert lexicon.forget_other_rounds("arc", "default", "run2") == 1
    conn = db.connect()
    try:
        left = [row["ca"] for row in conn.execute("SELECT ca FROM lexicon")]
    finally:
        conn.close()
    assert left == [other]


def test_forgetting_takes_the_evidence_links_with_it(isolated_db):
    """A link outliving its entry would be a citation to a dossier nobody has."""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO lexicon (ca, chain_id, run_id, symbol, updated_at)"
            " VALUES (?, 'arc', 'old', 'T', '2026-09-24T20:00:00+08:00')",
            (CA,),
        )
        conn.execute(
            "INSERT INTO lexicon_sources (ca, chain_id, kind, url, fetched_at)"
            " VALUES (?, 'arc', 'website', 'https://x.example', '2026-09-24T20:00:00+08:00')",
            (CA,),
        )
    finally:
        conn.close()

    assert lexicon.forget_other_rounds("arc", "default", "new") == 1

    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM lexicon_sources").fetchone()["n"] == 0
    finally:
        conn.close()


def test_forgetting_a_store_that_is_already_one_round_is_a_no_op(isolated_db):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO lexicon (ca, chain_id, run_id, symbol, updated_at)"
            " VALUES (?, 'arc', 'r1', 'T', '2026-09-24T20:00:00+08:00')",
            (CA,),
        )
    finally:
        conn.close()

    assert lexicon.forget_other_rounds("arc", "default", "r1") == 0


def test_an_old_lexicon_loses_the_research_columns_and_their_index(tmp_path):
    """The research pass is gone, so a database from the two-pass build must lose its queue
    columns. The index has to go first: SQLite refuses to drop a column an index still names,
    and that refusal is caught, so without the drop the column would survive on exactly the
    one database that had the index."""
    path = tmp_path / "old.db"
    db.init_db(db.connect(path))

    conn = db.connect(path)
    conn.execute("ALTER TABLE lexicon ADD COLUMN research_state TEXT NOT NULL DEFAULT 'none'")
    conn.execute("ALTER TABLE lexicon ADD COLUMN research_note TEXT")
    conn.execute("CREATE INDEX idx_lexicon_state ON lexicon(research_state)")
    conn.close()

    db.init_db(db.connect(path))

    conn = db.connect(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(lexicon)")}
        indexes = {row["name"] for row in
                   conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        conn.close()
    assert "research_state" not in columns
    assert "research_note" not in columns
    assert "idx_lexicon_state" not in indexes


def test_the_report_payload_carries_the_five_blocks_in_card_order():
    payload = lexicon.report_payload(
        {"tag": "基建", "concl": "x" * 200, "prod_text": "p", "substance_text": "s",
         "team_text": "t", "narr_text": "n", "social": "c"},
        [{"key": "市值", "value": "+0%"}],
    )
    assert list(payload["sections"]) == ["产品完成度", "项目实质与亮点", "团队与 dev 可信度",
                                        "叙事与官方关系", "社区活跃度"]
    assert payload["verdict"] == "x" * 200
    assert payload["deltas"] == [{"key": "市值", "value": "+0%"}]
    assert payload["badges"] == {}, "no badge, no badge map"


def test_a_badge_travels_beside_the_narrative_block():
    payload = lexicon.report_payload({"concl": "x", "badge": "官方背书"}, [])
    assert payload["badges"] == {"叙事与官方关系": "官方背书"}


# ------------------------------------------------------------------ the reading pass


def test_the_pass_reads_the_pages_and_writes_the_entry(isolated_db, monkeypatch):
    _snapshot(CA, socials={"twitter": "https://x.com/t"}, websites=["https://t.xyz"])
    seen: dict = {}
    _page(monkeypatch)
    _model_answering(monkeypatch, seen)

    out = research.research_entries({"id": "arc"}, _targets([CA]), "run1")

    assert seen["purpose"] == "research"
    assert "https://t.xyz" in seen["pages"], "the model is handed what was read"
    assert out[CA]["tag"] == "读过官网"
    row = _row(CA)
    assert row["run_id"] == "run1"
    assert row["tag"] == "读过官网"
    urls = sorted(source["url"] for source in research.sources_for(CA))
    assert urls == ["https://t.xyz", "https://x.com/t"]


def test_a_token_with_no_readable_page_is_absent_and_the_note_says_so(isolated_db):
    notes: list[str] = []

    out = research.research_entries({"id": "arc"}, _targets([CA]), "run1", notes=notes)

    assert out == {}, "a token nobody could read has no entry"
    assert notes and "链下调研未完成" in notes[0]
    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM lexicon").fetchone()["n"] == 0
    finally:
        conn.close()


def test_a_page_that_answers_with_nothing_is_not_evidence(isolated_db, monkeypatch):
    _snapshot(CA, socials={"twitter": "https://x.com/t"})
    _page(monkeypatch, body="")

    out = research.research_entries({"id": "arc"}, _targets([CA]), "run1")

    assert out == {}


def test_an_answer_that_fails_the_contract_writes_no_entry(isolated_db, monkeypatch):
    _snapshot(CA, websites=["https://t.xyz"])
    _page(monkeypatch)
    monkeypatch.setattr(research.llm, "complete_json", lambda messages, **kw: {"entries": {}})
    notes: list[str] = []

    out = research.research_entries({"id": "arc"}, _targets([CA]), "run1", notes=notes)

    assert out == {}, "an entry that never validates is an entry that never scores"
    assert notes and "链下调研未完成" in notes[0]


def test_a_round_without_a_model_researches_nothing(isolated_db, monkeypatch):
    monkeypatch.setattr(research.llm, "configured", lambda: False)

    assert research.research_entries({"id": "arc"}, _targets([CA]), "run1") == {}


def test_a_token_links_are_read_at_the_same_time(isolated_db, monkeypatch):
    """One card's links are four different hosts. Read one after another and the card waits for
    four page loads; read together and it waits for the slowest one. The order still holds -
    the website first - because the prompt and the stored evidence both read it that way."""
    _snapshot(CA, socials={"twitter": "https://x.com/t", "telegram": "https://t.me/t",
                           "github": "https://g.com/t"}, websites=["https://t.xyz"])
    live = _counting_pages(monkeypatch)
    _page(monkeypatch)  # the pages are fake, the reads are real
    seen: dict = {}
    _model_answering(monkeypatch, seen)

    assert research.research_entries({"id": "arc"}, _targets([CA]), "run1")[CA]["tag"] == \
        "读过官网"
    assert live["peak"] == 4, "four links, four reads in flight"
    assert seen["pages"].index("### website https://t.xyz") < seen["pages"].index(
        "### twitter https://x.com/t"), "the order the list was built in is the order read"


def _omitting(*drop: str) -> dict:
    """A draft with a field left out, which is the shape every rejection has had here."""
    payload = _payload()
    for key in drop:
        payload.pop(key, None)
    return {"entries": {CA: payload}}


def test_a_draft_the_validator_rejects_is_asked_once_more(isolated_db, monkeypatch):
    """A missing judgement is a field the model forgot to write down, not a token without a
    card. The rejected answer goes back with the finding that rejected it, and the token keeps
    the entry the second call wrote."""
    _snapshot(CA, websites=["https://t.xyz"])
    _page(monkeypatch)
    asks: list = []

    def fake(messages, **kw):
        asks.append((kw.get("purpose"), messages[-1]["content"]))
        if len(asks) == 1:
            return _omitting("f_community")
        return {"entries": {CA: _payload(tag="第二次")}}

    monkeypatch.setattr(research.llm, "complete_json", fake)

    out = research.research_entries({"id": "arc"}, _targets([CA]), "run1")

    assert out[CA]["tag"] == "第二次"
    assert [purpose for purpose, _ in asks] == ["research", "research_repair"], \
        "one more call, not a loop"
    assert "f_community" in asks[1][1], "the finding that rejected it is what it is told"


def test_a_draft_that_validates_is_asked_once(isolated_db, monkeypatch):
    """Only a rejected answer pays for the second call: the repair is not a second opinion on
    every card."""
    _snapshot(CA, websites=["https://t.xyz"])
    _page(monkeypatch)
    asks: list = []

    def fake(messages, **kw):
        asks.append(kw.get("purpose"))
        return {"entries": {CA: _payload()}}

    monkeypatch.setattr(research.llm, "complete_json", fake)

    assert research.research_entries({"id": "arc"}, _targets([CA]), "run1")[CA]["tag"] == "meme"
    assert asks == ["research"]

def test_both_model_calls_carry_the_round_they_belong_to(isolated_db, monkeypatch):
    """`llm_calls` is what answers "what did this round spend on the model", so every call
    has to say which round it served - the repair included. Without it the only way back to a
    round own model time is guessing at timestamps."""
    _snapshot(CA, websites=["https://t.xyz"])
    _page(monkeypatch)
    seen: list = []

    def fake(messages, **kw):
        seen.append(kw)
        if len(seen) == 1:
            return _omitting("f_community")
        return {"entries": {CA: _payload()}}

    monkeypatch.setattr(research.llm, "complete_json", fake)

    research.research_entries({"id": "arc"}, _targets([CA]), "run1")

    assert [kw["run_id"] for kw in seen] == ["run1", "run1"]

def test_a_pass_researches_its_cards_at_the_same_time(isolated_db, monkeypatch):
    """The cards are independent of each other, so a pass should cost the slowest one rather
    than the sum of them. The model calls are the shared resource; this counts them in flight."""
    _instant_gather(monkeypatch)
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def fake_one(row, gathered=None):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with guard:
            live["now"] -= 1
        return _payload(tag=str(row["ca"]))

    monkeypatch.setattr(research, "one", fake_one)

    out = research.research_entries({"id": "arc"}, _targets([CA, CA2, CA3]), "run1")

    assert live["peak"] == 3, "three cards, three reads in flight"
    assert sorted(out) == sorted([CA, CA2, CA3])


def test_a_card_that_fails_leaves_its_neighbours_alone(isolated_db, monkeypatch):
    """The per-card boundary has to survive the concurrency: one card without a readable page
    is absent from the answer, and the two beside it are still written."""
    _instant_gather(monkeypatch)

    def fake_one(row, gathered=None):
        if str(row["ca"]) == CA2:
            raise ValueError("页面上什么都没有")
        return _payload(tag=str(row["ca"]))

    monkeypatch.setattr(research, "one", fake_one)
    notes: list[str] = []

    out = research.research_entries({"id": "arc"}, _targets([CA, CA2, CA3]), "run1", notes=notes)

    assert CA2 not in out
    assert CA in out and CA3 in out
    assert notes and "链下调研未完成" in notes[0]


def test_the_progress_callback_counts_every_token(isolated_db, monkeypatch):
    _instant_gather(monkeypatch)
    monkeypatch.setattr(research, "one", lambda row, gathered=None: _payload())
    seen: list[tuple[int, int]] = []

    research.research_entries({"id": "arc"}, _targets([CA, CA2]), "run1",
                              on_done=lambda done, total: seen.append((done, total)))

    assert seen[-1] == (2, 2), "one tick per token, and the last one is the total"


# ------------------------------------------------------------------ links


def test_a_link_an_earlier_round_learned_is_still_a_link(isolated_db):
    """A source that goes quiet this round must not blank a website we have had for days.
    Without this the reading pass reports "no links" on a token whose links are in the table."""
    _run("old1")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES ('old1', ?, '{}', '{}', ?, ?)",
            (CA, json.dumps({"socials": {"twitter": "https://x.com/t"},
                             "websites": ["https://t.xyz"]}), NOW.isoformat()),
        )
    finally:
        conn.close()
    _snapshot(CA)  # newer row, and this time the sources had nothing to say

    metrics, _ = research._latest_metrics(CA)

    assert research.links_for(metrics) == [
        ("website", "https://t.xyz"), ("twitter", "https://x.com/t")]


def test_a_token_the_sources_gave_an_account_is_still_searched(monkeypatch):
    """A handle in the sources does not replace the search. The known links are candidates to
    confirm; the search is what finds the pages the sources never listed, and every token is
    asked about."""
    metrics = {"websites": ["https://a.xyz"], "socials": {"twitter": "https://x.com/a"}}
    asked: list[tuple[str, str]] = []

    monkeypatch.setattr(research, "_latest_metrics", lambda ca: (metrics, {}))

    def fake_search(chain_id, ca, symbol, known, **kw):
        asked.append((chain_id, ca))
        return None

    monkeypatch.setattr(research, "search_links", fake_search)

    out = research.gather({"ca": CA, "chain_id": "arc", "symbol": "A"})

    assert asked == [("arc", CA)], "the search runs for every token, account or not"
    assert out["links"] == [("website", "https://a.xyz"), ("twitter", "https://x.com/a")]

    research.gather({"ca": CA2, "chain_id": "arc", "symbol": "B"})
    assert asked[-1] == ("arc", CA2), "and for one the sources listed nothing for"


def test_a_token_with_an_account_keeps_the_links_it_had_when_searching_is_skipped():
    known = [("website", "https://a.xyz"), ("twitter", "https://x.com/a")]
    found = research.merge_links(None, known)
    assert found == known


# ------------------------------------------------------------------ reading a page


def _counting_pages(monkeypatch, delay: float = 0.05) -> dict:
    """Read the pages for real, and count how many are in flight at once. Each one is slowed
    down enough that a pass reading them one after another shows up as a peak of one."""
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()
    inner = research._read

    def wrapped(kind, url, budget):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            time.sleep(delay)
            return inner(kind, url, budget)
        finally:
            with guard:
                live["now"] -= 1

    monkeypatch.setattr(research, "_read", wrapped)
    return live


#: A launchpad front end as it arrives: a title, an empty root, and the bundle that would
#: have filled it in.
SHELL_PAGE = ('<html><head><title>Memespad</title></head><body><div id="root"></div>'
              '<script>boot()</script></body></html>')


def test_the_card_reads_a_shell_page_with_a_browser_and_an_account_without_one(
        isolated_db, monkeypatch):
    """The same reading the search layer judges candidate sites by, so a page is not quoted
    from one document and judged from another. Accounts are the exception: drawing an X
    profile needs a signed-in browser, and it comes back with less than the plain document."""
    drawn = "<html><body>%s</body></html>" % ("rendered copy " * 30)
    monkeypatch.setattr(research.http, "get", lambda url, headers=None, **kw: SHELL_PAGE)
    monkeypatch.setattr(identify_mod.browser, "render", lambda url: drawn)

    site = research._read("website", "https://memespad.com", 4000)
    account = research._read("twitter", "https://x.com/memes_pad", 4000)

    assert "rendered copy" in site["text"], "the shell was drawn and the drawing was read"
    assert account is None, "a shell that cannot be drawn out of an account is not evidence"
def test_the_prompt_names_the_chain_the_round_is_about(isolated_db, monkeypatch):
    """One prompt file serves every chain, so the name is substituted per call. An Arc
    prompt asking a Robinhood token 「和 Arc 什么关系」 is the radar answering the wrong
    question, and the model can only answer what it was asked (user, 2026-09-30)."""
    _snapshot(CA, websites=["https://t.xyz"])
    _page(monkeypatch)
    seen: dict = {}
    _model_answering(monkeypatch, seen)

    research.research_entries({"id": "robinhood", "name": "Robinhood"}, _targets([CA]),
                              "run1")

    assert "Robinhood" in seen["prompt"]
    assert "Arc" not in seen["prompt"], "the prompt is one file for every chain"
    assert research.CHAIN_TOKEN not in seen["prompt"], "the placeholder is filled in"
