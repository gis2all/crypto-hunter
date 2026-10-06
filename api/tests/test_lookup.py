"""CA search: one address, one card, and the row it leaves behind.

A search starts from the address, not from a chain, so the chain is the answer to the first
question rather than the input. And the card it produces is not a round's: there is no pool
to rank in, so it carries no score, and what it writes is a record of the reader looking
something up (`origin = 'search'`, `kept = 0`) rather than a kept card.

What is held to here:

* the chain is read off the feed, deepest liquidity first, before anything else is asked;
* an address nothing knows is refused, not answered with an empty card;
* a chain this app has no config for still gets a card, with its on-chain half unjudged;
* a failed off-chain pass still leaves a card, and the card says which half is missing;
* a search writes one row without touching the entry a round wrote, and collecting it flips
  `kept` instead of writing a second row.
* a record can be dropped - one at a time or the whole list - and a row the reader starred is
  never one of them: that row is a card on the shelf, and the star is the only write that ends
  a collection.
"""

import json

import pytest

from app import lookup
from app.core.models import SafetyFacts, TokenMetrics
from app.report import assemble as report_assemble
from app.report import cards as card_builder
from app.sources import dexscreener
from app.store import db

CA = "0x" + "a" * 40
ARC = "arc"

#: The off-chain half every search in here is given, unless the test is about it failing.
#: The shape `research.one` answers in - the model's own fields, which `lexicon.report_payload`
#: then turns into the card's sections.
WRITTEN = {"tag": "叙事", "concl": "一句话结论", "narr_text": "和本链有关"}


def _pair(ca, chain="arc", liq=1000):
    return {"chainId": chain, "baseToken": {"address": ca}, "liquidity": {"usd": liq}}


def _metrics(ca=CA, symbol="TKN", **over):
    return TokenMetrics(ca=ca, symbol=symbol, market_cap_usd=120000.0,
                        liquidity_usd=30000.0, **over)


def _stub(monkeypatch, *, chain="arc", lp=(True, "已锁", {}), research=WRITTEN,
          site=""):
    """The four things a search pays for, replaced by their answers.

    `identify` is the feed saying which chain the address is on, `read` is every source's
    answer about the token, `research.gather` is finding the links - and with them the one
    site the search proved - and `research.one` is the model pass. Everything between them is
    the code under test. An empty `chain` is a feed that never saw the address; a `None`
    `research` is the off-chain half failing.

    Answers with `(calls, found)`: the kwargs `one` was called with, and the kwargs
    `gather` was, since the links a search starts from are read off the second one now.
    """
    monkeypatch.setattr(dexscreener, "identify",
                        lambda ca: [(chain, 1000.0)] if chain else [])
    monkeypatch.setattr(lookup, "read", lambda cfg, ca: (_metrics(ca), SafetyFacts()))
    if lp is not None:
        monkeypatch.setattr(lookup.decide, "lp_verdict",
                            lambda cfg, metrics, facts, reader=None: lp)

    calls: list[dict] = []
    found: list[dict] = []

    def fake_gather(row, **kwargs):
        found.append({"row": row, **kwargs})
        return {"ca": row["ca"], "links": [], "metrics": {}, "site": site, "safety": {},
                "timeline": [], "talk": [], "official": [], "error": ""}

    def fake_one(row, gathered=None, **kwargs):
        calls.append({"row": row, "gathered": gathered, **kwargs})
        if research is None:
            raise RuntimeError("没有可读的外链（官网 / X / GitHub 都没有）")
        return research

    monkeypatch.setattr(lookup.research, "gather", fake_gather)
    monkeypatch.setattr(lookup.research, "one", fake_one)
    return calls, found


def _card(row):
    return json.loads(row["payload_json"])


def _search(client, monkeypatch, ca=CA):
    """A search through the endpoint, driven to the end in this thread.

    The endpoint answers a job now rather than the card, so a test that is not about
    watching the job runs it inline - and because the job is the same dict the runner
    mutates, what comes back is already the finished reading.
    """
    monkeypatch.setattr(lookup, "submit", lambda job: lookup.drive(job))
    return client.post("/api/v1/lookup", json={"ca": ca})


def test_identify_groups_the_chains_and_puts_the_deepest_first(monkeypatch):
    """The feed answers one row per pool and repeats the chain, so the same address twice on
    one chain is one answer - and the chain it is deepest on is the one a reader means."""
    monkeypatch.setattr(dexscreener, "json_get", lambda *args, **kwargs: {"pairs": [
        _pair(CA, chain="base", liq=100),
        _pair(CA, chain="robinhood", liq=1200),
        _pair(CA, chain="base", liq=200),
        _pair(CA, chain="robinhood", liq=500),
    ]})
    assert dexscreener.identify(CA) == [("robinhood", 1700.0), ("base", 300.0)]


def test_identify_is_empty_when_the_feed_never_saw_the_address(monkeypatch):
    def gone(*args, **kwargs):
        raise dexscreener.SourceError("404 Not Found")

    monkeypatch.setattr(dexscreener, "json_get", gone)
    assert dexscreener.identify(CA) == []


def test_run_refuses_something_that_is_not_an_address():
    with pytest.raises(lookup.Refused) as caught:
        lookup.run("hello world")
    assert "合约地址" in str(caught.value)


def test_run_refuses_an_address_the_feed_never_saw(monkeypatch):
    _stub(monkeypatch, chain="")
    with pytest.raises(lookup.Refused) as caught:
        lookup.run(CA)
    assert "没见过" in str(caught.value)


def test_a_search_writes_a_card_with_no_score_and_a_record_of_looking(isolated_db, monkeypatch):
    calls, _found = _stub(monkeypatch)
    row = lookup.run(CA)

    assert row["origin"] == "search"
    assert row["kept"] == 0
    assert row["config_id"] is None
    assert row["rank"] is None
    assert row["chain_id"] == ARC

    card = _card(row)
    assert card["score"] is None, "there is no pool to rank in, so there is no score"
    assert card["sym"] == "TKN"
    assert card["rank"] == 0
    assert [s["text"] for s in card["verdict"]] == ["一句话结论"]
    assert card["research_note"] == ""
    assert calls[0]["store"] is False, "a search is not a round; it must not write an entry"


def test_the_card_carries_the_mark_the_sources_gave_it(isolated_db, monkeypatch):
    """The mark travels to the card the way the numbers do. The head is drawn from this same
    payload a moment later, so a card read twice cannot show two different pictures."""
    _stub(monkeypatch)
    monkeypatch.setattr(lookup, "read", lambda cfg, ca: (
        _metrics(ca, logo_url="https://cdn.example/mark.png"), SafetyFacts()))

    row = lookup.run(CA)

    assert _card(row)["logo"] == "https://cdn.example/mark.png"


def test_a_search_hands_the_links_its_sources_named_to_the_writer(isolated_db, monkeypatch):
    """A CA search starts from scratch, so the links it hands the writer are the ones the
    sources just answered - not the ones a round filed in the snapshots table, which a token
    nobody has enriched has none of. Only the starting list differs from a round's; the
    digging that follows is the same code (6.4.3)."""
    calls, found = _stub(monkeypatch)
    live = _metrics(CA, socials={"twitter": "https://x.com/tkn"},
                    websites=["https://tkn.example"])
    monkeypatch.setattr(lookup, "read", lambda cfg, ca: (live, SafetyFacts()))

    lookup.run(CA)

    assert found[0]["known"] == [("website", "https://tkn.example"),
                                 ("twitter", "https://x.com/tkn")]


def test_a_search_leaves_the_lexicon_to_the_rounds(isolated_db, monkeypatch):
    _stub(monkeypatch)
    lookup.run(CA)

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM lexicon").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_chain_this_app_does_not_screen_still_gets_a_card(isolated_db, monkeypatch):
    """No config file means no node and no explorer, not no answer: the market half and the
    off-chain half are still read, and the on-chain half is honestly unjudged."""
    _stub(monkeypatch, chain="base", lp=None)

    def unreachable(*args, **kwargs):
        raise AssertionError("a chain with no rpc has nothing to read")

    monkeypatch.setattr(lookup.decide, "lp_verdict", unreachable)

    row = lookup.run(CA)
    card = _card(row)
    assert row["chain_id"] == "base"
    assert card["sym"] == "TKN"
    assert card["safety"]["lp"]["label"] == "未判定"
    assert card["details"], "the off-chain half still answers"


def test_a_failed_off_chain_pass_still_leaves_a_card(isolated_db, monkeypatch):
    """A token whose links will not load is still a token with market numbers and a chain
    reading. The card says which half is missing rather than looking broken."""
    _stub(monkeypatch, research=None)
    row = lookup.run(CA)
    card = _card(row)
    assert card["sym"] == "TKN"
    assert "链下调研没有结果" in card["research_note"]
    assert card["details"] == []


def _seed_round(conn, run_id, ca=CA, symbol="TKN", score=71.2, chain_id=ARC,
                at="2026-10-05T09:00:00+08:00", config="default"):
    """One round with one written card, at the shape `assemble.cards` reads back."""
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, status, scanned, dropped,"
        " candidates, selected, config_id, scheduled_at, started_at)"
        " VALUES (?, ?, ?, 'manual', 'researched', 10, 6, 3, 1, ?, ?, ?)",
        (run_id, run_id, chain_id, config, at, at),
    )
    conn.execute(
        "INSERT INTO run_items (run_id, ca, decision, score, rank, breakdown_json)"
        " VALUES (?, ?, 'selected', ?, 1, ?)",
        (run_id, ca, score, json.dumps({"total": score, "onchain": 66.0, "product": 55.0})),
    )
    conn.execute(
        "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json, created_at)"
        " VALUES (?, ?, ?, '{}', '{}', ?)",
        (run_id, ca, json.dumps({"ca": ca, "symbol": symbol, "market_cap_usd": 120000.0,
                                 "liquidity_usd": 30000.0}), at),
    )
    conn.execute(
        "INSERT INTO reports (run_id, ca, rank, verdict, status, payload_json, created_at)"
        " VALUES (?, ?, 1, ?, 'written', ?, ?)",
        (run_id, ca, "一句话结论",
         json.dumps({"verdict": "一句话结论",
                     "sections": {"叙事与主线": "和本链有关"}}), at),
    )
    conn.execute(
        "INSERT INTO run_summary (run_id, payload_json, created_at) VALUES (?, ?, ?)",
        (run_id, json.dumps({"batch_label": "2026-10-05 09:00 +08",
                             "scoring": {"onchain_weight": 55, "offchain_weight": 45}}), at),
    )


def test_a_round_takes_over_the_card_without_collecting_it(isolated_db, monkeypatch):
    """A search read the token with no settings context, so the round's card is the more
    informed one and replaces it - on the record, not on the shelf."""
    _stub(monkeypatch)
    lookup.run(CA)
    conn = db.connect()
    try:
        conn.execute("UPDATE favorites SET origin_label = '2026-09-30 16:19'")
        _seed_round(conn, "run-1")
        conn.commit()
    finally:
        conn.close()

    report_assemble.refresh_kept("run-1")

    conn = db.connect()
    try:
        row = dict(conn.execute("SELECT * FROM favorites").fetchone())
    finally:
        conn.close()
    assert row["config_id"] == "default"
    assert row["score"] == 71.2
    assert row["kept"] == 0, "the round wrote a better card, not a collected one"


def test_re_searching_does_not_take_a_collected_card_off_the_shelf(isolated_db, client,
                                                                  monkeypatch):
    """The star is the reader's, and the only write allowed to change it is the star."""
    _stub(monkeypatch)
    _search(client, monkeypatch)
    assert client.post("/api/v1/favorites/%s/%s" % (ARC, CA)).status_code == 201

    _search(client, monkeypatch)

    shelf = client.get("/api/v1/favorites").json()
    assert [r["ca"] for r in shelf] == [CA], "still collected"
    assert len(client.get("/api/v1/lookup/recent").json()) == 1, "one row, not two"


def test_collecting_a_searched_card_flips_the_row_it_already_has(isolated_db, client,
                                                                 monkeypatch):
    """The star on a searched token has no round to copy a card from - the card is already on
    file - so collecting it is one flip and no second row."""
    _stub(monkeypatch)
    assert _search(client, monkeypatch).json()["status"] == "done"
    assert client.get("/api/v1/favorites").json() == [], "a search is not a collection"

    kept = client.post("/api/v1/favorites/%s/%s" % (ARC, CA))
    assert kept.status_code == 201
    assert kept.json()["card"]["sym"] == "TKN"
    assert [r["ca"] for r in client.get("/api/v1/favorites").json()] == [CA]


def test_uncollecting_a_searched_card_leaves_it_on_the_recent_list(isolated_db, client,
                                                                   monkeypatch):
    """Unstarring a searched token is putting the record back, not deleting the answer: the
    reader looked that address up, and opening it again is still free."""
    _stub(monkeypatch)
    _search(client, monkeypatch)
    client.post("/api/v1/favorites/%s/%s" % (ARC, CA))

    gone = client.delete("/api/v1/favorites/%s/%s" % (ARC, CA))
    assert gone.status_code == 200
    assert gone.json()["removed"] is True
    assert client.get("/api/v1/favorites").json() == []
    assert len(client.get("/api/v1/lookup/recent").json()) == 1


def test_the_recent_list_runs_in_the_order_of_the_times_it_prints(isolated_db):
    """最近搜索 prints 生成时间, so it is ordered by 生成时间 - not by `saved_at`.

    The two are different clocks. `saved_at` is frozen at a row's first write, so a token
    searched twice keeps the position its first search gave it while printing the second
    search's stamp, and the list shows an older time above a newer one with neither number
    wrong. Looking one address up again is the reader saying "this one, again", and the row
    that answers goes back to the top - without `saved_at` moving, because that stamp is the
    shelf's 收藏时间."""
    other = "0x" + "b" * 40
    card = {"ca": CA, "sym": "TKN", "score": None, "rank": 0}
    first = db.favorite_put(report_assemble.lookup_row(
        ARC, CA, "TKN", card, "2026-10-01T15:23:18+08:00"))
    db.favorite_put(report_assemble.lookup_row(
        ARC, other, "OTH", card, "2026-10-01T18:02:31+08:00"))
    assert [row["symbol"] for row in db.lookups()] == ["OTH", "TKN"]

    again = db.favorite_put(report_assemble.lookup_row(
        ARC, CA, "TKN", card, "2026-10-01T19:13:08+08:00"))

    assert [row["symbol"] for row in db.lookups()] == ["TKN", "OTH"], \
        "searched again, so it is back at the top - where the time it prints says it is"
    assert again["origin_label"] == "2026-10-01 19:13", "and it prints the new search"
    assert again["saved_at"] == first["saved_at"], \
        "while saved_at, the shelf's 收藏时间, did not move"


def test_collecting_something_with_no_row_is_a_404(isolated_db, client):
    assert client.post("/api/v1/favorites/%s/%s" % (ARC, CA)).status_code == 404


def test_a_word_is_refused_before_it_is_queued(isolated_db, client):
    """The shape check is the API's own, so it answers on the request and never becomes a job."""
    assert client.post("/api/v1/lookup", json={"ca": "hello world"}).status_code == 404


def test_an_address_the_feed_never_saw_fails_the_job_with_the_reason(isolated_db, client,
                                                                    monkeypatch):
    """Whether the feed has ever seen an address can only be asked inside the run, so that
    refusal comes back on the job - first stage failed, reason kept - rather than on a request
    that would have to hold open for it."""
    _stub(monkeypatch, chain="")
    body = _search(client, monkeypatch).json()
    assert body["status"] == "failed"
    assert "没见过" in body["error"]
    stages = {stage["key"]: stage["state"] for stage in body["stages"]}
    assert stages["resolve"] == "failed"
    assert stages["market"] == "pending"
    assert body["row"] is None


def test_a_queued_search_answers_a_job_before_the_work_starts(isolated_db, client, monkeypatch):
    """What the page gets back is the list of stages to draw, all of them waiting."""
    _stub(monkeypatch)
    monkeypatch.setattr(lookup, "submit", lambda job: None)
    body = client.post("/api/v1/lookup", json={"ca": CA})
    assert body.status_code == 202
    job = body.json()
    assert job["status"] == "running" and job["stage"] == "resolve"
    assert job["ca"] == CA and job["row"] is None and job["finished_at"] is None
    assert [stage["key"] for stage in job["stages"]] == [
        "resolve", "market", "safety", "research", "card"]
    assert {stage["state"] for stage in job["stages"]} == {"pending"}
    assert client.get("/api/v1/lookup/jobs/%s" % job["id"]).json()["id"] == job["id"]


def test_a_finished_job_carries_the_card_and_a_time_for_every_stage(isolated_db, client,
                                                                   monkeypatch):
    _stub(monkeypatch)
    job = _search(client, monkeypatch).json()
    assert job["status"] == "done" and job["chain_id"] == ARC
    assert [stage["state"] for stage in job["stages"]] == ["done"] * 5
    assert all(stage["seconds"] is not None for stage in job["stages"])
    assert job["row"]["card"]["sym"] == "TKN"
    assert job["finished_at"]


def test_a_job_that_dies_late_keeps_the_stages_that_finished(isolated_db, client, monkeypatch):
    """A failure is not a blank page: what already answered stays answered."""
    _stub(monkeypatch)

    def boom(cfg, ca):
        raise RuntimeError("Explorer 没有回答")

    monkeypatch.setattr(lookup, "read", boom)
    job = _search(client, monkeypatch).json()
    assert job["status"] == "failed" and "Explorer 没有回答" in job["error"]
    stages = {stage["key"]: stage["state"] for stage in job["stages"]}
    assert stages == {"resolve": "done", "market": "failed", "safety": "pending",
                      "research": "pending", "card": "pending"}
    assert job["row"] is None


def test_a_job_id_this_app_never_made_is_a_404(isolated_db, client):
    assert client.get("/api/v1/lookup/jobs/nope").status_code == 404


def test_recent_searches_come_back_newest_first(isolated_db, client):
    older, newer = "0x" + "1" * 40, "0x" + "2" * 40

    def seed(ca, sym, at):
        card = card_builder.card(rank=0, metrics={"ca": ca, "symbol": sym}, safety={},
                                 written={}, breakdown={})
        db.favorite_put(report_assemble.lookup_row(ARC, ca, sym, card, at))

    seed(older, "OLD", "2026-09-30T10:00:00+08:00")
    seed(newer, "NEW", "2026-10-01T10:00:00+08:00")
    conn = db.connect()
    try:
        conn.execute("UPDATE favorites SET saved_at = CASE ca WHEN ? THEN ? ELSE ? END",
                     (older, "2026-09-30T10:00:00+08:00", "2026-10-01T10:00:00+08:00"))
    finally:
        conn.close()

    rows = client.get("/api/v1/lookup/recent").json()
    assert [row["symbol"] for row in rows] == ["NEW", "OLD"]
    assert rows[0]["card"]["ca"] == newer


def test_the_site_the_search_proved_is_the_one_the_card_prints(isolated_db, monkeypatch):
    """The card prints exactly one site, and it is the one that was read and tied back to the
    token. The sources listed none for this token, so before this the footer had no 官网 line
    at all however well the project had written its own page down."""
    _stub(monkeypatch, site="https://tkn.example/")

    row = lookup.run(CA)

    assert {"label": "官网", "url": "https://tkn.example/"} in _card(row)["links"]


def test_a_record_can_be_dropped_and_the_card_goes_with_it(isolated_db, client, monkeypatch):
    """搜索管理's delete. The record and the card are one row, so dropping the record is what
    "I never looked this up" means: the next look pays for a search again."""
    _stub(monkeypatch)
    _search(client, monkeypatch)
    assert len(client.get("/api/v1/lookup/recent").json()) == 1

    gone = client.delete("/api/v1/lookup/recent/%s/%s" % (ARC, CA))

    assert gone.status_code == 200
    assert gone.json()["removed"] is True
    assert client.get("/api/v1/lookup/recent").json() == []


def test_dropping_a_record_twice_is_not_an_error(isolated_db, client, monkeypatch):
    """A second press, or one another tab already made, answers false rather than 404 - the
    same way unstarring a card twice is not one."""
    _stub(monkeypatch)
    _search(client, monkeypatch)
    client.delete("/api/v1/lookup/recent/%s/%s" % (ARC, CA))

    again = client.delete("/api/v1/lookup/recent/%s/%s" % (ARC, CA))

    assert again.status_code == 200
    assert again.json()["removed"] is False


def test_the_manager_cannot_drop_a_row_the_reader_starred(isolated_db, client, monkeypatch):
    """A starred row is not only a record - it is a card on the shelf - so both deletes leave
    it where it is; 搜索管理 greys the row out rather than offering a press that would do
    nothing."""
    _stub(monkeypatch)
    _search(client, monkeypatch)
    client.post("/api/v1/favorites/%s/%s" % (ARC, CA))

    refused = client.delete("/api/v1/lookup/recent/%s/%s" % (ARC, CA))

    assert refused.status_code == 200
    assert refused.json()["removed"] is False
    assert [row["ca"] for row in client.get("/api/v1/favorites").json()] == [CA]
    assert len(client.get("/api/v1/lookup/recent").json()) == 1


def test_clearing_the_recent_list_keeps_the_starred_rows(isolated_db, client, monkeypatch):
    """清空 is one write for the whole list and the same rule as the one-row delete: what the
    reader collected stays, on the shelf and on 最近搜索."""
    _stub(monkeypatch)
    held = "0x" + "b" * 40
    for ca in (CA, held, "0x" + "c" * 40):
        assert _search(client, monkeypatch, ca=ca).status_code == 202
    client.post("/api/v1/favorites/%s/%s" % (ARC, held))

    cleared = client.delete("/api/v1/lookup/recent")

    assert cleared.status_code == 200
    assert cleared.json()["removed"] is True
    assert [row["ca"] for row in client.get("/api/v1/lookup/recent").json()] == [held]
    assert [row["ca"] for row in client.get("/api/v1/favorites").json()] == [held]


def test_clearing_a_list_with_nothing_to_drop_is_not_an_error(isolated_db, client):
    empty = client.delete("/api/v1/lookup/recent")

    assert empty.status_code == 200
    assert empty.json()["removed"] is False
