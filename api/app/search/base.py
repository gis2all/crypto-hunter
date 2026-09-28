"""What a search returns, and how much of it is worth keeping.

A hit is a lead, not a fact: it says this URL came back for this query. Which lead is the
project's own account is decided in identify, by our rules, against pages we fetched -
never by the prose a model wrote around the links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: X paths that are navigation rather than an account. i belongs here because /i/user/123
#: is an identity page: a real account, but the URL carries no handle to show or compare.
X_RESERVED = frozenset((
    'i', 'home', 'search', 'explore', 'notifications', 'messages', 'settings',
    'intent', 'share', 'hashtag', 'login', 'signup', 'compose', 'about', 'tos', 'privacy',
    'following', 'followers', 'account', 'jobs',
))

#: Query parameters that identify the click rather than the page. s and t are X's own.
TRACKING = frozenset((
    's', 't', 'ref', 'ref_src', 'ref_url', 'src', 'fbclid', 'gclid', 'igshid',
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
))

#: What a sentence leaves stuck to the end of a URL it quotes.
TRAILING = '.,;:!?)]}\u3002\uff0c\uff1b\uff1a\u3001\uff09'

#: A link written as markdown, and a link written bare. Both appear in one answer.
#: A leading scheme, and a leading scheme-that-is-not-a-web-page.
_SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://')
_SCHEME_LIKE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*:')

_MD_LINK = re.compile(r'\[([^\]]*)\]\((https?://[^)\s]+)\)')
_BARE_LINK = re.compile(r'https?://[^\s<>"\')]+')


@dataclass(frozen=True)
class SearchHit:
    """One URL a search engine returned, with whatever it said around the URL."""

    url: str
    engine: str
    kind: str
    title: str = ''
    snippet: str = ''

@dataclass(frozen=True)
class TweetRow:
    """One post, as the client returned it: what it says and how it landed.

    A search is asked for leads and answers with a crowd; these are the crowd. They are
    kept whole rather than reduced to a count as they arrive, because what a round can say
    about third-party discussion is decided later, by the caller that knows which of the
    handles turned out to be the project's own.
    """

    id: str
    handle: str
    name: str
    text: str
    created_at: str
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    views: int = 0


@dataclass(frozen=True)
class EngineAnswer:
    """What one engine returned for one query.

    The searches it ran travel with the leads, because an engine holds no state: a round
    asks about several tokens at once, and an engine that remembered its last query on
    itself would hand one token's search to another token's card.
    """

    hits: list[SearchHit]
    queries: list[str] = field(default_factory=list)
    #: What the search actually read, kept for the round's own statistics rather than for
    #: the reading order: the hits say where the project lives, these say who is talking.
    tweets: list[TweetRow] = field(default_factory=list)


@runtime_checkable
class Engine(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 20) -> EngineAnswer:
        """Candidate URLs for one query, or an empty answer when the engine finds none."""


def _split(url: str):
    """urlsplit, with a malformed string answered as "not a URL" instead of an exception.

    A bracketed markdown link that lost its label looks like an IPv6 host to urlsplit, and
    a search answer is full of half-eaten markup. Raising there would take down a round
    over one bad citation.
    """
    try:
        return urlsplit(url)
    except ValueError:
        return None


def normalize_url(raw: str) -> str | None:
    """One URL, cleaned enough that two mentions of it compare equal.

    Search answers arrive wrapped in markdown and prose, so the same address shows up bare,
    inside a link, and with a sentence's full stop stuck to its tail. Dropping the fragment
    and the click-tracking parameters is what makes the dedupe downstream work, and the
    host is lowercased because X answers with both cases for the same account.
    """
    if not raw:
        return None
    text = raw.strip().strip('<>"\'' + chr(96))
    text = text.rstrip(TRAILING)
    if not text:
        return None
    if not _SCHEME.match(text):
        if _SCHEME_LIKE.match(text):
            return None  # mailto:, tel:, javascript: - a scheme, but not a page to fetch
        text = 'https://' + text.lstrip('/')
    parts = _split(text)
    if parts is None or parts.scheme not in ('http', 'https'):
        return None
    host = (parts.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    if not host or '.' not in host:
        return None
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = '%s:%d' % (host, parts.port)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in TRACKING]
    return urlunsplit((parts.scheme, netloc, parts.path or '/', urlencode(query), ''))


def host_of(url: str) -> str:
    """The lowercase host of a URL without the leading www, or an empty string.

    The host is the part two mentions of one site always agree on, so it is what a page is
    compared by - both when deciding whether a link points at a site we already know and
    when deciding whether a hit is a marketplace rather than a project.
    """
    parts = _split(url)
    host = (parts.hostname or '').lower() if parts is not None else ''
    return host[4:] if host.startswith('www.') else host


def kind_of(url: str) -> str:
    """Which family a hit belongs to, since the four are read differently."""
    host = host_of(url)
    if host in ('x.com', 'twitter.com'):
        return 'x'
    if host in ('github.com',):
        return 'github'
    if host in ('t.me', 'telegram.me'):
        return 'telegram'
    if host in ('discord.gg', 'discord.com'):
        return 'discord'
    return 'web'


def handle_of(url: str) -> str | None:
    """The X handle of a profile URL, or None when the URL is not one.

    x.com/foo, x.com/foo/status/1 and x.com/foo/ are all account foo. x.com/i/user/1 is
    only an account - the numeric form carries no handle, and treating its first segment
    as one would invent a candidate called i.
    """
    if kind_of(url) != 'x':
        return None
    parts = _split(url)
    if parts is None:
        return None
    segments = [segment for segment in parts.path.split('/') if segment]
    if not segments:
        return None
    first = segments[0]
    if first.lower() in X_RESERVED:
        return None
    if len(first) > 15 or not re.fullmatch(r'[A-Za-z0-9_]+', first):
        return None
    return first


def links_in(text: str) -> list[tuple[str, str]]:
    """Every URL in one piece of prose, as (label, url), markdown links first.

    The label of a markdown link is worth keeping: the model wrote the account's name as
    the label and the profile URL as the target, and that is a stronger lead than a bare
    address that happens to appear in a sentence.
    """
    found: list[tuple[str, str]] = []
    remaining = text or ''
    for match in _MD_LINK.finditer(remaining):
        found.append((match.group(1).strip(), match.group(2)))
    remaining = _MD_LINK.sub(' ', remaining)
    for match in _BARE_LINK.finditer(remaining):
        found.append(('', match.group(0)))
    return found
