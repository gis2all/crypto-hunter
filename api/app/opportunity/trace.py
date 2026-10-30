"""线索 → 官方家：一条帖子要落到项目自己的官方来源上，才算一条权威条目。

Discovery answers "what is being talked about"; this answers "whose is it". A post from an
account we already watch is already authoritative. Anything else has to be traced:

- the links it carries (`expandedUrl`, so a `t.co` resolves to the real site);
- the accounts it mentions;
- and, when it carries neither, a **second X search on the project name** - the search answers
  with posts, so the candidate accounts are read off their authors.

The evidence is graded, because a name search runs into impersonators by construction (same
name, `_official` suffix, copied avatar). Only mutual proof makes an account official:

| 级 | 证据 | 结果 |
| --- | --- | --- |
| A | 官网提到项目名/handle，**且**官方号资料页链回同一个域名 | 当官方；账号自动进 watchlist |
| B | handle 与项目名相似，**且**资料页与线索同题 | 进列表，但 trust = 社区（未互证）；不进 watchlist |
| C | 只有同名 | 不进列表，记「未找到官方来源」 |

This is the same shape as `search/discover.py`, and it deliberately is: the reader is reused
(`identify.fetch_page`, `base.links_in`, `harvest.site_candidates`). What cannot be reused is the
proof itself - that module proves a site by "this page mentions the contract address", and a lead
has no address. The equivalent here is the site ↔ account handshake above.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from ..search.base import handle_of, links_in, normalize_url
from ..search.identify import fetch_page

log = logging.getLogger("crypto-hunter.opportunity")

#: Bumped when the rules below change, so a stored verdict can say which rules made it.
TRACE_VERSION = "trace@4"

#: Hosts that are a channel rather than a home: a project's Discord or Telegram says where its
#: people are, not where the project lives. `x.com` is here for the same reason - the account is
#: what we are trying to prove, and a link to it proves nothing on its own.
NOT_A_HOME = ("x.com", "twitter.com", "t.co", "discord.gg", "discord.com", "t.me", "telegram.me",
              "youtube.com", "youtu.be", "medium.com", "mirror.xyz", "linktr.ee", "opensea.io")

#: How many candidates one trace may read. Two sites and three accounts is enough to find a
#: mutual proof when one exists, and the trace runs for eight leads a scan.
MAX_SITES = 2
MAX_PROFILES = 3
MAX_DOCS = 2
_DOC_HINTS = ("docs", "documentation", "whitepaper", "gitbook", "learn", "developers")

_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{2,40})[\"“”'‘’]")
_TITLE_RUN = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){1,2})\b")
_WORD = re.compile(r"[a-z0-9]{3,}")


def name_guess(text: str) -> str:
    """A fallback guess at the project's name, for the second search.

    The grouping pass normally hands the real name over. When a caller has no name, this takes
    the first quoted phrase, else the first run of two or three capitalised words. A guess that
    is wrong costs one wasted search, which is why it is allowed to be a guess.
    """
    body = str(text or "")
    quoted = _QUOTED.search(body)
    if quoted:
        return quoted.group(1).strip()
    run = _TITLE_RUN.search(body)
    return run.group(1).strip() if run else ""


def candidates(text: str, *, handle: str = "", urls=()) -> dict:
    """The three kinds of pointer one post can carry: sites, accounts, and a name."""
    author = str(handle or "").lstrip("@").lower()
    found_sites: list[str] = []
    for raw in list(urls or []) + [url for url, _kind in links_in(str(text or ""))]:
        host = (urlsplit(str(raw)).hostname or "").lower()
        if not host or any(host == bad or host.endswith("." + bad) for bad in NOT_A_HOME):
            continue
        clean = normalize_url(str(raw)) or ""
        if clean and clean not in found_sites:
            found_sites.append(clean)
    mentioned: list[str] = []
    for match in re.findall(r"@([A-Za-z0-9_]{2,20})", str(text or "")):
        low = match.lower()
        if low != author and low not in mentioned:
            mentioned.append(low)
    return {"sites": found_sites[:MAX_SITES], "handles": mentioned[:MAX_PROFILES],
            "name": name_guess(text)}


def similar(name: str, handle: str) -> bool:
    """Does this handle look like it belongs to this name?

    **One distinctive word is enough.** The first version demanded every word of the name appear in
    the handle, which failed on real data: the model answered `Consumer Graph` for a project whose
    account is `@conso_xyz`, so `graph` had nowhere to match and the verdict fell to C. A single
    word of four or more characters matching is the right bar here because this is evidence for
    **level B only** - a weak grade that already prints 「未互证」 and never grants 官方.
    """
    words = [word for word in re.split(r"[^a-z0-9]+", str(name or "").lower()) if len(word) >= 4]
    target = re.sub(r"[^a-z0-9]", "", str(handle or "").lower())
    if not words or not target:
        return False
    return any(target.startswith(word[:4]) or word[:4] in target for word in words)


def mentions(text: str, *needles: str) -> bool:
    """Is one of these names in the text? Case-insensitive, and a needle under three characters
    is refused rather than matched everywhere."""
    body = str(text or "").lower()
    return any(str(needle).lower() in body for needle in needles if len(str(needle)) >= 3)


def shares_topic(profile_text: str, lead_text: str, *, extra=()) -> bool:
    """Do these two texts talk about the same thing?

    Word overlap on words of four or more characters, minus a stop list - the same cheap
    similarity the rest of this codebase uses rather than a model call: this decision is about
    whether to *look* closer, and a wrong yes costs one profile read.
    """
    def words(text: str) -> set[str]:
        return {word for word in _WORD.findall(str(text or "").lower()) if len(word) >= 4}

    left = words(profile_text)
    right = words(lead_text) | {str(item).lower() for item in extra}
    return len(left & right) >= 2


def verdict(*, name: str, handle: str, lead_text: str, site_texts: list[str],
            profile_texts: list[str], site_host: str = "", profile_links=(),
            from_mention: bool = False, site_from_profile: bool = False) -> dict:
    """A, B or C from the evidence - pure, so the rules can be argued with in a test.

    `site_texts` and `profile_texts` are what was actually read; empty lists mean nothing was
    read, which is a C rather than a B: "we did not look" is never a proof.
    """
    linked_hosts = {(urlsplit(link).hostname or "").lower() for link in profile_links}
    # 账号那一侧有两条路：资料页**正文里提到**这个域名，或资料页**链到**它 —— 后者正是我们
    # 找到这个官网的方式，也同样是"账号自己认领了这个站"。
    linked = bool(site_host) and (
        any(mentions(text, site_host) for text in profile_texts) or site_host in linked_hosts)
    domain_match = bool(site_host) and _domain_matches_handle(handle, site_host)
    # 官网这一侧有两条路：正文里写到项目名 / handle，或者站点是从**官方号的资料页**点进去的、
    # 且域名就是账号名的那个。后一条是为 JS 站点留的：tiny-farm.xyz 的首页全是 canvas 与导航，
    # 一个字都不提 "tiny_farming"，但账号自己链到了它，这已经是"账号认领了这个站"（2026-10-29,
    # user 拿 Tiny Farm 指出的）。
    site_side = any(mentions(text, name, handle) for text in site_texts) \
        or (site_from_profile and domain_match)
    account_side = linked or domain_match
    plausible = similar(name, handle) if name else from_mention
    if site_texts and profile_texts and site_side and account_side and plausible:
        how = "资料页链回 %s" % site_host if linked else "官方号与域名 %s 匹配" % site_host
        return {"level": "A", "trust": "official",
                "basis": "官网与官方号互证（官网提到 %s，%s）" % (name or handle, how)}
    # 有名字就比名字（搜索来的账号必须过这一关，冒充号的风险在那里）；没有名字时只认
    # "帖子自己提到的账号" —— 那是这条线索唯一能证明"它在说这个账号"的东西。
    if profile_texts and plausible and any(
            shares_topic(text, lead_text) for text in profile_texts):
        return {"level": "B", "trust": "community",
                "basis": "同名账号（@%s）且近期内容与线索同题，未互证" % handle}
    return {"level": "C", "trust": "unjudged", "basis": "未找到官方来源"}


#: Scripts and styles, which are most of what a modern page is.
_SCRIPT = re.compile(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>")
_TAG = re.compile(r"<[^>]+>")


def visible_text(page: str) -> str:
    """HTML as the words a person would see.

    The trace judges pages, and it used to judge raw markup: X's profile page is 148KB of CSS and
    embedded JSON around one bio line, so `mentions`/`shares_topic` were matching noise and every
    B-level verdict failed (measured 2026-10-19: A 0 / B 0 / C 8 on eight leads). The same text is
    what the extraction is handed, so this fixes the prompt material as well.
    """
    body = _SCRIPT.sub(" ", str(page or ""))
    body = _TAG.sub(" ", body)
    return " ".join(html.unescape(body).split())


#: Anything that looks like a URL, in the raw document.
_HREF = re.compile(r"https?://[^\s\"'<>\\)\]]+")


def links_of(page: str) -> list[str]:
    """Site candidates inside one page, out of the **raw** document.

    X keeps a profile's links in the JSON it embeds, so the visible text has already lost them -
    and without a site there is nothing for the account to shake hands with, which is why every
    verdict stopped at B.
    """
    out: list[str] = []
    for raw in _HREF.findall(str(page or "")):
        host = (urlsplit(raw).hostname or "").lower()
        if not host or any(host == bad or host.endswith("." + bad) for bad in NOT_A_HOME):
            continue
        clean = normalize_url(raw)
        if clean and clean not in out:
            out.append(clean)
    return out


def read_site(url: str, *, fetch=None, raw: bool = False) -> str:
    """One candidate page, through the same reader the round uses (static lanes + Playwright).

    `raw=True` answers the document itself, which is what `links_of` needs; the default is the
    visible text, which is what a verdict is judged on.
    """
    reader = fetch or fetch_page
    try:
        page = str(reader(url) or "")
    except Exception as exc:  # noqa: BLE001
        log.info("opportunity: trace could not read %s: %r", url, exc)
        return ""
    return page if raw else visible_text(page)


def _page(url: str, *, fetch=None) -> tuple[str, list[str]]:
    """One page as (visible text, links), read from the raw document."""
    raw = read_site(url, fetch=fetch, raw=True)
    return visible_text(raw), links_of(raw)


def _docs(links: list[str]) -> list[str]:
    """The first documentation-looking links on an official page."""
    out: list[str] = []
    for url in links:
        low = str(url or "").lower()
        if not low or not any(hint in low for hint in _DOC_HINTS):
            continue
        if low not in out:
            out.append(low)
        if len(out) >= MAX_DOCS:
            break
    return out


def _add_source(sources: list[dict], kind: str, label: str, url: str) -> None:
    url = str(url or "").strip()
    if not url or any(item.get("url") == url for item in sources):
        return
    sources.append({"kind": kind, "label": label, "url": url})


_IMAGE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico)(?:[?#]|$)", re.I)


def _page_candidates(links, limit: int = 4) -> list[str]:
    """The profile links worth fetching: no social hosts, no CDN images.

    A profile page is mostly avatars and banners; taking `links[:2]` read two `pbs.twimg.com`
    JPEGs and never reached the site the account was pointing at (Tiny Farm, 2026-10-29).
    """
    out: list[str] = []
    for link in links:
        host = (urlsplit(link).hostname or "").lower()
        if not host or host in NOT_A_HOME:
            continue
        if any(host == bad or host.endswith("." + bad) for bad in NOT_A_HOME):
            continue
        if host.endswith("twimg.com") or _IMAGE.search(link):
            continue
        if link not in out:
            out.append(link)
        if len(out) >= limit:
            break
    return out


def _domain_matches_handle(handle: str, host: str) -> bool:
    """Does the site's base name equal the handle without punctuation?"""
    def word(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    name = word(handle)
    if not name:
        return False
    labels = [part for part in str(host or "").lower().split(".") if part]
    while labels and labels[0] in ("www", "docs", "doc", "beta", "app"):
        labels.pop(0)
    if not labels:
        return False
    # The registrable name is the first label after www/docs/beta - joining the first two labels
    # spelled `tiny-farm.xyz` as "tinyfarmxyz" and no handle could ever match it (2026-10-29).
    base = word(labels[0])
    return name == base or name.startswith(base) or base.startswith(name)


def trace_lead(lead: dict, *, known: set[str], fetch=None, search=None, web_search=None) -> dict:
    """One lead as its official home, or as the reason it has none.

    `lead` is a material row (text, handle, url, urls, name). `name` is the project name the
    grouping pass read; when it is absent this falls back to `name_guess`. `known` is the
    watchlist's own official handles. `search` is the second X search, injected so a test can
    answer without the network: it takes a query and returns rows with a `handle` attribute.
    """
    author = str(lead.get("handle") or "").lstrip("@").lower()
    if author and author in {name.lstrip("@").lower() for name in known}:
        return {"level": "A", "trust": "official", "account": author, "site": "",
                "basis": "来自我们在盯的官方号 @%s" % author, "version": TRACE_VERSION,
                "pages": [],
                "sources": [{"kind": "official_account", "label": "官方 X",
                             "url": "https://x.com/%s" % author}],
                "page_sources": []}

    pointers = candidates(str(lead.get("text") or ""), handle=author, urls=lead.get("urls") or ())
    # 名字为空时不用发帖人兜底（那比的是另一个问题）：B 改看"这个账号是不是帖子自己提到的"。
    # 分组已经读过整条帖子，它给的项目名比从正文里猜的可靠；只有调用方没给名字时才退回猜测。
    name = str(lead.get("name") or "").strip() or pointers["name"]
    site = pointers["sites"][0] if pointers["sites"] else ""
    site_host = (urlsplit(site).hostname or "") if site else ""
    site_texts: list[str] = []
    page_sources: list[dict] = []
    sources: list[dict] = []
    site_links: list[str] = []
    if site:
        text, site_links = _page(site, fetch=fetch)
        if text:
            site_texts.append(text)
            page_sources.append({"url": site, "text": text})
            _add_source(sources, "official_site", "官网", site)
    for url in _docs(site_links):
        text, _links = _page(url, fetch=fetch)
        if text:
            site_texts.append(text)
            page_sources.append({"url": url, "text": text})
            _add_source(sources, "official_doc", "文档", url)

    accounts: list[str] = []
    for handle in [str(lead.get("project_handle") or "").lstrip("@").lower(),
                   *pointers["handles"]]:
        if handle and handle != author and handle not in accounts:
            accounts.append(handle)
    # 没有注入搜索函数时不能写「搜过」：监控之前没接二次搜索，诊断里却一直显示 searched=true
    # （2026-10-30）。
    searched = search is not None
    if not accounts and name and search is not None:
        # The post named nothing to follow, so the name is asked about: a search answers with
        # posts, and the accounts are read off their authors.
        try:
            for row in search(name) or []:
                handle = str(getattr(row, "handle", "") or "").lstrip("@").lower()
                if handle and handle not in accounts and handle != author:
                    accounts.append(handle)
                if len(accounts) >= MAX_PROFILES:
                    break
        except Exception as exc:  # noqa: BLE001
            # Counted, not swallowed: a second search that fails leaves the lead at C, and the
            # caller has to be able to say "we could not look" rather than "there is nothing".
            searched = False
            log.info("opportunity: the second search for %r did not answer: %r", name, exc)

    # Prefer a handle that looks like the project name; the first mentioned account can be a
    # person or an ecosystem account (e.g. `@justinsuntron` for AINFT / USDD).
    if name:
        accounts.sort(key=lambda handle: 0 if similar(name, handle) else 1)

    profile_texts: list[str] = []
    profile_links: list[str] = []
    account = accounts[0] if accounts else ""
    for handle in accounts[:MAX_PROFILES]:
        page = read_site("https://x.com/%s" % handle, fetch=fetch, raw=True)
        if page:
            profile_texts.append(visible_text(page))
            profile_links.extend(links_of(page))
            _add_source(sources, "official_account", "官方 X", "https://x.com/%s" % handle)
            if not account:
                account = handle
    if not account and name and similar(name, author):
        # The post's own author may be the project: a lead from `@SpoutFi` about Spout Finance.
        account = author
        page = read_site("https://x.com/%s" % author, fetch=fetch, raw=True)
        if page:
            profile_texts.append(visible_text(page))
            # 帖子的作者就是项目号时，资料页的链接同样要读出来：官网常常只挂在这一页，帖子正文
            # 里一个字都没有（Tiny Farm：@tiny_farming 的资料页链到 tiny-farm.xyz，2026-10-29）。
            profile_links.extend(links_of(page))
            _add_source(sources, "official_account", "官方 X", "https://x.com/%s" % author)

    # 帖子里没有官网时，从资料页里找：官方号链到的那个站就是它的官网，读它并与账号互证。
    site_from_profile = False
    if not site and profile_links:
        for candidate in _page_candidates(profile_links):
            body, links = _page(candidate, fetch=fetch)
            host = (urlsplit(candidate).hostname or "").lower()
            if body and (mentions(body, name, account)
                         or _domain_matches_handle(account or author, host)):
                site, site_host, site_texts = candidate, \
                    host, [body]
                site_from_profile = True
                page_sources.append({"url": candidate, "text": body})
                is_doc = any(hint in candidate.lower() for hint in _DOC_HINTS)
                _add_source(sources, "official_doc" if is_doc else "official_site",
                            "文档" if is_doc else "官网", candidate)
                for url in _docs(links):
                    text, _links = _page(url, fetch=fetch)
                    if text:
                        site_texts.append(text)
                        page_sources.append({"url": url, "text": text})
                        _add_source(sources, "official_doc", "文档", url)
                break

    # X profiles sometimes hide their bio link behind a login wall. When the profile gave us
    # nothing, ask the general web for the project's own site, then read the page and its docs.
    if not site and name and web_search is not None:
        query = "%s %s official website docs" % (name, account or "")
        try:
            rows = web_search(query.strip()) or []
        except Exception as exc:  # noqa: BLE001
            log.info("opportunity: web search for %r did not answer: %r", name, exc)
            rows = []
        buckets: dict[str, list[dict]] = {}
        for row in rows[:8]:
            host = (urlsplit(str(row.get("url") or "")).hostname or "").lower()
            labels = [part for part in host.split(".") if part]
            while labels and labels[0] in ("www", "docs", "doc", "beta", "app"):
                labels.pop(0)
            if len(labels) >= 2:
                buckets.setdefault(".".join(labels[-2:]), []).append(row)
        agreed = [group for group in buckets.values() if len(group) >= 2]
        for group in agreed[:MAX_SITES]:
            row = group[0]
            candidate = str(row.get("url") or "")
            host = (urlsplit(candidate).hostname or "").lower()
            if not host or any(host == bad or host.endswith("." + bad) for bad in NOT_A_HOME):
                continue
            body, links = _page(candidate, fetch=fetch)
            if not body or not mentions(body, name, account or author):
                continue
            site, site_host, site_texts = candidate, host, [body]
            page_sources.append({"url": candidate, "text": body})
            _add_source(sources, "official_site", "官网", candidate)
            for url in _docs(links):
                text, _links = _page(url, fetch=fetch)
                if text:
                    site_texts.append(text)
                    page_sources.append({"url": url, "text": text})
                    _add_source(sources, "official_doc", "文档", url)
            break

    answer = verdict(name=name, handle=account or author, lead_text=str(lead.get("text") or ""),
                     site_texts=site_texts, profile_texts=profile_texts, site_host=site_host,
                     profile_links=profile_links,
                     from_mention=bool(account) and account in set(pointers["handles"]),
                     site_from_profile=site_from_profile)
    level = answer["level"]
    labels = {"A": {"official_account": "官方 X", "official_site": "官网", "official_doc": "文档"},
              "B": {"official_account": "候选 X（未互证）", "official_site": "候选官网（未互证）",
                    "official_doc": "候选文档（未互证）"}}
    for item in sources:
        item["label"] = labels.get(level, {}).get(item["kind"], item["label"])
        if level != "A":
            item["kind"] = item["kind"].replace("official_", "candidate_")
    answer.update({"account": account, "site": site, "version": TRACE_VERSION,
                   "searched": searched, "sources": sources, "page_sources": page_sources,
                   "pages": [text for text in site_texts + profile_texts if text]})
    return answer


#: How many projects one scan traces. Deduped by project: a dozen posts about the same thing are
#: one trace. Four traces overlap, and the setting is the safety cap rather than a page size.
MAX_TRACES = 200
TRACE_WORKERS = 4
#: One lead may not hold a worker forever: a page that never answers (or an X profile that
#: redirects to a Tor address) used to stall the whole stage - the 01:51 scan sat in trace for
#: ten hours until the process was restarted (2026-10-30).
LEAD_TIMEOUT = float(os.environ.get("CH_TRACE_LEAD_TIMEOUT", "120") or 120)


def run(*, limit: int | None = None, known: set[str] | None = None, tell=None,
        cancel=None, fetch=None, search=None, web_search=None) -> dict:
    """Trace what is waiting, newest first by the gate's score. Returns the pass's counters.

    Runs **after classification** on purpose: the project name comes from that pass, and a name is
    what the second search needs (`classify.md`'s `name` field). The first version guessed the
    name with a regex during collection and every lead came back C.
    """
    from ..store import db

    from . import settings

    limit = int(settings.value("traces_per_scan", MAX_TRACES) if limit is None else limit)
    counters = {"traced": {"A": 0, "B": 0, "C": 0}, "skipped": 0, "added": [], "failed": 0}
    queue = db.opportunity_trace_queue(limit=limit * 4)
    groups: dict[str, list[dict]] = {}
    for material in queue:
        key = str(material.get("project_name") or "").strip().lower() or \
            str(material.get("handle") or "").lower()
        groups.setdefault(key, []).append(material)

    selected = list(groups.values())[:limit]
    counters["skipped"] = len(queue) - len(groups) + sum(
        len(group) for group in list(groups.values())[limit:])
    total = len(selected) or 1
    done = 0

    def ask(materials: list[dict]) -> tuple[list[dict], dict | None, Exception | None]:
        material = materials[0]
        try:
            urls = json.loads(material.get("urls_json") or "[]")
        except (TypeError, ValueError):
            urls = []
        try:
            project_urls = json.loads(material.get("project_urls_json") or "[]")
        except (TypeError, ValueError):
            project_urls = []
        lead = {"text": material["text"], "handle": material["handle"],
                "urls": list(dict.fromkeys([*urls, *project_urls])),
                "name": material.get("project_name") or "",
                "project_handle": material.get("project_handle") or ""}
        try:
            answer = trace_lead(lead, known=known or set(), fetch=fetch, search=search,
                                web_search=web_search)
            return materials, answer, None
        except Exception as exc:  # noqa: BLE001
            return materials, None, exc

    if cancel is not None and cancel.is_set():
        raise RuntimeError("scan cancelled")
    with ThreadPoolExecutor(max_workers=max(1, min(TRACE_WORKERS, total))) as pool:
        futures = [pool.submit(ask, group) for group in selected]
        for index, future in enumerate(futures):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("scan cancelled")
            try:
                materials, answer, error = future.result(timeout=LEAD_TIMEOUT)
            except TimeoutError:
                material = selected[index][0]
                counters["failed"] += 1
                log.info("opportunity: trace timed out after %ss for %r", LEAD_TIMEOUT,
                         material.get("project_name") or material.get("handle"))
                done += 1
                if tell:
                    tell(done, total)
                continue
            material = materials[0]
            key = str(material.get("project_name") or "").strip().lower() or \
                str(material.get("handle") or "").lower()
            if error is not None:
                counters["failed"] += 1
                log.info("opportunity: trace of %r failed: %r", key, error)
                done += 1
                if tell:
                    tell(done, total)
                continue
            counters["traced"][answer["level"]] = counters["traced"].get(answer["level"], 0) + 1
            db.opportunity_trace_put_many([row["ref"] for row in materials], answer["level"], dict(answer))
            if answer["level"] == "A" and answer["account"] and \
                    answer["account"] not in (known or set()):
                db.opportunity_source_put([{
                    "target": answer["account"], "kind": "official",
                    "ecosystem": answer.get("ecosystem") or "multi", "cluster": None,
                    # 身份库：记下 handle，但不再读它的时间线（2026-10-27 起 X 只跑查询）。
                    "enabled": False, "window_hours": 24, "slice_hours": 4, "max_results": 40,
                    "note": "2026-10-20 溯源 A 级：%s" % answer["basis"],
                    "added_at": db.now_iso(),
                }])
                counters["added"].append(answer["account"])
            done += 1
            if tell:
                tell(done, total)
    return counters
