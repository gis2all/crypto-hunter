"""What an entry is written from: the pages, the posts, and the counts.

The reading pass joins four things and the model only ever sees the join. These are the
joins that decide what it sees: which link wins when two sources name the same channel, how
many posts fit, and what the round is allowed to say about people it did not read.
"""

from __future__ import annotations

from app.report import research
from app.report.research import merge_links, post_block, talk_block
from app.search.base import TweetRow
from app.search.discover import Discovery
from app.search.identify import CARRIES, OWN, Site

WHEN = "2026-09-27T10:43:55+00:00"
CA = "0x" + "a" * 40


def _post(index, handle="solonlabs1", likes=0, text="a post", when=WHEN):
    return TweetRow(id=str(index), handle=handle, name="", text=text,
                    created_at=when, likes=likes)


def test_a_harvested_channel_replaces_the_one_a_data_source_listed():
    """Same channel, two claims on it, and the claim read off the project's own page wins."""
    found = Discovery(chain="ARC", ca="0xabc",
                      harvested=[("telegram", "https://t.me/solonpad")])
    merged = merge_links(found, [("telegram", "https://t.me/old"),
                                 ("github", "https://github.com/a/b")])
    assert merged == [("telegram", "https://t.me/solonpad"),
                      ("github", "https://github.com/a/b")]


def test_a_round_that_found_nothing_new_keeps_what_it_had():
    """A search that found nothing is not a reason to read less than last round did."""
    known = [("website", "https://a.fun"), ("twitter", "https://x.com/a")]
    assert merge_links(None, known) == known


def test_the_reading_list_is_capped_however_many_sources_spoke():
    """The cap belongs here, where the reading list is finally decided."""
    found = Discovery(chain="ARC", ca="0xabc", harvested=[("telegram", "https://t.me/a")])
    known = [("github", "https://github.com/%d/repo" % i) for i in range(12)]
    assert len(merge_links(found, known)) == 8


def test_the_posts_budget_decides_how_many_arrive_not_how_long_they_may_be():
    """Half a post with its engagement stuck to it is worse than the post left out."""
    rows = [_post(i, text="x" * 60) for i in range(6)]
    text, shown = post_block(rows, 220)
    assert 0 < shown < len(rows)
    assert text.count(WHEN) == shown
    assert "x" * 60 in text



def test_the_projects_own_posts_are_not_discussion_about_the_project():
    """The only number the round owns about its audience, and the project is not its audience."""
    rows = [_post(1, "Solonlabs1", likes=99), _post(2, "stranger", likes=3),
            _post(3, "another", likes=5)]
    line = talk_block(rows, ["Solonlabs1"])
    assert "命中 2 条" in line
    assert "作者 2 个" in line
    assert "合计赞 8" in line


def test_a_round_whose_only_mentions_were_its_own_says_nothing():
    """An empty line is the honest answer; a zero would read as a measurement."""
    assert talk_block([_post(1, "solonlabs1")], ["solonlabs1"]) == ""


def test_the_header_is_not_written_for_a_story_nobody_can_count():
    """No strangers, no block - and no heading over an empty section either."""
    assert talk_block([], ["solonlabs1"]) == ""


def test_the_links_a_caller_hands_in_are_the_ones_that_are_merged(monkeypatch):
    """`known` is where a caller's own links go in, and they replace the ones the snapshots
    table would have supplied - a CA search just read them off the sources and has no row in
    that table to read them from. Nothing else about the pass changes: the engine is still
    asked, exactly as a round asks it."""
    monkeypatch.setattr(research, "_latest_metrics",
                        lambda ca: ({"socials": {"twitter": "https://x.com/stale"}}, {}))
    seen = {}

    def fake_discover(chain, ca, symbol, *, known=(), search=True):
        seen["known"], seen["search"] = list(known), search
        return None

    monkeypatch.setattr(research, "discover", fake_discover)
    out = research.gather({"ca": CA, "chain_id": "arc", "symbol": "TKN"},
                          known=[("twitter", "https://x.com/live")])

    assert seen["known"] == [("twitter", "https://x.com/live")]
    assert seen["search"] is True, "a search digs for links the way a round does"
    assert out["links"] == [("twitter", "https://x.com/live")]


def test_the_gathered_answer_carries_the_site_the_search_proved(monkeypatch):
    """The one address a card prints, handed on by the pass that read the pages: nothing
    downstream can work out which of the candidates earned the top tier."""
    monkeypatch.setattr(research, "_latest_metrics", lambda ca: ({}, {}))
    site = Site("https://solonpad.fun/", OWN, "links to solonlabs1")
    monkeypatch.setattr(research, "discover",
                        lambda *args, **kwargs: Discovery(chain="ARC", ca=CA, site=site))

    out = research.gather({"ca": CA, "chain_id": "arc", "symbol": "SOLON"})

    assert out["site"] == "https://solonpad.fun/"


def test_a_page_that_only_carries_the_address_is_not_handed_on_as_the_site(monkeypatch):
    """A listing is worth reading and is not the project's own page, and the card prints one
    site as the project's own."""
    monkeypatch.setattr(research, "_latest_metrics", lambda ca: ({}, {}))
    site = Site("https://listing.fun/", CARRIES, "carries the contract address")
    monkeypatch.setattr(research, "discover",
                        lambda *args, **kwargs: Discovery(chain="ARC", ca=CA, site=site))

    out = research.gather({"ca": CA, "chain_id": "arc", "symbol": "SOLON"})

    assert out["site"] == ""
    assert out["links"] == [("website", "https://listing.fun/")]
