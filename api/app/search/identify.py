"""Which of the leads is the project itself.

A model's prose is not evidence. Every tier here is earned by reading a page, and there are
two ways to earn the top one.

A page that carries the contract address in its own description is the project describing
itself. On X that description is the bio, which is the only part of a profile readable
without logging in and the only part the account's owner writes for the page rather than
for a post. A launch post that merely quotes the address is what the airdrop accounts do,
so it is the weaker tier, not this one.

The other way is that the two ends of a project agree: a site whose own description names
the token and an account that links to that site are one project. Neither half is enough
alone - a page claiming a ticker proves nothing about who runs it, and a link proves nothing
about what the site is - but a stranger's page and a stranger's account do not produce that
pair by accident.

A contract address is 40 hex characters that cannot be paraphrased, spelled around, or
confused with another token, and that is what makes reading cheaper than trusting. A false
positive here is worse than a miss, because it puts a stranger's account on the card as the
project's own.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..sources import browser, http
from .base import SearchHit, handle_of, host_of

#: The three answers this layer can give about one lead. own is the project speaking about
#: itself; carries is the address sitting on a page that somebody else, or a post, put it
#: on; none is the lead being wrong.
OWN = 'own'
CARRIES = 'carries'
NONE = 'none'

#: Highest rank first, so the caller reads the answer in the order it should trust it.
RANK = {OWN: 0, CARRIES: 1, NONE: 2}

X_PROFILE = 'https://x.com/%s'

#: What the head of an X profile says about the account. og:description is the bio; the
#: plain description is the same string for the crawlers that do not read Open Graph.
_META_TAG = re.compile(r'<meta\b[^>]*>', re.I)
_META_NAME = re.compile(
    r'(?:property|name)\s*=\s*"(og:description|description|twitter:description)"', re.I)
_CONTENT_ATTR = re.compile(r'content\s*=\s*"([^"]*)"', re.I)

#: An x.com or twitter.com link to an account, anywhere on somebody else's page.
X_LINK = re.compile(r'(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})', re.I)

#: Hosts that answer about a token without being the project. They carry the address
#: because listing it is their job, so they are never the website the card should link to.
AGGREGATORS = (
    # Token pages: they carry every address because listing them is the product.
    'gmgn.ai', 'dexscreener.com', 'debot.ai', 'geckoterminal.com', 'coingecko.com',
    'coinmarketcap.com', 'birdeye.so', 'dextools.io', 'defined.fi', 'photonsol.ai',
    # Explorers, screeners and registries.
    'blockscout.com', 'sourcify.dev', 'gopluslabs.io', 'radardex.pro', 'tolly.ai',
    'solscan.io', 'bscscan.com', 'etherscan.io', 'ethplorer.io', 'arcscreener.live',
    # Exchanges and market data: they list a token because listing is their job. One run
    # spent twelve seconds each reading lbank.com and nomics.com for a token neither had
    # ever heard of, and pump.fun and arctools.fun for two more.
    'lbank.com', 'nomics.com', 'defillama.com', 'coinhall.org', 'binance.com', 'okx.com',
    'gate.io', 'mexc.com', 'bitget.com', 'kucoin.com', 'htx.com',
    # Another chain's launchpad, tool directories, and link hubs.
    'pump.fun', 'arctools.fun', 'linktr.ee', 'beacons.ai',
    # The chain itself. explorer.arc.io is the Arc explorer and arc.io is Arc's own site,
    # and neither will ever be the project - on one run both were offered as one. Robinhood
    # is here for the same reason on its own chain: the broker's site names the chain and
    # hosts none of its tokens.
    'arc.io', 'circle.com', 'robinhood.com',
    # Platforms and search engines, which are where a project is talked about.
    'x.com', 'twitter.com', 'github.com', 't.me', 'discord.gg', 'discord.com',
    'uniswap.org', 'grok.com', 'google.com', 't.co',
)


@dataclass(frozen=True)
class Profile:
    """One candidate X account, after its own page was read."""

    handle: str
    url: str
    tier: str
    bio: str = ''
    evidence: str = ''

    @property
    def official(self) -> bool:
        return self.tier == OWN


@dataclass(frozen=True)
class Site:
    """One candidate website, after its own page was read."""

    url: str
    tier: str
    evidence: str = ''

    @property
    def confirmed(self) -> bool:
        return self.tier != NONE


def description_of(page: str) -> str:
    """What a page says about itself in its head, entity-decoded.

    This is the only part of an X profile we can read without logging in, and the only part
    the account's owner writes for the page rather than for a post.
    """
    for tag in _META_TAG.finditer(page or ''):
        raw = tag.group(0)
        if not _META_NAME.search(raw):
            continue
        content = _CONTENT_ATTR.search(raw)
        if content:
            return html.unescape(content.group(1)).strip()
    return ''


def mentions(text: str, ca: str) -> bool:
    """Whether a page carries this address, ignoring case, and accepting it without 0x.

    X answers with whatever case the poster typed, and the same address turns up both as
    0xAbC... and as its bare hex. Comparing the folded forms is what stops a real match
    from looking like a miss.
    """
    needle = (ca or '').strip().lower()
    if not text or not needle:
        return False
    folded = text.lower()
    if needle in folded:
        return True
    bare = needle[2:] if needle.startswith('0x') else needle
    return len(bare) == 40 and bare in folded


def classify(page: str, ca: str) -> tuple[str, str]:
    """(tier, bio) for one fetched profile page.

    The bio decides, and the rest of the page only gets the account a weaker label. That
    ordering is the whole rule: the bio is what its owner wrote about the account, while a
    post that quotes the address may be about somebody else's token entirely.
    """
    bio = description_of(page)
    if mentions(bio, ca):
        return OWN, bio
    if mentions(page, ca):
        return CARRIES, bio
    return NONE, bio


def names_token(text: str, symbol: str) -> bool:
    """Whether a page calls this token by name, in the form a project writes on its own site.

    Only the dollar form counts. A bare ticker is three or four letters that turn up inside
    ordinary words, and one live run offered a Japanese blog post and a company registry as
    a token's homepage because of a name word; a project writing about itself writes $TICKER.

    Three characters is the floor: two-letter symbols are rare and the ones that exist are
    usually two letters that mean something else as well. A site for such a token is still
    confirmed the primary way - by linking to its account.
    """
    ticker = (symbol or '').strip()
    if len(ticker) < 3 or not ticker.isalnum() or not text:
        return False
    return re.search(r'\$' + re.escape(ticker) + r'(?![A-Za-z0-9])', text, re.I) is not None


def read_profiles(hits: Sequence[SearchHit], ca: str, *,
                  fetch: Callable[[str], str] | None = None,
                  limit: int = 8) -> tuple[list[Profile], dict[str, str]]:
    """Read every candidate account the hits mention, best answer first.

    The pages come back with the answers because a profile is read once and used twice: for
    the tier it earns here, and for the links on it, which is what lets a site and an
    account confirm each other. Fetching them again to compare would double the traffic of
    the most expensive step in the search.

    The limit exists because a search for an address returns a crowd - on one run six
    candidates, five of them strangers - and every candidate costs one page. The order of
    the hits is the model's own order, a hint about which it thought mattered, and using it
    as the tie-break is free.
    """
    fetch = fetch or fetch_html
    handles: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        handle = handle_of(hit.url)
        if not handle or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        handles.append(handle)
        if len(handles) >= limit:
            break

    profiles: list[Profile] = []
    pages: dict[str, str] = {}
    for handle in handles:
        url = X_PROFILE % handle
        try:
            page = fetch(url)
        except Exception as exc:
            profiles.append(Profile(handle, url, NONE, '', 'could not be read: %s' % _why(exc)))
            continue
        pages[url] = page
        tier, bio = classify(page, ca)
        profiles.append(Profile(handle, url, tier, bio, _evidence(tier)))
    return sorted(profiles, key=lambda profile: RANK.get(profile.tier, 99)), pages


def identify(hits: Sequence[SearchHit], ca: str, *,
             fetch: Callable[[str], str] | None = None, limit: int = 8) -> list[Profile]:
    """The tiers alone, for a caller that does not need the pages behind them."""
    profiles, _ = read_profiles(hits, ca, fetch=fetch, limit=limit)
    return profiles


def promote_by_site(profiles: Sequence[Profile], pages: dict[str, str],
                    hosts: Iterable[str]) -> list[Profile]:
    """Give an account the tier of the site it links to.

    The other direction of the same agreement: the site said what it was, and the account
    points at it. An account that links to a page named after the token is that page's
    account, and nothing else on the page is consulted - the tier is already earned.
    """
    wanted = [host.lower() for host in hosts if host]
    if not wanted:
        return list(profiles)
    promoted: list[Profile] = []
    for profile in profiles:
        page = pages.get(profile.url, '').lower()
        match = next((host for host in wanted if host in page), '')
        if profile.tier == OWN or not match:
            promoted.append(profile)
            continue
        promoted.append(Profile(profile.handle, profile.url, OWN, profile.bio,
                                'links to %s' % match))
    return sorted(promoted, key=lambda profile: RANK.get(profile.tier, 99))


def check_website(url: str, ca: str, handles: Iterable[str],
                 *, fetch: Callable[[str], str] | None = None, symbol: str = '') -> Site:
    """Read one candidate site and say how well it ties back to the token.

    Three ties, strongest first. A page that links to an account we already verified is the
    project's own site, because two independent places agreeing on one handle is not
    something a stranger's page produces. A page whose own description names the token and
    which carries the address is the project describing itself. A page that only carries the
    address is a listing - which is what a marketplace is, and why those hosts never get here.
    """
    fetch = fetch or fetch_page
    try:
        page = fetch(url)
    except Exception as exc:
        return Site(url, NONE, 'could not be read: %s' % _why(exc))
    known = {handle.lower() for handle in handles if handle}
    for found in X_LINK.findall(page or ''):
        if found.lower() in known:
            return Site(url, OWN, 'links to %s' % found)
    if mentions(page, ca) and names_token(description_of(page), symbol):
        return Site(url, OWN, 'its own description calls the token $%s' % (symbol or '').strip())
    if mentions(page, ca):
        return Site(url, CARRIES, 'carries the contract address')
    return Site(url, NONE, 'neither the address nor the account appears on it')


def website_candidates(hits: Sequence[SearchHit], *, limit: int = 6,
                       extra: Sequence[str] = ()) -> list[str]:
    """The URLs that could be the project's own site, in the order they arrived.

    `extra` is the candidates a caller found on the project's own pages - see
    `harvest.site_candidates` - and they come first: a project writing its own address
    down is a better lead than a model's hit list. The two share the cap, so a footer's worth
    of partner links cannot spend every reading slot.
    """
    found: list[str] = []
    for url in list(extra) + [hit.url for hit in hits if hit.kind == 'web']:
        if not url or _is_aggregator(url) or url in found:
            continue
        found.append(url)
        if len(found) >= limit:
            break
    return found


def check_sites(hits: Sequence[SearchHit], ca: str, handles: Iterable[str],
                *, fetch: Callable[[str], str] | None = None,
                symbol: str = '', extra: Sequence[str] = ()) -> list[Site]:
    """Read every candidate site, in the order the search offered them.

    All of them, including the ones that turn out to be nothing: a round that silently read
    six domains and kept one has no way to say what it ruled out, and the ruled-out list is
    what makes a wrong answer visible next time. `extra` is passed straight to
    `website_candidates` and is read the same way as anything else.
    """
    handles = list(handles)
    return [check_website(url, ca, handles, fetch=fetch, symbol=symbol)
            for url in website_candidates(hits, extra=extra)]


def pick_website(hits: Sequence[SearchHit], ca: str, handles: Iterable[str],
                 *, fetch: Callable[[str], str] | None = None,
                 symbol: str = '', extra: Sequence[str] = ()) -> Site | None:
    """The best candidate site, preferring one that ties back to the token.

    An account we verified is the strongest thing a page can point at, so a site that links
    to one wins outright. Carrying the address is the weaker tie and is taken only when
    nothing better showed up.
    """
    best: Site | None = None
    for site in check_sites(hits, ca, handles, fetch=fetch, symbol=symbol, extra=extra):
        if site.tier == OWN:
            return site
        if site.tier == CARRIES and best is None:
            best = site
    return best


def fetch_html(url: str) -> str:
    """One page, as HTML, with the browser lane as the second attempt.

    X serves a profile to a plain client, which is what makes the bio rule cheap. When it
    does not - a challenge page, a redirect chain - the browser lane is the same request
    with the fingerprints of something that renders, and it is worth one try.

    Neither lane runs JavaScript. For a candidate site that is the whole problem - see
    fetch_page - and for a profile it is the point: the bio is in the document.
    """
    try:
        return http.get(url, http.UA_HTML, transport='http', tries=2, timeout=25)
    except http.SourceError:
        return http.get(url, http.UA_HTML, transport='browser', tries=1, timeout=30)


#: Hosts a browser cannot read any better than a plain client. X serves a profile's bio to a
#: plain client and a login wall to a browser nobody signed in to, so drawing one costs five
#: seconds to come back with less than the document already had. Telegram's preview page is
#: the other way round: it already has everything in it, and drawing it measured 27 words
#: against the same 27 in 7.5 seconds.
NO_RENDER = ('x.com', 'twitter.com', 't.me')


#: Below this many words a document has not been drawn yet. Measured 2026-09-27 over the
#: project sites this pipeline had already read: three arrived at eight, eight and ten words,
#: and the next one along arrived with ninety-four of its own. The gap is the whole point,
#: and it is why one number is enough here.
SHELL_WORDS = 40


def visible_words(page: str) -> int:
    """How many words a reader would see, ignoring the markup and the code."""
    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', page or '', flags=re.S | re.I)
    return len(re.sub(r'<[^>]+>', ' ', body).split())


def is_shell(page: str) -> bool:
    """Whether the document arrived with nothing drawn in it yet."""
    return visible_words(page) < SHELL_WORDS


def _drawable(url: str) -> bool:
    """Whether a browser could get more out of this URL than the plain lanes did."""
    host = host_of(url)
    return not any(host == blocked or host.endswith('.' + blocked) for blocked in NO_RENDER)


def fetch_page(url: str) -> str:
    """One page: the plain lanes first, a browser if what came back was not drawn yet.

    A site whose document holds no text is not a site with nothing to say; it is a site whose
    text is written by a framework after the document loads, and reading the markup instead
    of the page is what makes a stranger's site look like the project's. On 2026-09-27 the
    Arc launchpad's own site was outranked by a stranger's page for exactly this reason.

    The plain lanes go first because they cost a fraction of a second and answer for most
    sites. The browser is four to six seconds, so it only runs once the page has already
    proved empty - or refused both plain lanes, which is the other way a document arrives
    with nothing in it. A browser that cannot answer leaves whatever the plain lane had.

    Accounts are the exception, and NO_RENDER is where that is written down: a browser can
    only read an X profile it is signed in to, and the bio this pass needs is in the
    document X serves to anybody.
    """
    try:
        page = fetch_html(url)
    except http.SourceError:
        drawn = browser.render(url) if _drawable(url) else ''
        if not drawn:
            raise
        return drawn
    if not is_shell(page) or not _drawable(url):
        return page
    drawn = browser.render(url)
    # More text than the markup had, or the browser was not worth the four seconds. A drawn
    # page is compared rather than measured against the same threshold: a page whose whole
    # body is a contract address is a real page with six words on it, and the plain document
    # that came before it drew nothing at all.
    return drawn if visible_words(drawn) > visible_words(page) else page


def _is_aggregator(url: str) -> bool:
    host = host_of(url)
    return any(host == blocked or host.endswith('.' + blocked) for blocked in AGGREGATORS)


def _evidence(tier: str) -> str:
    if tier == OWN:
        return 'the bio carries the contract address'
    if tier == CARRIES:
        return 'the address appears on the page, but not in the bio'
    return 'the address does not appear on the page'


def _why(exc: Exception) -> str:
    return str(exc)[:120]
