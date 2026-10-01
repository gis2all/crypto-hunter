"""The rule that decides which lead is the project.

Every test here is about evidence the project wrote about itself, or evidence it did not.
The negative cases matter more than the positive one: naming a stranger's account as the
project is the failure this layer exists to prevent.
"""

from __future__ import annotations

from app.search import identify as identify_mod
from app.search.base import SearchHit
from app.sources import http
from app.search.identify import (CARRIES, NONE, OWN, SHELL_WORDS, check_sites,
                                 check_website, classify, description_of, identify,
                                 is_shell, mentions, names_token, pick_website,
                                 promote_by_site, read_profiles, website_candidates)

CA = "0xd8d51e2a23fb82fa4b50de3898c7352f108f2246"


def _profile_page(bio: str, body: str = "") -> str:
    return ('<!DOCTYPE html><html><head>'
            '<meta property="og:title" content="ArcStudio (@arcstudio_ai) on X">'
            '<meta property="og:description" content="%s">'
            '</head><body>%s</body></html>' % (bio, body))


def _hit(url: str, kind: str = "x") -> SearchHit:
    return SearchHit(url=url, engine="grok", kind=kind)


def test_the_bio_is_the_string_x_publishes_for_crawlers():
    page = _profile_page("The First AI studio for Creators on Arc &amp; more")
    assert description_of(page) == "The First AI studio for Creators on Arc & more"


def test_a_page_with_no_description_has_an_empty_bio():
    assert description_of("<html><head></head></html>") == ""
    assert description_of("") == ""


def test_an_address_is_recognised_whatever_case_or_prefix_it_was_typed_with():
    assert mentions(CA, CA)
    assert mentions(CA.upper(), CA)
    assert mentions(CA.replace("0x", ""), CA)
    assert mentions("CA: " + CA.upper(), CA)
    # A truncated address is a different token, and an empty page says nothing.
    assert not mentions("0xd8d51e2a23fb82fa4b50de3898c7352f108f224", CA)
    assert not mentions("", CA)
    assert not mentions("some other text", CA)


def test_the_bio_is_what_makes_an_account_official():
    page = _profile_page("The First AI studio for Creators on Arc\n\n" + CA)
    assert classify(page, CA) == (OWN, "The First AI studio for Creators on Arc\n\n" + CA)


def test_an_account_that_only_posted_the_address_is_not_official():
    """This is the airdrop account: it quotes the address in a post, and its own
    description says something else entirely."""
    page = _profile_page("Multi-follow back. DM for promo.",
                         "<div>new gem %s 100x</div>" % CA)
    tier, bio = classify(page, CA)
    assert tier == CARRIES
    assert bio == "Multi-follow back. DM for promo."


def test_an_account_with_the_address_nowhere_is_ruled_out():
    page = _profile_page("New crowdfunding app, join tg")
    assert classify(page, CA) == (NONE, "New crowdfunding app, join tg")


def test_every_candidate_is_read_and_the_best_one_comes_first():
    pages = {
        "https://x.com/spammer": _profile_page("gm", CA),
        "https://x.com/arcstudio_ai": _profile_page("AI studio on Arc " + CA),
    }
    hits = [_hit("https://x.com/spammer"), _hit("https://x.com/arcstudio_ai")]
    found = identify(hits, CA, fetch=pages.__getitem__)
    assert [profile.handle for profile in found] == ["arcstudio_ai", "spammer"]
    assert [profile.tier for profile in found] == [OWN, CARRIES]
    assert found[0].official and not found[1].official


def test_a_profile_that_cannot_be_read_is_ruled_out_rather_than_guessed_at():
    def fetch(url):
        raise RuntimeError("HTTP 404")

    found = identify([_hit("https://x.com/ghost")], CA, fetch=fetch)
    assert found[0].tier == NONE
    assert "404" in found[0].evidence


def test_the_numeric_and_reserved_urls_never_become_candidates():
    hits = [_hit("https://x.com/i/user/123"), _hit("https://x.com/search?q=x"),
            _hit("https://arcstudio.fun", kind="web")]
    assert identify(hits, CA, fetch=lambda url: "") == []


def test_only_the_first_few_candidates_are_read_because_each_one_costs_a_page():
    hits = [_hit("https://x.com/a%d" % index) for index in range(20)]
    seen = []

    def fetch(url):
        seen.append(url)
        return _profile_page("nothing here")

    identify(hits, CA, fetch=fetch, limit=3)
    assert seen == ["https://x.com/a0", "https://x.com/a1", "https://x.com/a2"]


def test_the_aggregators_are_never_treated_as_the_projects_site():
    hits = [_hit("https://gmgn.ai/arc/token/" + CA, kind="web"),
            _hit("https://www.dexscreener.com/arc/" + CA, kind="web"),
            _hit("https://arcstudio.fun", kind="web"),
            _hit("https://x.com/arcstudio_ai")]
    assert website_candidates(hits) == ["https://arcstudio.fun"]


def test_the_exchanges_and_other_chains_launchpads_are_never_the_projects_site():
    """A live run read lbank.com and nomics.com for one token and pump.fun and arctools.fun
    for two more, twelve seconds a page, and got nothing back. A market page lists every
    token there is; that is what it is for."""
    hits = [_hit("https://www.lbank.com/trade/" + CA, kind="web"),
            _hit("https://nomics.com/assets/abc", kind="web"),
            _hit("https://pump.fun/coin/abc", kind="web"),
            _hit("https://arctools.fun/token/" + CA, kind="web"),
            _hit("https://arcstudio.fun", kind="web")]
    assert website_candidates(hits) == ["https://arcstudio.fun"]



def test_the_chains_own_sites_are_never_the_projects_site():
    """One live run offered the Arc explorer, Uniswap and the chain's own blog as the
    token's website, because all three carried the address. Carrying it is their job."""
    hits = [_hit("https://explorer.arc.io/token/" + CA, kind="web"),
            _hit("https://app.uniswap.org/explore/tokens/arc/" + CA, kind="web"),
            _hit("https://arc.io/blog/introducing-arc-studio", kind="web"),
            _hit("https://developers.circle.com/ai/arc-studio", kind="web"),
            _hit("https://arcstudio.fun", kind="web")]
    assert website_candidates(hits) == ["https://arcstudio.fun"]


def test_a_site_that_links_to_the_verified_account_is_the_projects_own():
    site = check_website("https://arcstudio.fun", CA, ["arcstudio_ai"],
                         fetch=lambda url: '<a href="https://x.com/arcstudio_ai">X</a>')
    assert site.tier == OWN
    assert "arcstudio_ai" in site.evidence


def test_a_site_that_only_carries_the_address_is_a_listing():
    site = check_website("https://somewhere.fun", CA, ["arcstudio_ai"],
                         fetch=lambda url: "<p>token %s</p>" % CA)
    assert site.tier == CARRIES
    assert site.confirmed


def test_the_confirmed_site_beats_the_listing_that_carries_the_address():
    pages = {
        "https://listing.fun": "<p>%s</p>" % CA,
        "https://arcstudio.fun": '<a href="https://x.com/arcstudio_ai">X</a>',
    }
    hits = [_hit("https://listing.fun", kind="web"), _hit("https://arcstudio.fun", kind="web")]
    site = pick_website(hits, CA, ["arcstudio_ai"], fetch=pages.__getitem__)
    assert site.url == "https://arcstudio.fun"
    assert site.tier == OWN


def test_a_site_that_could_not_be_read_is_reported_as_unconfirmed():
    def fetch(url):
        raise RuntimeError("timed out")

    site = check_website("https://arcstudio.fun", CA, [], fetch=fetch)
    assert site.tier == NONE
    assert not site.confirmed
    assert "timed out" in site.evidence


def test_a_token_is_named_by_its_dollar_form_not_by_a_word_that_looks_like_it():
    """A live run offered a Japanese blog post and a company registry as one token's
    homepage because the ticker turned up as an ordinary word on them."""
    assert names_token("$REGI bootstraps Registrai", "REGI")
    assert names_token("we ship $regi weekly", "regi")
    assert not names_token("a creative studio in Berlin", "STUDIO")
    assert not names_token("$REGISTRAI is live", "REGI")
    assert not names_token("", "REGI")
    # A symbol too short to mean anything, or not a symbol at all, is never matched.
    assert not names_token("$AI does things", "AI")
    assert not names_token("$REGI is live", "--")


def test_a_site_that_names_the_token_in_its_own_description_is_the_projects_own():
    page = _profile_page("$REGI bootstraps Registrai on Arc", "<p>%s</p>" % CA)
    site = check_website("https://registrai.cc", CA, [], fetch=lambda url: page, symbol="REGI")
    assert site.tier == OWN
    assert "REGI" in site.evidence


def test_naming_the_token_is_not_enough_without_the_address_on_the_page():
    """The pair is the evidence. A page that only writes a ticker is one of the many pages
    about a ticker."""
    page = _profile_page("$REGI bootstraps Registrai on Arc")
    site = check_website("https://registrai.cc", CA, [], fetch=lambda url: page, symbol="REGI")
    assert site.tier == NONE


def test_an_account_that_links_to_a_confirmed_site_becomes_that_site_s_account():
    hits = [_hit("https://x.com/registraicc")]
    page = _profile_page("New crowdfunding app",
                         '<a href="https://registrai.cc">site</a> ' + CA)
    profiles, pages = read_profiles(hits, CA, fetch=lambda url: page)
    assert profiles[0].tier == CARRIES

    promoted = promote_by_site(profiles, pages, ["registrai.cc"])
    assert promoted[0].tier == OWN
    assert promoted[0].official
    assert "registrai.cc" in promoted[0].evidence


def test_an_account_is_left_alone_when_nothing_about_it_points_at_a_confirmed_site():
    hits = [_hit("https://x.com/spammer")]
    page = _profile_page("gm", CA)
    profiles, pages = read_profiles(hits, CA, fetch=lambda url: page)
    promoted = promote_by_site(profiles, pages, ["registrai.cc"])
    assert promoted[0].tier == CARRIES
    assert not promoted[0].official


def test_promotion_without_a_confirmed_site_changes_nothing():
    hits = [_hit("https://x.com/registraicc")]
    profiles, pages = read_profiles(hits, CA, fetch=lambda url: _profile_page("x", CA))
    assert promote_by_site(profiles, pages, []) == profiles


def test_a_profile_that_could_not_be_read_is_never_promoted():
    def fetch(url):
        raise RuntimeError("HTTP 404")

    hits = [_hit("https://x.com/ghost")]
    profiles, pages = read_profiles(hits, CA, fetch=fetch)
    promoted = promote_by_site(profiles, pages, ["registrai.cc"])
    assert promoted[0].tier == NONE


#: What the launchpad's own site served on 2026-09-27: a title, an empty root element and
#: the script that would have filled it in. Eight words of text once the markup is gone.
SHELL = ('<html><head><title>Memespad - Meme Token Launchpad</title></head>'
         '<body><div id="root"></div><script>boot()</script></body></html>')


#: What the same site serves once a browser has run it: sentences, and the address in them.
DRAWN = ('<html><body>Memespad is a meme token launchpad on Arc. It trades in USDC and the '
         'address is %s.</body></html>')


def _never_draw(url):
    raise AssertionError("no browser should have been started for %s" % url)


def test_a_shell_is_a_document_with_nothing_drawn_in_it_yet():
    assert is_shell(SHELL)
    assert is_shell('')
    assert is_shell('<html><body><div id="root"></div></body></html>')
    assert not is_shell('<html><body>%s</body></html>' % ('word ' * SHELL_WORDS))


def test_a_page_that_arrived_empty_is_drawn_before_it_is_judged(monkeypatch):
    """The failure this fixes: the launchpad's own site scored none, and a stranger's page
    that happened to carry the same address was linked on the card as the project."""
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: SHELL)
    monkeypatch.setattr(identify_mod.browser, "render",
                        lambda url: DRAWN % CA)

    site = check_website("https://memespad.com", CA, [])

    assert site.tier == CARRIES


def test_a_page_that_arrived_with_words_is_never_drawn(monkeypatch):
    page = "<html><body>%s%s</body></html>" % ("word " * 100, CA)
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: page)
    monkeypatch.setattr(identify_mod.browser, "render", _never_draw)

    site = check_website("https://arcstudio.fun", CA, [])

    assert site.tier == CARRIES


def test_a_page_that_stays_empty_keeps_what_the_plain_lane_had(monkeypatch):
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: SHELL)
    monkeypatch.setattr(identify_mod.browser, "render", lambda url: "")

    site = check_website("https://memespad.com", CA, [])

    assert site.tier == NONE, "a browser that cannot answer does not invent one"


def test_a_draw_that_added_nothing_keeps_the_plain_document(monkeypatch):
    """The browser is worth exactly what it added. A page that comes back with fewer words
    than the markup already had is a page that did not need one - or could not use it."""
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: SHELL)
    monkeypatch.setattr(identify_mod.browser, "render", lambda url: "<html>x</html>")

    site = check_website("https://memespad.com", CA, [])

    assert site.tier == NONE


def test_a_page_that_stays_empty_even_drawn_keeps_the_plain_one(monkeypatch):
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: SHELL)
    monkeypatch.setattr(identify_mod.browser, "render", lambda url: SHELL)

    site = check_website("https://memespad.com", CA, [])

    assert site.tier == NONE


def test_a_site_that_refuses_the_plain_lanes_is_still_drawn(monkeypatch):
    """A 403 is the other way a document arrives with nothing in it."""
    def refuse(url):
        raise http.SourceError('403')

    monkeypatch.setattr(identify_mod, "fetch_html", refuse)
    monkeypatch.setattr(identify_mod.browser, "render",
                        lambda url: DRAWN % CA)

    site = check_website("https://memespad.com", CA, [])

    assert site.tier == CARRIES


def test_an_account_page_is_not_drawn_even_when_it_arrives_empty(monkeypatch):
    """X answers a browser nobody signed in to with a login wall, so drawing a profile costs
    seconds and comes back with less than the document already had."""
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: SHELL)
    monkeypatch.setattr(identify_mod.browser, "render", _never_draw)

    assert identify_mod.fetch_page("https://x.com/arcstudio_ai") == SHELL
    assert identify_mod.fetch_page("https://twitter.com/arcstudio_ai") == SHELL
    assert identify_mod.fetch_page("https://t.me/registrai") == SHELL


def test_an_account_page_is_read_without_drawing_it(monkeypatch):
    """A profile is not a site: its bio is in the document, and X does not serve the rest
    of a page to a browser that is not logged in."""
    monkeypatch.setattr(identify_mod, "fetch_html", lambda url: _profile_page(CA))
    monkeypatch.setattr(identify_mod.browser, "render", _never_draw)

    profiles, pages = read_profiles([_hit("https://x.com/arcstudio_ai")], CA)

    assert profiles[0].tier == OWN
    assert pages, "and the page comes back with the answer for the site to agree with"


def test_a_site_the_projects_own_page_named_is_read_before_the_models_hits():
    """The candidate a caller found on the project's own pages - a bio that names the site -
    is the better lead, and it shares the cap with the search's own hits."""
    hits = [_hit("https://arcstudio.fun", kind="web")]
    assert website_candidates(hits, extra=["https://from-the-bio.example"]) == [
        "https://from-the-bio.example", "https://arcstudio.fun"]


def test_an_extra_candidate_is_read_like_any_other_site():
    """Nothing about an extra is trusted: it is fetched, and the page decides its tier."""
    pages = {"https://from-the-bio.example": "<p>token %s</p>" % CA}
    sites = check_sites([], CA, [], fetch=pages.__getitem__,
                        extra=["https://from-the-bio.example"])
    assert [(site.url, site.tier) for site in sites] == [
        ("https://from-the-bio.example", CARRIES)]
