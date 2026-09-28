"""What a proved page hands on: candidates, and only the kinds worth reading.

The taking, not the believing. Everything here is about which links come out of a page and
in what order; which of them is real is decided downstream, the same way it is for a search
hit. The rule that matters most is the one that keeps a page's whole document out of the
answer: a bundle carries the URL of everything it could ever draw, and a market table is not
a project's list of channels.
"""

from __future__ import annotations

from app.search.harvest import HARVEST_ACCOUNTS, candidates

SITE = 'https://solonpad.fun'


def _page(body: str, url: str = SITE, kind: str = 'web'):
    return [(kind, url, body)]


def test_only_the_links_a_page_shows_are_read():
    """An anchor is a link somebody put on the page; a bare URL in a bundle is not."""
    body = ('<a href="https://x.com/arcat_meme">x</a>'
            '<script>var all=["https://x.com/Apple","https://x.com/BlubBlobCoin"];</script>')
    assert candidates(_page(body)) == [('twitter', 'https://x.com/arcat_meme')]


def test_the_ticker_decides_which_account_spends_the_two_slots():
    """A market table sorts by volume and a footer comes last; the ticker is what breaks the tie."""
    body = ''.join('<a href="https://x.com/%s">%s</a>' % (h, h)
                   for h in ('Apple', 'BlubBlobCoin', 'Solonlabs1'))
    assert candidates(_page(body), 'SOLON') == [('twitter', 'https://x.com/Solonlabs1'),
                                                  ('twitter', 'https://x.com/Apple')]


def test_a_profile_page_is_read_through_its_own_data():
    """X rewrites every link it shows to t.co, so the expansion is the link and the anchor is not."""
    body = ('<a href="https://t.co/EL3srb2Tbk">t.me</a>'
            '{"expanded_url":"https://t.me/solonpad"}')
    assert candidates(_page(body, 'https://x.com/Solonlabs1', 'x')) == [
        ('telegram', 'https://t.me/solonpad')]


def test_a_repository_keeps_its_owner_and_its_name():
    """A link into a file inside a repo is not the repo, and the reader fetches what it is given."""
    body = '<a href="https://github.com/solonlend/solonpad-skill/blob/main/README.md">r</a>'
    assert candidates(_page(body)) == [('github', 'https://github.com/solonlend/solonpad-skill')]


def test_a_listing_and_a_shortener_are_never_channels():
    """The places a token is listed are who reads about it, not where it lives."""
    body = ('<a href="https://gmgn.ai/arc/token/0xabc">gmgn</a>'
            '<a href="https://t.co/HfzI0EGdfH">x</a>'
            '<a href="https://dexscreener.com/arc/0xabc">ds</a>')
    assert candidates(_page(body)) == []


def test_a_website_is_not_harvested():
    """The card prints one site and it is the one that was proved, not one a proved page named."""
    body = '<a href="https://partner.example/">p</a>'
    assert candidates(_page(body)) == []


def test_the_caps_are_what_the_page_does_not_get_to_spend():
    """A partner wall is longer than a project's list of its own channels."""
    body = ''.join('<a href="https://x.com/handle%d">h</a>' % i for i in range(6))
    body += ''.join('<a href="https://github.com/org/repo%d">r</a>' % i for i in range(4))
    body += ''.join('<a href="https://t.me/chan%d">t</a>' % i for i in range(3))
    found = candidates(_page(body))
    assert len([kind for kind, _ in found if kind == 'twitter']) == HARVEST_ACCOUNTS
    assert len([kind for kind, _ in found if kind == 'github']) == 2
    assert len([kind for kind, _ in found if kind == 'telegram']) == 1


def test_a_page_does_not_harvest_itself():
    """A site linking its own host is not a second site, and a profile is not its own account."""
    assert candidates(_page('<a href="https://solonpad.fun/">home</a>')) == []
