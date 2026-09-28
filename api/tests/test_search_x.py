"""The X engine: two questions, one ordered answer, and no judgement of its own.

Everything here runs on a fake client, because the real one is a subprocess holding a
logged-in session. What is asserted is only what this module decides - which questions go
out, which accounts come back in what order, and which URLs count as a site - since the
tier of an account is earned somewhere else, by reading its page.
"""

from __future__ import annotations

import pytest

from app.search.engines.xsearch import PROFILE_HITS, XEngine, affinity, root_of

CA = "0xd8d51e2a23fb82fa4b50de3898c7352f108f2246"
QUESTION = ("Find the official links of the crypto token on the arc chain with contract "
            "address %s (ticker FUCI) Search X for the address first." % CA)


class FakeCli:
    """The subprocess, replaced: argv in, tweets out, and a record of what was asked."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.argv: list[list[str]] = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        return list(self.answers.get((argv[2], argv[4])) or [])

    @property
    def asked(self):
        return [(argv[2], argv[4]) for argv in self.argv]


def _tweet(handle, name="", text="", urls=(), retweet=False):
    return {"isRetweet": retweet, "author": {"screenName": handle, "name": name},
            "text": text, "urls": [{"expandedUrl": url} for url in urls]}


def _engine(answers):
    cli = FakeCli(answers)
    return XEngine(runner=cli), cli


def test_the_address_is_asked_first_and_its_answer_becomes_an_account():
    engine, cli = _engine({(CA, "Latest"): [_tweet("MEMES_PAD", "Meme's Pad", urls=["https://x.com/MEMES_PAD/status/1"])]})
    answer = engine.search(QUESTION)
    assert cli.asked == [(CA, "Latest"), ("$FUCI", "Top")]
    assert (answer.hits[0].url, answer.hits[0].kind) == ("https://x.com/MEMES_PAD", "x")


def test_the_ticker_is_asked_too_because_some_accounts_never_post_the_address():
    """Measured on the last Arc round: the address alone never surfaced FUCI's account."""
    engine, cli = _engine({(CA, "Latest"): [_tweet("CryptChaz", "stranger")],
                           ("$FUCI", "Top"): [_tweet("fucidotfamily", "FUCI")]})
    answer = engine.search(QUESTION)
    assert ("$FUCI", "Top") in cli.asked
    assert "https://x.com/fucidotfamily" in [hit.url for hit in answer.hits]


def test_the_reading_order_puts_the_account_that_looks_like_the_ticker_first():
    """A crowd of shills outnumbers the official account; the order is what fixes that."""
    crowd = [_tweet("CryptChaz", "degen", text="x") for _ in range(8)]
    engine, _ = _engine({(CA, "Latest"): crowd + [_tweet("fucidotfamily", "FUCI")],
                         ("$FUCI", "Top"): []})
    answer = engine.search(QUESTION)
    assert answer.hits[0].url == "https://x.com/fucidotfamily"


def test_a_retweet_does_not_put_its_repeater_in_the_reading_order():
    """The account a retweet credits is in the text, so the repeater is a stranger."""
    engine, _ = _engine({(CA, "Latest"): [_tweet("fucidotfamily", retweet=True)],
                         ("$FUCI", "Top"): []})
    assert [hit.url for hit in engine.search(QUESTION).hits] == []


def test_only_the_best_accounts_are_offered_for_reading():
    """One page per candidate: the list is cut where the reading budget is."""
    tweets = [_tweet("stranger%d" % index) for index in range(30)]
    engine, _ = _engine({(CA, "Latest"): tweets, ("$FUCI", "Top"): []})
    hits = engine.search(QUESTION).hits
    assert len(hits) == PROFILE_HITS


def test_a_deep_link_is_trimmed_to_the_site_that_hosts_it():
    engine, _ = _engine({(CA, "Latest"): [_tweet("Usearckit", urls=["https://usearckit.online/stake/3"])],
                         ("$FUCI", "Top"): []})
    assert "https://usearckit.online/" in [hit.url for hit in engine.search(QUESTION).hits]


def test_a_site_linked_twice_is_offered_once():
    engine, _ = _engine({(CA, "Latest"): [_tweet("a", urls=["https://x.fun/one"]),
                                          _tweet("b", urls=["https://x.fun/two"])],
                         ("$FUCI", "Top"): []})
    urls = [hit.url for hit in engine.search(QUESTION).hits]
    assert urls.count("https://x.fun/") == 1


def test_a_permalink_is_the_tweet_and_not_a_place_the_project_lives():
    engine, _ = _engine({(CA, "Latest"): [_tweet("a", urls=["https://x.com/a/status/9"])],
                         ("$FUCI", "Top"): []})
    assert [hit.url for hit in engine.search(QUESTION).hits] == ["https://x.com/a"]


def test_a_question_without_an_address_asks_the_client_nothing():
    engine, cli = _engine({})
    answer = engine.search("Find the official links of some token")
    assert answer.hits == [] and cli.argv == []


def test_missing_credentials_are_a_refusal_and_not_an_empty_answer(monkeypatch):
    """A round has to be able to tell "X found nothing" from "X was never asked"."""
    monkeypatch.delenv("CH_X_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CH_X_CT0", raising=False)
    with pytest.raises(RuntimeError) as refusal:
        XEngine().search(QUESTION)
    assert "CH_X_AUTH_TOKEN" in str(refusal.value)


def test_a_cli_that_fails_is_reported_with_what_it_said(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "WARNING twitter_cli.client: Rate limited (429), retrying in 10.5s"

    monkeypatch.setattr("app.search.engines.xsearch.subprocess.run",
                        lambda *args, **kwargs: Failed())
    engine = XEngine(token="t", ct0="c")
    with pytest.raises(RuntimeError) as failure:
        engine.search(QUESTION)
    assert "429" in str(failure.value)


def test_the_name_lookalike_tiers_are_ordered_and_bounded():
    """Four tiers, measured against the handles the last Arc round actually returned."""
    assert affinity("MEMES_PAD", "Meme's Pad", "MEMESPAD") == 4      # handle equals ticker
    assert affinity("fucidotfamily", "FUCI", "FUCI") == 3           # ticker inside handle
    assert affinity("batpool", "ArcBat launch", "ARCBAT") == 2      # ticker only in the name
    assert affinity("arcan", "something else", "ARCANUM") == 1      # four-letter prefix only
    assert affinity("Usearckit", "ArcKit", "AKIT") == 0             # nothing in common


def test_a_link_is_trimmed_to_its_host():
    assert root_of("https://usearckit.online/stake/3?x=1") == "https://usearckit.online/"
    assert root_of("not a url") == "not a url"


def _post(tweet_id, handle, text="hello", likes=0, retweets=0, replies=0, views=0,
          created="2026-09-27T10:43:55+00:00", retweet=False):
    """One row of the client's output, in the shape `--json` prints it."""
    return {"id": tweet_id, "isRetweet": retweet, "createdAtISO": created,
            "metrics": {"likes": likes, "retweets": retweets, "replies": replies,
                        "views": views},
            "author": {"screenName": handle, "name": handle.title()},
            "text": text, "urls": []}


class FakeTimeline:
    """The subprocess for a timeline: argv in, posts out, and a record of what was asked."""

    def __init__(self, posts=()):
        self.posts = list(posts)
        self.argv: list[list[str]] = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        return list(self.posts)


def test_a_timeline_is_the_accounts_own_posts_with_their_engagement():
    """The round's second question, and the only one asked about a handle rather than a token."""
    cli = FakeTimeline([_post("1", "Solonlabs1", "Season 0 settles today at 06:00 UTC.",
                              likes=35, retweets=7, replies=1, views=1147,
                              created="2026-09-27T03:20:46+00:00")])
    rows = XEngine(runner=cli).timeline("Solonlabs1")
    assert cli.argv == [["twitter", "user-posts", "Solonlabs1", "--max", "20", "--json"]]
    assert [(row.id, row.likes, row.retweets, row.views, row.created_at) for row in rows] == [
        ("1", 35, 7, 1147, "2026-09-27T03:20:46+00:00")]


def test_a_retweet_is_not_the_accounts_own_post():
    """Its engagement is somebody else's audience, and crediting the project with it is the
    same error the reading order refuses to make."""
    cli = FakeTimeline([_post("1", "someone", retweet=True), _post("2", "Solonlabs1")])
    assert [row.id for row in XEngine(runner=cli).timeline("Solonlabs1")] == ["2"]


def test_an_at_sign_is_not_part_of_the_handle():
    """A handle copied from a bio arrives with one, and the client wants it without."""
    cli = FakeTimeline([_post("1", "Solonlabs1")])
    XEngine(runner=cli).timeline("@Solonlabs1")
    assert cli.argv[0][2] == "Solonlabs1"


def test_a_timeline_without_a_handle_asks_nothing():
    cli = FakeTimeline([_post("1", "Solonlabs1")])
    assert XEngine(runner=cli).timeline("  ") == []
    assert cli.argv == []


def test_a_post_that_answered_both_questions_is_counted_once():
    """A tweet can come back for the address and for the ticker; counting it twice inflates
    the one number the round owns about who is talking."""
    tweet = _post("7", "someone")
    cli = FakeCli({(CA, "Latest"): [tweet], ("$FUCI", "Top"): [tweet]})
    answer = XEngine(runner=cli).search(QUESTION)
    assert [row.id for row in answer.tweets] == ["7"]


def test_a_row_the_client_did_not_finish_is_not_a_post():
    """A post with no text or no author is a parse to drop, not a crash and not a row."""
    cli = FakeTimeline([_post("1", "Solonlabs1", text=""),
                        {"id": "2", "isRetweet": False, "text": "no author"},
                        _post("3", "Solonlabs1")])
    assert [row.id for row in XEngine(runner=cli).timeline("Solonlabs1")] == ["3"]


def test_a_count_the_client_did_not_send_is_a_zero():
    """An engagement block with a missing field is a post, not an exception in a round."""
    cli = FakeTimeline([{"id": "1", "isRetweet": False, "text": "hi",
                         "author": {"screenName": "Solonlabs1"},
                         "metrics": {"likes": None, "views": "many"}}])
    row = XEngine(runner=cli).timeline("Solonlabs1")[0]
    assert (row.likes, row.views) == (0, 0)
