"""A contract address in, the project's own links out.

This is the step the data sources cannot do. On Arc a new token arrives from the pool
listings with no socials at all, and X already has the launch thread - so the search goes
looking for the address, and what comes back is candidates. Turning candidates into the
project's own account is identify's job, and it does it by reading pages rather than by
trusting the answer that produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .base import SearchHit, TweetRow, handle_of, host_of, kind_of, normalize_url
from . import harvest
from .identify import (CARRIES, NONE, OWN, Profile, Site, check_sites, check_website,
                       fetch_html, fetch_page, promote_by_site, read_profiles)
from .service import SearchResult, SearchService

#: How many of a search's own hits are read as possible channels. The rest are sites, and
#: `check_sites` reads six of those; a tweet that names three repositories should not spend
#: the round on all three.
CHANNEL_CANDIDATES = 3

#: The hit kinds that can be a channel rather than a site. An account is not here: one a
#: search turned up is already read by `read_profiles`, with more room and a better order.
CHANNEL_HITS = ('github', 'telegram')


@dataclass(frozen=True)
class Discovery:
    """What one search turned up about one token, and how much of it held up."""

    chain: str
    ca: str
    symbol: str = ''
    query: str = ''
    hits: list[SearchHit] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    site: Site | None = None
    sites: list[Site] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: The channels the project's own pages pointed at, kept only once they survived being
    #: read. They are offered after the verified pair and before the round's own links:
    #: proved, but proved about a link whose whole claim on the project was where it was
    #: written down.
    harvested: list[tuple[str, str]] = field(default_factory=list)
    #: The account whose own timeline the round read, and the one whose posts it therefore
    #: counts as the project's own when it counts the discussion around it.
    timeline_handle: str = ''
    #: The posts the search read on its way to the leads. Not leads - a tweet is not a place
    #: a project lives - and the only record of who was talking about the token.
    tweets: list[TweetRow] = field(default_factory=list)
    #: The proved account's own recent posts, newest first. Empty when no account was proved
    #: or the client would not answer, which is a card without a timeline and not a failure.
    timeline: list[TweetRow] = field(default_factory=list)

    @property
    def official_handles(self) -> list[str]:
        return [profile.handle for profile in self.profiles if profile.official]

    @property
    def official_profile(self) -> Profile | None:
        for profile in self.profiles:
            if profile.official:
                return profile
        return None

    @property
    def website(self) -> str | None:
        """The project's site, only when the read confirmed it.

        An unconfirmed candidate is not returned: the card links to a site as the project's
        own, and pointing at a stranger's page is the same mistake as naming the wrong
        account.
        """
        return self.site.url if self.site is not None and self.site.confirmed else None

    @property
    def found(self) -> bool:
        return bool(self.official_handles or self.website)

    @property
    def mine(self) -> list[str]:
        """Every handle this round counts as the project speaking.

        The proved accounts, plus the one whose timeline was read when that reading was
        decided by the round's own record rather than by a page - both are the project as
        far as the grammar of a card is concerned, and a post of the project's own counted
        as a stranger's would inflate the only number the round owns about its audience.
        """
        found = [profile.handle for profile in self.profiles if profile.official]
        if self.timeline_handle and self.timeline_handle not in found:
            found.append(self.timeline_handle)
        return found

    @property
    def official_links(self) -> list[tuple[str, str]]:
        """The verified pages as (kind, url), best evidence first.

        The kind is the one the reading stage files a page under, so a confirmed account is
        stored as an X page and a confirmed site as a website, whichever order they were
        confirmed in.
        """
        found = [('twitter', profile.url) for profile in self.profiles if profile.official]
        if self.website:
            found.append(('website', self.website))
        return found

    @property
    def links(self) -> list[str]:
        """What the reading stage should fetch next, best evidence first."""
        return [url for _, url in self.official_links]

    @property
    def checked(self) -> list[tuple[str, str]]:
        """Every candidate with the tier it earned - the record of what was ruled out."""
        reviewed = [(profile.url, profile.tier) for profile in self.profiles]
        reviewed.extend((site.url, site.tier) for site in self.sites)
        return reviewed


def token_pages(profiles: Sequence[Profile], pages: dict[str, str], sites: Sequence[Site],
                fetch: Callable[[str], str]) -> list[tuple[str, str, str]]:
    """The pages this round read that it knows are about this token, as (kind, url, body).

    Both site tiers, and not only `own`: what these pages feed is a list of candidates, and
    a candidate is read before it is kept, so a wrong page costs one fetch and a page left
    out costs a channel nobody opens. A marketplace is never among them - it is refused a
    site before it is read - which is what keeps a listing's rows out of this.

    A site is fetched again through the same memorised fetch its tier came from, so the
    second read costs no traffic, and it comes first because a footer is where a project
    writes its channels down. An account is taken only when it is `own`: an account that
    merely quotes the address is a stranger with the address, and a stranger's page is not
    this token's index. Its body is the one that earned its bio.
    """
    out: list[tuple[str, str, str]] = []
    for site in sites:
        if site.tier == NONE:
            continue
        try:
            body = fetch(site.url)
        except Exception:
            continue
        out.append(('web', site.url, body))
    for profile in profiles:
        if profile.tier != OWN:
            continue
        body = pages.get(profile.url)
        if body:
            out.append(('x', profile.url, body))
    return out


def channel_hits(hits: Sequence[SearchHit], *, limit: int = CHANNEL_CANDIDATES) -> list[str]:
    """The hit URLs a search offered that could be a channel rather than a site."""
    found: list[str] = []
    for hit in hits:
        if hit.kind not in CHANNEL_HITS or harvest.listed(host_of(hit.url)):
            continue
        if hit.url in found:
            continue
        found.append(hit.url)
        if len(found) >= limit:
            break
    return found


def named_by_source(profiles: Sequence[Profile],
                    known: Sequence[tuple[str, str]]) -> list[str]:
    """The account the round's own metadata names, when the search named it as well.

    Every tier here is earned by reading a page, and one rule asks a page to carry the
    address. A project whose bio is its own name and its own site - which is most of them -
    never does: on 2026-09-28 the Arc launchpad was proved by its site, its account by
    nothing, and its timeline went unread because of it.

    This is the narrow rule that reaches that case. The round's own record of this token
    names an account; an independent search offered the same account for the same token; and
    two sources agreeing on one handle is the evidence a site and an account agree by. It
    decides which timeline to read and nothing else - no tier moves, and an account that
    merely quoted the address is still not the project's.
    """
    named = {(handle_of(url) or '').lower() for kind, url in known
             if str(kind).lower() in ('twitter', 'x')}
    named.discard('')
    if not named:
        return []
    return [profile.handle for profile in profiles if profile.handle.lower() in named]


def prove_channels(candidates: Sequence[tuple[str, str]], ca: str, handles: Sequence[str],
                   *, profile_fetch: Callable[[str], str], site_fetch: Callable[[str], str],
                   symbol: str, hosts: Sequence[str] = ()) -> list[tuple[str, str]]:
    """The candidates that survived being read, as (kind, url).

    An account is read for the address exactly as every other account was, and a repository
    or a channel is read as a site was. `hosts` is the mutual confirmation pointed the other
    way: an account that links to the site this round already proved is that site's account,
    which is the same two-ends-agree rule the site slot is decided by.
    """
    out: list[tuple[str, str]] = []
    for kind, url in candidates:
        if kind == 'twitter':
            continue
        site = check_website(url, ca, handles, fetch=site_fetch, symbol=symbol)
        if site.tier == OWN:
            out.append((kind, url))
    accounts = [url for kind, url in candidates if kind == 'twitter']
    if accounts:
        reads = [SearchHit(url=url, engine='harvest', kind='x') for url in accounts]
        profiles, pages = read_profiles(reads, ca, fetch=profile_fetch)
        if hosts:
            profiles = promote_by_site(profiles, pages, hosts)
        out.extend(('twitter', profile.url) for profile in profiles
                   if profile.tier != NONE)
    return out


def build_query(chain: str, ca: str, symbol: str = '') -> str:
    """The question put to the engines.

    Written as a lookup with the address quoted, because the address is the one string in
    it that cannot be paraphrased. The ticker is a hint, not a key: two tokens on one chain
    can share one, and only the address decides.
    """
    parts = ['Find the official links of the crypto token on the %s chain with contract '
             'address %s' % (chain, ca)]
    if symbol:
        parts.append('(ticker %s)' % symbol)
    parts.append('Search X for the address first, then confirm the account and the site.')
    return ' '.join(parts)


def memo(fetch: Callable[[str], str]) -> Callable[[str], str]:
    """One fetch per URL per discovery.

    A site is read twice - once before any account is confirmed and once after - because the
    two confirmations point opposite ways. Caching the bodies is what makes the second read
    free; caching the failures keeps a dead domain from being retried at the slowest point
    in the round.
    """
    seen: dict[str, str | Exception] = {}

    def cached(url: str) -> str:
        if url not in seen:
            try:
                seen[url] = fetch(url)
            except Exception as exc:
                seen[url] = exc
        value = seen[url]
        if isinstance(value, Exception):
            raise value
        return value

    return cached


#: What the round's own link kinds mean here. A docs page on the project's own domain is a
#: page to read, not the site to link, so it keeps a kind of its own rather than being
#: treated as the homepage the card would point at.
SOURCE_KINDS = {'twitter': 'x', 'telegram': 'telegram', 'github': 'github',
                'docs': 'docs', 'website': 'web'}


def known_hits(known: Sequence[tuple[str, str]]) -> list[SearchHit]:
    """Links the round already had, as candidates for the same verification.

    A launchpad's record of a token's X handle is a candidate exactly like a search hit, and
    running both through one read is what turns "a source said so" into "the bio carries the
    address". Nothing is trusted on arrival: the point is to confirm it, not to copy it.
    """
    found: list[SearchHit] = []
    seen: set[str] = set()
    for kind, url in known:
        clean = normalize_url(str(url or ''))
        if not clean or clean in seen:
            continue
        seen.add(clean)
        found.append(SearchHit(url=clean, engine='source',
                               kind=SOURCE_KINDS.get(str(kind or '').lower(), kind_of(clean))))
    return found


def best_site(sites: Sequence[Site]) -> Site | None:
    """The one site worth linking, out of everything that was read."""
    for site in sites:
        if site.tier == OWN:
            return site
    for site in sites:
        if site.tier == CARRIES:
            return site
    return None


def discover(chain: str, ca: str, symbol: str = '', *, service: SearchService | None = None,
             fetch: Callable[[str], str] | None = None, limit: int = 25,
             profiles: int = 8, known: Sequence[tuple[str, str]] = (),
             search: bool = True) -> Discovery:
    """Search for one token's own links and say which of them survived a read.

    With `search=False` this still reads and verifies the round's own links and skips the
    engine entirely: a caller that already has something to read pays nothing to find out
    what the links are worth.

    The pool is the round's own links plus whatever the search found, and both are read the
    same way: a launchpad's record of a token's X handle earns its tier the same way a hit
    does, and either can be the one that confirms the site. Combining them is the point -
    the sources know about tokens a search cannot find, and a search finds the ones the
    sources never listed.

    Two passes over the sites, because the mutual confirmations point opposite ways. The
    first reads them knowing no account, which is where a site whose own description names
    the token earns its tier. The accounts are read next, and the second pass then reads the
    same sites knowing which accounts came back confirmed, which is where a site that links
    to one earns the same tier. Only the first pass costs traffic.
    """
    service = service or SearchService()
    # Two readers, because the two kinds of page ask different things of a fetch: an
    # account's bio is in the document it serves, and a candidate site may not be drawn
    # until a browser runs. Both are memoised, so the second pass over the same sites - the
    # one that knows which accounts came back confirmed - costs no traffic at all.
    read = memo(fetch or fetch_html)
    site = memo(fetch or fetch_page)
    query = build_query(chain, ca, symbol)
    # Reading the round's own links is free and always worth doing; asking an engine is not.
    # `search=False` is how a caller that has enough to read says so, and it skips the one
    # request in this layer that costs real money.
    result = service.search(query, limit=limit) if search else SearchResult(query=query)
    # The round's own links come first: they are already known to be about this token, so a
    # candidate that arrived that way is read before one a model offered.
    hits = known_hits(known) + list(result.hits)

    sites = check_sites(hits, ca, [], fetch=site, symbol=symbol)
    confirmed = [host_of(site.url) for site in sites if site.tier == OWN]

    found, pages = read_profiles(hits, ca, fetch=read, limit=profiles)
    if confirmed:
        found = promote_by_site(found, pages, confirmed)

    handles = [profile.handle for profile in found if profile.tier != NONE]
    if handles:
        sites = check_sites(hits, ca, handles, fetch=site, symbol=symbol)
    # The second half of step one: the channels the project's own pages point at, and the
    # ones a search offered as a channel rather than a site. Both are candidates, and both
    # are read before they are believed - see `harvest` for why inheritance stops there.
    picked = list(harvest.candidates(token_pages(found, pages, sites, site), symbol))
    picked.extend((kind_of(url), url) for url in channel_hits(hits))
    proven = prove_channels(picked, ca, handles, profile_fetch=read, site_fetch=site,
                            symbol=symbol,
                            hosts=[host_of(row.url) for row in sites if row.tier == OWN])
    # The proved account, asked what it has been saying. One account and not two: twenty
    # posts is what the round reads for "is anyone still there", and a second account is
    # another twenty about a different face of the same project.
    official = [profile.handle for profile in found if profile.official]
    if not official:
        official = named_by_source(found, known)
    handle = official[0] if official else ''
    posts = service.timeline(handle) if (search and handle) else []
    return Discovery(chain=chain, ca=ca, symbol=symbol, query=query, hits=hits,
                     profiles=found, site=best_site(sites), sites=sites,
                     queries=result.queries, errors=result.errors,
                     harvested=proven, tweets=list(result.tweets), timeline=posts,
                     timeline_handle=handle)
