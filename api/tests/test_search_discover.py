"""The flow a token takes: address in, the project's own links out."""

from __future__ import annotations

from app.search import discover as discover_mod
from app.search import identify as identify_mod
from app.search.base import EngineAnswer, SearchHit
from app.search.discover import build_query, discover
from app.search.identify import CARRIES, NONE, OWN
from app.search.service import SearchService

CA = "0xd8d51e2a23fb82fa4b50de3898c7352f108f2246"


class FakeEngine:
    name = "fake"

    def __init__(self, hits, queries=()):
        self.hits = list(hits)
        self.queries = list(queries)
        self.asked = []

    def search(self, query, *, limit=20):
        self.asked.append(query)
        return EngineAnswer(hits=self.hits[:limit], queries=self.queries)


def _service(hits, queries=()):
    return SearchService([FakeEngine(hits, queries)])


def _page(bio: str, body: str = "") -> str:
    return ('<meta property="og:description" content="%s">%s' % (bio, body))


def test_the_question_quotes_the_address_because_it_cannot_be_paraphrased():
    query = build_query("Arc", CA, "STUDIO")
    assert CA in query and "Arc" in query and "STUDIO" in query


def test_a_discovery_returns_only_the_links_that_survived_a_read():
    hits = [SearchHit("https://x.com/arcstudio_ai", "fake", "x"),
            SearchHit("https://x.com/spammer", "fake", "x"),
            SearchHit("https://arcstudio.fun", "fake", "web")]
    pages = {
        "https://x.com/arcstudio_ai": _page("AI studio on Arc " + CA),
        "https://x.com/spammer": _page("gm", CA),
        "https://arcstudio.fun": '<a href="https://x.com/arcstudio_ai">X</a>',
    }
    found = discover("Arc", CA, "STUDIO", service=_service(hits, ["q1", "q2"]),
                     fetch=pages.__getitem__)
    assert found.official_handles == ["arcstudio_ai"]
    assert found.website == "https://arcstudio.fun"
    assert found.links == ["https://x.com/arcstudio_ai", "https://arcstudio.fun"]
    assert found.found
    assert found.queries == ["q1", "q2"]
    assert (("https://x.com/spammer", CARRIES) in found.checked)


def test_an_account_that_only_quotes_the_address_is_never_the_official_one():
    """The airdrop account is the case this whole layer is built against: it posts the
    address, it is not the project, and the card must not link to it."""
    hits = [SearchHit("https://x.com/spammer", "fake", "x")]
    pages = {"https://x.com/spammer": _page("gm", CA)}
    found = discover("Arc", CA, service=_service(hits), fetch=pages.__getitem__)
    assert found.official_handles == []
    assert found.profiles[0].tier == CARRIES
    assert found.links == []


def test_a_page_that_carries_the_address_is_a_lead_even_with_no_account_to_confirm_it():
    """The site is worth keeping when it quotes the exact address - 40 hex characters is
    not something a stranger's page produces by accident - and it is recorded as the
    weaker tier rather than as an official match."""
    hits = [SearchHit("https://arcstudio.fun", "fake", "web")]
    found = discover("Arc", CA, service=_service(hits),
                     fetch=lambda url: "<p>token %s</p>" % CA)
    assert found.website == "https://arcstudio.fun"
    assert found.site.tier == CARRIES
    assert found.links == ["https://arcstudio.fun"]


def test_a_lead_that_carries_nothing_leaves_the_discovery_empty_and_says_what_it_read():
    hits = [SearchHit("https://somewhere.fun", "fake", "web"),
            SearchHit("https://x.com/nobody", "fake", "x")]
    pages = {
        "https://somewhere.fun": "<p>nothing to do with it</p>",
        "https://x.com/nobody": _page("just a person"),
    }
    found = discover("Arc", CA, service=_service(hits), fetch=pages.__getitem__)
    assert not found.found
    assert found.links == []
    assert found.website is None
    assert found.checked == [("https://x.com/nobody", NONE), ("https://somewhere.fun", NONE)]


def test_a_site_and_an_account_that_point_at_each_other_confirm_each_other():
    """The case the two rules exist for: the site names the token but never mentions the
    account, and the account links to the site but never puts the address in its bio.
    Neither half is official on its own, and together they are one project."""
    hits = [SearchHit("https://x.com/registraicc", "fake", "x"),
            SearchHit("https://registrai.cc", "fake", "web")]
    pages = {
        "https://registrai.cc": _page("$REGI bootstraps Registrai on Arc",
                                      "<p>%s</p>" % CA),
        "https://x.com/registraicc": _page("New crowdfunding app",
                                           '<a href="https://registrai.cc">site</a> ' + CA),
    }
    found = discover("Arc", CA, "REGI", service=_service(hits), fetch=pages.__getitem__)
    assert found.official_handles == ["registraicc"]
    assert found.website == "https://registrai.cc"
    assert found.found
    assert found.site.tier == OWN


def test_a_site_is_confirmed_by_linking_to_an_account_confirmed_by_its_bio():
    """The other direction, which needs no new rule: the account earned its tier from the
    bio, and the second pass is where the site learns about it."""
    hits = [SearchHit("https://x.com/arcstudio_ai", "fake", "x"),
            SearchHit("https://arcstudio.fun", "fake", "web")]
    pages = {
        "https://x.com/arcstudio_ai": _page("AI studio on Arc " + CA),
        "https://arcstudio.fun": '<a href="https://x.com/arcstudio_ai">X</a>',
    }
    found = discover("Arc", CA, "STUDIO", service=_service(hits), fetch=pages.__getitem__)
    assert found.website == "https://arcstudio.fun"
    assert found.site.tier == OWN


def test_a_site_is_read_once_even_though_both_passes_ask_for_it():
    """The second pass exists so a site can be upgraded; paying for it twice at the slowest
    point in the round is not part of the deal."""
    reads = []
    hits = [SearchHit("https://x.com/registraicc", "fake", "x"),
            SearchHit("https://registrai.cc", "fake", "web")]
    pages = {
        "https://registrai.cc": _page("$REGI bootstraps Registrai on Arc",
                                      "<p>%s</p>" % CA),
        "https://x.com/registraicc": _page("New crowdfunding app",
                                           '<a href="https://registrai.cc">site</a> ' + CA),
    }

    def fetch(url):
        reads.append(url)
        return pages[url]

    discover("Arc", CA, "REGI", service=_service(hits), fetch=fetch)
    assert reads.count("https://registrai.cc") == 1
    assert reads.count("https://x.com/registraicc") == 1


def test_a_lead_that_only_quotes_the_address_never_promotes_the_account_to_official():
    """Two halves of a project is the standard. A page that carries the address and an
    account that carries the address are two pages that could be about anybody."""
    hits = [SearchHit("https://x.com/spammer", "fake", "x"),
            SearchHit("https://listing.fun", "fake", "web")]
    pages = {
        "https://x.com/spammer": _page("gm", CA),
        "https://listing.fun": "<p>%s</p>" % CA,
    }
    found = discover("Arc", CA, "REGI", service=_service(hits), fetch=pages.__getitem__)
    assert found.official_handles == []
    assert found.profiles[0].tier == CARRIES


def test_searching_can_be_switched_off_while_the_round_s_links_are_still_verified():
    """Verification is a page read and costs nothing; the engine call is the expensive half.
    A caller that already has enough to read pays only for the read."""
    engine = FakeEngine([])
    hits = [SearchHit("https://x.com/registraicc", "source", "x"),
            SearchHit("https://registrai.cc", "source", "web")]
    pages = {
        # known_hits normalises, so the fixture is keyed by the URL the fetch is
        # actually called with - a bare host comes back with its trailing slash.
        "https://registrai.cc/": _page("$REGI bootstraps Registrai on Arc", "<p>%s</p>" % CA),
        "https://x.com/registraicc": _page("New crowdfunding app",
                                           '<a href="https://registrai.cc">site</a> ' + CA),
    }
    found = discover("Arc", CA, "REGI", service=SearchService([engine]), fetch=pages.__getitem__,
                     known=[("twitter", "https://x.com/registraicc"),
                            ("website", "https://registrai.cc")],
                     search=False)
    assert engine.asked == []
    assert found.official_handles == ["registraicc"]
    assert found.website == "https://registrai.cc/"


def test_a_round_link_is_verified_the_same_way_a_search_hit_is():
    """A launchpad's record of a handle is a candidate, not a confirmation: it earns the
    tier from the bio like anything else."""
    engine = FakeEngine([])
    pages = {
        "https://x.com/arcstudio_ai": _page("AI studio on Arc " + CA),
    }
    found = discover("Arc", CA, "STUDIO", service=SearchService([engine]),
                     fetch=pages.__getitem__,
                     known=[("twitter", "https://x.com/arcstudio_ai")], search=False)
    assert found.official_handles == ["arcstudio_ai"]
    assert found.profiles[0].evidence == "the bio carries the contract address"


SHELL = ('<html><head><title>Memespad</title></head><body><div id="root"></div>'
         '<script>boot()</script></body></html>')
DRAWN = ('<html><body>Memespad is a launchpad on Arc and the address is %s.</body></html>')


def test_a_blank_site_is_drawn_while_the_accounts_are_read_as_they_arrive(monkeypatch):
    """One fetch would make every account page pay for a browser it cannot use: X serves a
    profile's bio to a plain client and refuses the rest to a browser that is not logged in."""
    hits = [SearchHit("https://x.com/memes_pad", "fake", "x"),
            SearchHit("https://memespad.com", "fake", "web")]
    drawn: list[str] = []

    def plain(url):
        return SHELL if url.endswith("memespad.com") else _page("MEMES_PAD on Arc")

    # Both modules name the plain reader: identify for the site it falls back around, and
    # discover for the accounts it reads with it. The browser is patched once, because
    # there is only one.
    monkeypatch.setattr(identify_mod, "fetch_html", plain)
    monkeypatch.setattr(discover_mod, "fetch_html", plain)
    monkeypatch.setattr(identify_mod.browser, "render",
                        lambda url: drawn.append(url) or DRAWN % CA)

    found = discover("Arc", CA, "MEMESPAD", service=_service(hits))

    assert drawn == ["https://memespad.com"], "the browser is opened for the site alone"
    assert found.site is not None and found.site.tier == CARRIES
