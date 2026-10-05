"""X, asked through the advanced search the site's own box uses.

The engine, and the cheap one: a search is a subprocess and no model tokens, which is what
makes asking about every candidate affordable instead of a decision to be rationed. An
earlier build asked a search model over the same ground and paid about a hundred and fifty
thousand input tokens a call for it; this spends none.

Two questions per token, because one is not enough. The address is the one string that
cannot be paraphrased and it finds whoever pinned it; the ticker finds the account that
never wrote the address down. Measured on the last Arc round, the ticker was the only thing
that surfaced some official accounts at all, while the address alone found others.

What comes back is ordered, never judged. Which account is the project's is decided in
identify, by reading its page and finding the address there. Ordering decides something
smaller and still decisive: `read_profiles` reads eight accounts in the order given, and on
a busy token the eight are otherwise spent on strangers before the official one is reached.
An account whose handle or name looks like the ticker earns that place, and earns nothing
else - a token called PI has dozens of them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

from ..base import EngineAnswer, SearchHit, TweetRow, kind_of, normalize_url

#: The question carries both strings this engine needs, in the shape `discover.build_query`
#: writes them: the address quoted, the ticker in parentheses.
ADDRESS = re.compile(r'0x[a-fA-F0-9]{40}')
TICKER = re.compile(r'\(ticker ([^)]*)\)')

#: The search client that already carries a logged-in session. Named rather than vendored:
#: the advanced-search assembly, the pacing and the retry are the part that took work, and
#: they live in that project.
DEFAULT_CLI = 'twitter'
#: How many tweets one query asks for, and how long it may take. The client paces itself and
#: retries a 429 three times; the timeout is the backstop for the retry that never returns.
SEARCH_SIZE = 20
QUERY_TIMEOUT = 120.0
#: What the 测试连接 button is allowed to spend (系统设置). A person is waiting on this one, so it
#: takes the round's 120s backstop down to something a button can fail at.
PROBE_TIMEOUT = 30.0
#: What that button asks: the account the cookies claim to be. The cheapest real read the client
#: has - one profile, no query, nothing spent against the search budget a round needs - and the
#: one answer that tells a session apart from two strings that look like one.
PROBE_ARGV = ('whoami', '--json')
#: How many of an account's own posts a round reads. Twenty is one page of the client's
#: own output and roughly two weeks of a project that posts daily, which is what "is
#: anyone still here" needs: the text of each post plus the engagement beside it.
TWEET_LIMIT = 20
#: How many accounts are offered for reading. Cut just above the eight `read_profiles` reads,
#: so a couple of strangers do not cost the round the account that matters.
PROFILE_HITS = 12


def root_of(url: str) -> str:
    """A link trimmed to the site that hosts it.

    A tweet links the page it is about, not the home page, and a check run against
    /stake/3 says nothing about the site that hosts it - on one run three deep links to
    one launchpad were each read and ruled out while the home page was never tried.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    return '%s://%s/' % (parts.scheme or 'https', parts.hostname)


def squash(text: str) -> str:
    """Letters and digits only, lowercased. Handles write MEMES_PAD and tickers MEMESPAD."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def affinity(handle: str, name: str, ticker: str) -> int:
    """How much an account looks like the token, for ordering only.

    Four tiers, and the top one is a handle equal to the ticker. The bottom one is a
    four-letter prefix, which is a guess and is treated as one: it only moves an account up
    the reading order, and the account still has to carry the address on its page to count.
    """
    want, handle_key, name_key = squash(ticker), squash(handle), squash(name)
    if not want:
        return 0
    if handle_key == want:
        return 4
    if want in handle_key:
        return 3
    if len(want) >= 3 and want in name_key:
        return 2
    if len(want) >= 4 and want[:4] in handle_key:
        return 1
    return 0


def _int(value: Any) -> int:
    """A count from the client, or zero. A field the client did not send is not a crash."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def rows_of(tweets: Sequence[dict]) -> list[TweetRow]:
    """The client's rows as `TweetRow`, retweets skipped.

    A retweet's engagement belongs to whoever wrote it, so counting one as this account's
    own post would credit a project with a stranger's audience - the same reason the
    reading order skips them. A row without text or author is not a post and is dropped.
    """
    out: list[TweetRow] = []
    for tweet in tweets:
        if not isinstance(tweet, dict) or tweet.get('isRetweet'):
            continue
        author = tweet.get('author')
        author = author if isinstance(author, dict) else {}
        metrics = tweet.get('metrics')
        metrics = metrics if isinstance(metrics, dict) else {}
        text = str(tweet.get('text') or '').strip()
        handle = str(author.get('screenName') or '').strip()
        if not text or not handle:
            continue
        quoted = tweet.get('quotedTweet')
        quoted = quoted if isinstance(quoted, dict) else {}
        quoted_author = quoted.get('author')
        quoted_author = quoted_author if isinstance(quoted_author, dict) else {}
        out.append(TweetRow(
            id=str(tweet.get('id') or ''),
            handle=handle,
            name=str(author.get('name') or ''),
            text=text,
            created_at=str(tweet.get('createdAtISO') or tweet.get('createdAt') or ''),
            likes=_int(metrics.get('likes')),
            retweets=_int(metrics.get('retweets')),
            replies=_int(metrics.get('replies')),
            views=_int(metrics.get('views')),
            quoted_id=str(quoted.get('id') or ''),
            quoted_text=str(quoted.get('text') or '').strip(),
            quoted_handle=str(quoted_author.get('screenName') or '').strip(),
        ))
    return out


def _probe_refusal(payload: Any, proc) -> str:
    """What the client said when the session was refused, in its own words where it had any.

    `--json` prints the failure to stdout as `{"ok": false, "error": {"message": ...}}` and
    exits 1, so the payload is the sentence worth showing; the tail of stderr is the fallback
    for a client that died before it could write one.
    """
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    tail = ' '.join((proc.stderr or '').split()[-24:])
    argv = getattr(proc, 'args', None)
    return tail or ("%s rc=%s" % (argv[0] if argv else 'x cli', proc.returncode))


@dataclass
class _Author:
    """One account as the tweets saw it. Mutable because two queries add to one row."""

    handle: str
    name: str = ''
    text: str = ''
    count: int = 0
    source: int = 0

    def score(self, ticker: str) -> int:
        return affinity(self.handle, self.name, ticker)


class XEngine:
    """One question in, one ordered list of candidate URLs out."""

    name = 'x'

    def __init__(self, *, token: str | None = None, ct0: str | None = None,
                 cli: str | None = None, size: int = SEARCH_SIZE,
                 timeout: float = QUERY_TIMEOUT, runner=None):
        self.token = token
        self.ct0 = ct0
        self.cli = cli or os.environ.get('CH_X_CLI', '').strip() or DEFAULT_CLI
        self.size = size
        self.timeout = timeout
        #: The seam the tests use. It takes the argv this engine would run and returns the
        #: tweets, which is the whole of what the subprocess is for.
        self.runner = runner

    # -- the subprocess ---------------------------------------------------------------
    def credentials(self) -> tuple[str, str]:
        """The session this engine borrows, or a refusal that names what is missing.

        A refusal and not a silent empty answer: the round has to be able to tell "X found
        nothing" from "X was never asked", and those are the two facts this whole layer is
        built to keep apart.
        """
        token = self.token or os.environ.get('CH_X_AUTH_TOKEN', '').strip()
        ct0 = self.ct0 or os.environ.get('CH_X_CT0', '').strip()
        if not token or not ct0:
            raise RuntimeError('x search needs CH_X_AUTH_TOKEN and CH_X_CT0'
                               ' (see config/system.yaml.example)')
        return token, ct0

    def _ask(self, text: str, product: str) -> list[dict]:
        """One query, as the tweets it returned."""
        return self._run([self.cli, 'search', text, '-t', product, '--max', str(self.size),
                          '--json'])

    def _exec(self, argv: list[str], timeout: float | None = None):
        """One subprocess with the session in its environment.

        The credentials and the environment are assembled here, once, because a search and the
        测试连接 probe have to spend the same cookie the same way; `_run` reads rows out of this
        call and `probe` reads its exit code and its payload.
        """
        token, ct0 = self.credentials()
        env = {**os.environ, 'TWITTER_AUTH_TOKEN': token, 'TWITTER_CT0': ct0}
        return subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=env,
                              timeout=timeout if timeout is not None else self.timeout)

    def _run(self, argv: list[str]) -> list[dict]:
        """One subprocess, as the rows the client printed.

        The same runner answers a search and an account's own timeline: both are `--json`
        output of one shape, and one runner is what keeps the credentials, the timeout and
        the error text from drifting apart between the two.
        """
        if self.runner is not None:
            return list(self.runner(argv) or [])
        proc = self._exec(argv)
        if proc.returncode != 0:
            raise RuntimeError('x cli rc=%s: %s' % (proc.returncode,
                                                   ' '.join((proc.stderr or '').split()[-24:])))
        payload = json.loads(proc.stdout)
        if isinstance(payload, dict):
            return list(payload.get('data') or [])
        return list(payload or [])

    # -- whether this session still works -----------------------------------------------
    def probe(self) -> dict:
        """The account these cookies claim to be, or the refusal in the client's own words.

        `获取模型` has a `/models` that proves a door opens; X has no such free door, and the
        thing an operator is asking with this button is not "does the internet work" but "is this
        still a logged-in session" - so it asks for the one page only a session can read. A
        refusal comes back in `note` rather than as an exception, the shape `llm.probe` uses: a
        401 is the reading the button was pressed for, and printing it beside the button is the
        whole point.

        Never raises, never records, touches no cooldown: this is a button, not a round, and a
        session resting from a 429 is still the session this button is allowed to ask about.
        """
        out: dict = {"ok": False, "note": "", "username": "", "latency_ms": 0}
        started = time.time()
        try:
            proc = self._exec([self.cli, *PROBE_ARGV], timeout=PROBE_TIMEOUT)
        except RuntimeError as exc:  # the credentials are missing, and it says which
            out["note"] = str(exc)
            return out
        except OSError as exc:  # the client is not installed, or is not runnable
            out["note"] = "%s 无法执行：%s" % (self.cli, exc)
            return out
        except subprocess.SubprocessError as exc:  # it hung past PROBE_TIMEOUT
            out["note"] = "%s 超过 %g 秒未响应：%s" % (self.cli, PROBE_TIMEOUT, exc)
            return out
        out["latency_ms"] = int((time.time() - started) * 1000)
        payload: Any = None
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
        if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is False:
            out["note"] = _probe_refusal(payload, proc)
            return out
        user = payload.get("data")
        user = user.get("user") if isinstance(user, dict) else None
        handle = str((user or {}).get("screenName") or (user or {}).get("username") or "")
        out["ok"] = True
        out["username"] = handle
        if handle:
            out["note"] = "连接正常 · %d 毫秒 · 会话 @%s" % (out["latency_ms"], handle)
        else:
            out["note"] = "连接正常 · %d 毫秒" % out["latency_ms"]
        return out

    # -- one token ---------------------------------------------------------------------
    def search(self, query: str, *, limit: int = 20) -> EngineAnswer:
        address = ADDRESS.search(query)
        if address is None:
            # Nothing to look up. An engine asked a question with no address in it answers
            # nothing rather than searching for the prose around it.
            return EngineAnswer(hits=[], queries=[])
        ticker_match = TICKER.search(query)
        ticker = ticker_match.group(1).strip() if ticker_match else ''

        passes = [(address.group(0), 'Latest')]
        if ticker:
            passes.append(('$' + ticker, 'Top'))

        authors: dict[str, _Author] = {}
        sites: dict[str, int] = {}
        asked: list[str] = []
        posts: list[TweetRow] = []
        seen_posts: set[str] = set()
        for text, product in passes:
            asked.append('x:%s:%s' % (product, text))
            rows = self._ask(text, product)
            # One tweet can answer both passes when the ticker also appears on the
            # address's page, and a post counted twice is a number the card quotes wrong.
            for row in rows_of(rows):
                if row.id and row.id in seen_posts:
                    continue
                seen_posts.add(row.id)
                posts.append(row)
            for tweet in rows:
                self._collect(tweet, authors, sites)
        return EngineAnswer(hits=self._hits(authors, sites, ticker, limit), queries=asked,
                            tweets=posts)

    # -- one account --------------------------------------------------------------------
    def timeline(self, handle: str, *, limit: int = TWEET_LIMIT) -> list[TweetRow]:
        """One account's own recent posts, newest first.

        The round's second question, and the only one asked about a handle instead of about
        a token. A search says where a project lives; this says whether anyone is still
        there - how often it posts and how the posts land - which is the half of "community"
        a profile page cannot show. It is asked about the account the reading already
        proved, so the answer is the project's own timeline and never a stranger's.
        """
        clean = (handle or '').lstrip('@').strip()
        if not clean:
            return []
        return rows_of(self._run([self.cli, 'user-posts', clean, '--max', str(limit),
                                  '--json']))

    def _collect(self, tweet: dict, authors: dict[str, _Author],
                 sites: dict[str, int]) -> None:
        """Fold one tweet into the two accumulators.

        A retweet is skipped because its author is whoever repeated it: the account it
        credits is the one in the text, which the urls list does not carry and the page
        will not show, so keeping it would put a stranger's handle in the reading order.
        """
        if tweet.get('isRetweet'):
            return
        author = tweet.get('author')
        author = author if isinstance(author, dict) else {}
        handle = str(author.get('screenName') or '').strip()
        text = str(tweet.get('text') or '').strip()
        if handle:
            row = authors.get(handle)
            if row is None:
                row = authors[handle] = _Author(handle=handle,
                                                name=str(author.get('name') or ''))
            row.count += 1
            if not row.text:
                row.text = text
        for link in tweet.get('urls') or []:
            if isinstance(link, dict):
                target = str(link.get('expandedUrl') or link.get('url') or '')
            else:
                target = str(link)
            if not target.startswith('http'):
                continue
            host = (urlsplit(target).hostname or '').lower()
            if host in ('x.com', 'twitter.com'):
                # The tweet's own permalink. It is the tweet, not a place the project lives.
                continue
            clean = normalize_url(root_of(target))
            if clean:
                sites.setdefault(clean, len(sites))

    def _hits(self, authors: dict[str, _Author], sites: dict[str, int],
              ticker: str, limit: int) -> list[SearchHit]:
        """The reading order: accounts most like the token first, then the sites they linked.

        The accounts come before the sites because the two budgets are separate -
        `read_profiles` reads eight accounts, `website_candidates` reads six sites - and a
        site is worth reading once an account can confirm it.
        """
        ranked = sorted(authors.values(),
                        key=lambda row: (-row.score(ticker), -row.count, row.handle.lower()))
        hits = [SearchHit(url='https://x.com/%s' % row.handle, engine=self.name, kind='x',
                          title=row.name, snippet=row.text[:180])
                for row in ranked[:PROFILE_HITS]]
        hits.extend(SearchHit(url=url, engine=self.name, kind=kind_of(url))
                    for url, _ in sorted(sites.items(), key=lambda item: item[1]))
        return hits[:limit]
