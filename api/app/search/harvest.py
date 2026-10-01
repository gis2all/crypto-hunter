"""The channels a proved page points at, taken as candidates.

Step one ends with a page the round has proved is the project's own: the address is on it.
That page is the best index of the project's channels there is - a footer names the account
and the repository, a bio names the channel - and reading it and then throwing those links
away is how a round knew a project had a GitHub without ever opening it.

What is taken here is a candidate, never a tier. A page being about this token says what
the page is, not what the links on it are, and the difference is measured rather than
theoretical: on 2026-09-28 the Arc launchpad's own home page carried a live market table,
and its markup offered x.com/Apple, a cashtag search and a stock's home page alongside its
own links. Every one of them would have been inherited as the project's own account. So a
candidate found here is proved the same way every other candidate is - by reading it - and
the page decides what to look at, never what to believe.

`candidates` offers accounts, channels and repositories and never a site: the footer of
a project's own site is a wall of partner links, the card prints exactly one site, and the
one it prints is the site that was proved rather than one a proved page mentioned.
`site_candidates` is the same reading pointed the other way. A project whose own X
profile names its site is a project an engine may never have indexed, and the only place that
site is written down is the page that was read for its bio - so those links are offered as
site candidates, and every one of them is read and has to earn its tier like any other.
"""

from __future__ import annotations

import html
import re
from typing import Sequence
from urllib.parse import urlsplit

from .base import handle_of, host_of, kind_of, normalize_url
from .engines.xsearch import affinity
from .identify import AGGREGATORS

#: How many candidates one token may draw from its own pages. The reading list is capped as
#: well, and a footer is longer than a project's list of channels, so this is the tighter
#: of the two caps and the one that decides what the model actually sees.
HARVEST_LIMIT = 6
#: A project has one channel and one repository; it may reasonably have two accounts. The
#: per-kind caps are what keep a market table's worth of tickers from spending the page reads
#: that the project's own account needs.
HARVEST_ACCOUNTS = 2
HARVEST_REPOS = 2

#: How many candidate sites one token's own pages may offer. The same tighter-of-two rule as
#: HARVEST_LIMIT: a project has one home page, and a footer's worth of partner links must not
#: spend the page reads a round has.
SITE_CANDIDATES = 6

#: Which kinds are worth taking, and what they are called downstream. `kind_of` answers with
#: the family a URL belongs to; the reading stage files pages under these names.
CHANNEL_KINDS = {'x': 'twitter', 'telegram': 'telegram', 'github': 'github'}

#: The platforms a project may legitimately live on. They are in `AGGREGATORS` because they
#: are never a token's own *site* - which is the opposite of the question asked here.
PLATFORMS = ('x.com', 'twitter.com', 'github.com', 't.me', 'discord.gg', 'discord.com')

#: Everything else in that list: the places a token is listed because listing is somebody
#: else's business. A link to one of them on a project's own page is a fact about the
#: listing, and following it would spend a reading slot on a screener.
NOT_CHANNELS = tuple(host for host in AGGREGATORS if host not in PLATFORMS)

#: Links the page actually offers: an anchor, and a markdown link in prose that arrived as
#: text. Bare URLs are deliberately not read. A document is mostly code, and a bundle
#: carries the URL of everything it could ever draw - the same launchpad page that listed a
#: stock account also spelled out x.com/search and its own sitemap in the data beside the
#: markup, and neither is a channel a person wrote down.
_ANCHOR = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'>\s]+)["\']', re.I)
_MARKDOWN = re.compile(r'\]\((https?://[^)\s]+)\)')

#: The real target of a link, as the page's own data carries it. X is the one site that
#: rewrites every link it displays to a t.co shortener, so a profile's anchors yield the
#: shortener and nothing else; the expansion is kept in the data beside it, and a document
#: that shipped as JSON escapes the slashes.
_EXPANDED = re.compile(r'expanded_url"?\s*:\s*"([^"]+)"')


def _urls(body: str, *, shorteners: bool = False) -> list[str]:
    """Every link a page offers, in the order it offers them.

    `shorteners` is for the pages whose links arrive wrapped: X replaces the href of every
    link it displays, and only there is the expansion the link rather than a duplicate of it.
    """
    found = [html.unescape(match) for match in _ANCHOR.findall(body or '')]
    found.extend(_MARKDOWN.findall(body or ''))
    if shorteners:
        for match in _EXPANDED.findall(body or ''):
            found.append(match.replace('\\u002F', '/').replace('\\/', '/'))
    return found


def _canonical(url: str, kind: str) -> str | None:
    """One channel link, trimmed to the thing it names.

    A URL into a file inside a repository is not the repository, and the reading stage
    fetches whatever it is handed - so GitHub keeps the owner and the repository, a channel
    keeps its name, and an account keeps its handle. None of the three has a deeper form
    worth a reading slot.
    """
    clean = normalize_url(url)
    if not clean:
        return None
    if kind == 'x':
        handle = handle_of(clean)
        return 'https://x.com/%s' % handle if handle else None
    segments = [part for part in urlsplit(clean).path.split('/') if part]
    if kind == 'github':
        if len(segments) < 2:
            return None
        return 'https://github.com/%s/%s' % (segments[0], segments[1])
    if not segments:
        return None
    return 'https://t.me/%s' % segments[0]


def score(url: str, ticker: str) -> int:
    """How much a link looks like this token, for ordering only.

    The same four-tier guess `xsearch` orders accounts by, pointed at a URL: a repository
    called solonpad-skill belongs to a token called SOLON and so does an account called
    Solonlabs1, while a market table's row for Apple belongs to Apple. It decides which
    candidates are worth one of the few page reads, and nothing else - a link it scores
    zero for is still kept if it is reached first and it still has to be proved.
    """
    parts = [part for part in urlsplit(url).path.split('/') if part]
    handle = handle_of(url) or (parts[-1] if parts else '')
    org = parts[0] if len(parts) > 1 else ''
    return affinity(handle, org, ticker)


def listed(host: str) -> bool:
    """Whether a host is a place a token is listed rather than a place it lives.

    Public because the search layer asks the same question of its own hits: a repository named
    in a tweet is not a channel if the host is a screener.
    """
    return any(host == blocked or host.endswith('.' + blocked) for blocked in NOT_CHANNELS)


def candidates(pages: Sequence[tuple[str, str, str]],
               ticker: str = '') -> list[tuple[str, str]]:
    """The channels a token's own pages point at, as (kind, url), best kind first.

    `pages` is (kind, url, body) of the pages this round read and knows belong to this
    token, in the order they were read. Reading order is not the order they come back in:
    a page that is a market sorts its rows by volume and puts its own footer last, so the
    links are scored against the ticker before the caps are applied - otherwise one
    launchpad's table spends the two slots a round has for the project's own account.
    """
    seen: set[str] = set()
    accounts: list[tuple[str, str, int]] = []
    channels: list[tuple[str, str, int]] = []
    repos: list[tuple[str, str, int]] = []
    for kind, url, body in pages:
        own_host = host_of(url)
        for target in _urls(body, shorteners=(kind == 'x')):
            link_kind = kind_of(target)
            label = CHANNEL_KINDS.get(link_kind)
            if not label:
                continue
            host = host_of(target)
            if not host or host == own_host or listed(host):
                continue
            clean = _canonical(target, link_kind)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            row = (label, clean, score(clean, ticker))
            if label == 'twitter':
                accounts.append(row)
            elif label == 'github':
                repos.append(row)
            else:
                channels.append(row)
    # Sorted and stable: the score decides which candidates are read, and the page's own
    # order decides between two the score cannot tell apart.
    accounts.sort(key=lambda row: -row[2])
    repos.sort(key=lambda row: -row[2])
    ordered = ([(k, u) for k, u, _ in accounts[:HARVEST_ACCOUNTS]]
               + [(k, u) for k, u, _ in channels[:1]]
               + [(k, u) for k, u, _ in repos[:HARVEST_REPOS]])
    return ordered[:HARVEST_LIMIT]


def site_candidates(pages: Sequence[tuple[str, str, str]], ticker: str = '') -> list[str]:
    """The sites a token's own pages point at, one per host, best first.

    `pages` is (kind, url, body) of the pages a round has read and proved belong to this
    token - the same list `candidates` is handed, read the same way, the X profile the bio
    came from included.

    A candidate is not a verdict. Every one of these is read and has to tie back to the
    token, the same as a search hit, so a partner link in a footer costs one fetch and
    nothing else - see the module docstring. The host is the unit: two links into one site
    are one candidate, and the link that looks most like the ticker is the one kept.
    """
    best: dict[str, tuple[int, str]] = {}
    for kind, url, body in pages:
        own_host = host_of(url)
        for target in _urls(body, shorteners=(kind == 'x')):
            if kind_of(target) != 'web':
                continue
            host = host_of(target)
            if not host or host == own_host or listed(host):
                continue
            clean = normalize_url(target)
            if not clean:
                continue
            worth = score(clean, ticker)
            if host not in best or worth > best[host][0]:
                best[host] = (worth, clean)
    ordered = sorted(best.values(), key=lambda row: -row[0])
    return [clean for _, clean in ordered[:SITE_CANDIDATES]]
