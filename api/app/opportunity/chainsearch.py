"""未分链的卡：先用已有的字，再一次网页搜索，最后才让模型判断。

抽取只读它手上的材料。大多数线索没有被溯源（每轮只溯源八个项目），帖子又常常不写链，
于是卡片落库时是 `未判定`。这一步在抽取之后统一处理它们，顺序是：

1. 卡片自己的标题与摘要 - 零成本，而且说的是这张卡讲的那件事；
2. 溯源已经读到的页 - 零成本；
3. 通用网页搜索 - 每个项目一次，取前三条结果；
4. 规则读不出来的，攒成一批交给模型，只允许在给出的结果里选。

搜索引擎走普通 HTTP（DDG Lite，被拦就换 Bing），不占 X 调用预算；失败就留 `未判定`，
`ecosystem_checked_at` 不写，下一轮重试。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import base64
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx

from ..llm import client as llm
from ..sources import http
from ..store import db
from . import model, probe
from .schema import MULTI, UNJUDGED, canonical_ecosystem

log = logging.getLogger("crypto-hunter.opportunity")

#: Bumped when the prompt or the parsers change, so a stored reading can name its rules.
SEARCH_VERSION = "chain@1"

#: Three results are enough for a name and a chain sentence; five only makes the prompt longer.
MAX_RESULTS = 3

#: Search requests overlap; the host pacer still serialises each engine.
SEARCH_WORKERS = 6

#: Projects per model call. Ten keeps the answer small enough to parse on the first try.
LLM_BATCH = 10

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

#: 两家无 key 的公共 MCP 搜索。搜索在它们自己的服务器上执行，本机只发一个 JSON-RPC 请求，
#: 所以不会碰上「代理把搜索结果页换掉」那一类问题（2026-10-24 实测：Bing 对真实查询返回无关内容）。
EXA_MCP = "https://mcp.exa.ai/mcp"
PARALLEL_MCP = "https://search.parallel.ai/mcp"
_MCP_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "crypto-hunter"}
_MCP_TIMEOUT = 30.0

#: Parallel asks for a per-process id for its anonymous rate limiting only.
_SESSION = uuid.uuid4().hex

_TAG = re.compile(r"<[^>]+>")
_DDG_LINK = re.compile(r"<a([^>]+class='result-link'[^>]*)>(.*?)</a>", re.S)
_DDG_HREF = re.compile(r"href=\"([^\"]+)\"")
_DDG_SNIP = re.compile(r"<td class='result-snippet'>(.*?)</td>", re.S)
_BING_ITEM = re.compile(r"<li class=\"b_algo\".*?</li>", re.S)
_BING_LINK = re.compile(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)
_BING_SNIP = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _text(raw: str) -> str:
    return " ".join(html_lib.unescape(_TAG.sub(" ", raw or "")).split())


def _real(href: str) -> str:
    """DDG wraps every result in `//duckduckgo.com/l/?uddg=<encoded>`."""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        target = parse_qs(urlsplit(href).query).get("uddg")
        if target:
            return target[0]
    return href


def _mcp_text(body: str) -> str:
    """The first text item of an MCP tools/call answer (SSE envelope or a plain JSON body)."""
    body = str(body or "")
    stripped = body.strip()
    candidates = [stripped] if stripped.startswith("{") else []
    candidates += [line[len("data: "):] for line in re.split(r"\r\n|\r|\n", body)
                   if line.startswith("data: ")]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        error = data.get("error")
        if error:
            raise RuntimeError(str(error.get("message") or error))
        result = data.get("result") or {}
        texts = [item.get("text", "") for item in result.get("content") or []
                 if isinstance(item, dict)]
        if result.get("isError"):
            raise RuntimeError(" ".join(text for text in texts if text) or "MCP tool error")
        text = next((str(value) for value in texts if value), "")
        if text:
            return text
    raise RuntimeError("MCP answer carried no text")


def _mcp(url: str, tool: str, arguments: dict) -> str:
    """One JSON-RPC `tools/call`, under the same per-host pacer the HTTP lanes use."""
    http.pacer(url).wait()
    response = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": tool, "arguments": arguments}},
                          headers=_MCP_HEADERS, timeout=_MCP_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError("HTTP %s" % response.status_code)
    # SSE carries no charset; JSON-RPC is UTF-8 by spec.
    return _mcp_text(response.content.decode("utf-8", errors="replace"))


_EXA_LABELS = ("Title:", "URL:", "Highlights:", "Published:", "Author:")


def _exa(text: str, limit: int) -> list[dict]:
    """Exa answers with `---`-separated `Title:/URL:/Highlights:` blocks."""
    rows: list[dict] = []
    for block in str(text or "").split("\n---\n"):
        title = url = ""
        highlights: list[str] = []
        inside = False
        for line in map(str.strip, block.splitlines()):
            if line.startswith("Title:"):
                title = line[len("Title:"):].strip()
            elif line.startswith("URL:"):
                url = line[len("URL:"):].strip()
            elif inside and line and not line.startswith(_EXA_LABELS):
                highlights.append(line)
            if line.startswith(_EXA_LABELS):
                inside = line.startswith("Highlights:")
        if url:
            rows.append({"url": url, "title": title, "snippet": " ".join(highlights)})
        if len(rows) >= limit:
            break
    return rows


def _parallel(text: str, limit: int) -> list[dict]:
    data = json.loads(text)
    return [{"url": str(row.get("url") or ""), "title": str(row.get("title") or ""),
             "snippet": " ".join(row.get("excerpts") or [])}
            for row in (data.get("results") or [])[:limit]]


def _ddg(page: str) -> list[dict]:
    snippets = _DDG_SNIP.findall(page)
    out: list[dict] = []
    for index, (attrs, body) in enumerate(_DDG_LINK.findall(page)):
        href = _DDG_HREF.search(attrs)
        title = _text(body)
        if not href or not title:
            continue
        out.append({"url": _real(href.group(1)), "title": title,
                    "snippet": _text(snippets[index]) if index < len(snippets) else ""})
    return out


def _bing(page: str) -> list[dict]:
    out: list[dict] = []
    for block in _BING_ITEM.findall(page):
        link = _BING_LINK.search(block)
        if not link:
            continue
        snippet = _BING_SNIP.search(block)
        out.append({"url": _bing_url(html_lib.unescape(link.group(1))), "title": _text(link.group(2)),
                    "snippet": _text(snippet.group(1)) if snippet else ""})
    return out


def _bing_url(href: str) -> str:
    """Bing wraps every result in `/ck/a?...&u=a1<base64 of the url>`."""
    if "bing.com/ck/a" not in href:
        return href
    value = parse_qs(urlsplit(href).query).get("u")
    if not value:
        return href
    raw = value[0][2:] if value[0].startswith("a1") else value[0]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return href


def _tokens(term: str) -> list[str]:
    # ASCII only: a Chinese title is a fine query but a result snippet will not repeat it, so
    # filtering on it would reject every answer. No ASCII token means no filter.
    return [part for part in re.split(r"[^0-9a-z]+", term.lower())
            if len(part) >= 3 and part != "blockchain"]


def _relevant(rows: list[dict], term: str) -> list[dict]:
    """Drop results that are not about this project.

    A blocked or empty search does not answer with nothing - it answers with a generic page
    ("Today in History" was what Bing returned for one run). Feeding that to the model is worse
    than admitting the search failed, so every token of the project name has to appear.
    """
    tokens = _tokens(term)
    if not tokens:
        return rows
    out = []
    for row in rows:
        haystack = ("%s %s %s" % (row["title"], row["snippet"], row["url"])).lower()
        if all(token in haystack for token in tokens):
            out.append(row)
    return out


def search(term: str, get=http.get) -> list[dict]:
    """The first three web results for one project, or `[]` when every lane refuses.

    The MCP lanes run the search on the vendor's servers, so they are tried first; the HTML
    lanes stay as the last resort for a machine whose proxy lets them through.
    """
    lanes = (
        ("exa", lambda: _exa(_mcp(EXA_MCP, "web_search_exa",
                                  {"query": term, "numResults": MAX_RESULTS}), MAX_RESULTS)),
        ("parallel", lambda: _parallel(_mcp(PARALLEL_MCP, "web_search",
                                            {"objective": term, "search_queries": [term],
                                             "session_id": _SESSION}), MAX_RESULTS)),
    )
    for name, call in lanes:
        try:
            rows = [row for row in call() if row["url"]]
        except Exception as exc:
            log.info("opportunity: chain search on %s failed: %r", name, exc)
            continue
        if rows:
            return rows[:MAX_RESULTS]

    query = quote_plus(term)
    engines = (
        ("lite.duckduckgo.com", "https://lite.duckduckgo.com/lite/?q=" + query, _ddg),
        ("www.bing.com", "https://www.bing.com/search?q=" + query, _bing),
    )
    for host, url, parse in engines:
        try:
            page = get(url, headers=_UA)
        except Exception as exc:
            log.info("opportunity: chain search on %s failed: %r", host, exc)
            continue
        rows = [row for row in parse(page) if row["url"] and row["title"]]
        rows = _relevant(rows, term)[:MAX_RESULTS]
        if rows:
            return rows
    return []


def _query(card: dict) -> str:
    name = str(card.get("project_name") or card.get("organizer") or card.get("title") or "")
    return ("%s blockchain" % name).strip()


def _named(text: str) -> str:
    """The one chain this short text names, or `""` when it names none or several.

    Only used on a card's own title and summary: they are short and about this event, so a name
    here is far more reliable than the same name anywhere on a landing page.
    """
    found = _chains_in(text)
    return next(iter(found)) if len(found) == 1 else ""


def _chains_in(text: str) -> set[str]:
    lowered = (text or "").lower()
    found: set[str] = set()
    for chain, aliases in probe.NAMES.items():
        for alias in aliases:
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered):
                found.add(chain)
                break
    return found


def _same(left: object, right: object) -> bool:
    def norm(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    a, b = norm(left), norm(right)
    return bool(a) and a == b


def _supported(chain: str, row: dict, card: dict) -> bool:
    """Is this chain actually supported by the result the model cited?

    `TxFlow → txflow` passed the old check because the evidence URL *was* in the list. The
    chain has to appear in that result, and a value the build does not know as a chain cannot
    simply be the project's own name. `多链` is the one answer that has no single URL to point
    at, so it is checked against the result naming two chains.
    """
    haystack = ("%s %s %s" % (row.get("title"), row.get("snippet"), row.get("url"))).lower()
    if chain == MULTI:
        return len(_chains_in(haystack)) >= 2
    if chain.lower() not in haystack:
        return False
    if chain in probe.NAMES:
        return True
    if not _same(chain, card.get("project_name") or card.get("organizer")):
        return True
    # The chain has the project's own name. That is legitimate only when the result writes it
    # as a chain (`Somnia Network`); a protocol calling itself by its own name (`TxFlow`) is not.
    return bool(re.search(r"\b" + re.escape(chain) + r"\s+(?:chain|network|mainnet|testnet|l1|l2)\b",
                          haystack))


def _free_chain(card: dict) -> str:
    """The chain already on hand: the card's own words first, then the trace's pages."""
    named = _named("%s %s" % (card.get("title") or "", card.get("summary") or ""))
    if named:
        return named
    raw = card.get("trace_json")
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return probe.from_pages(payload.get("pages") or [])


def _from_results(rows: list[dict]) -> str:
    """A strong phrase in the results themselves, before any model call."""
    text = " ".join("%s %s %s" % (row["title"], row["snippet"], row["url"]) for row in rows)
    return probe.signal(text, text)


OFFCHAIN = frozenset(("not_onchain", "not-onchain", "not onchain", "offchain", "off-chain",
                      "非链上", "不上链"))


def _decide(batch: list[tuple[dict, list[dict]]]) -> dict[str, tuple[str, str, bool]] | None:
    """One model call over a batch of cards and their three results each.

    `None` means the model did not answer, which must not be recorded as "we asked and there is
    no chain": that reading would keep the card 未判定 forever.
    """
    payload = [{"slug": card["slug"], "title": card.get("title") or "",
                "summary": card.get("summary") or "",
                "organizer": card.get("organizer") or "",
                "steps": card.get("steps_text") or "",
                "results": [{"title": row["title"], "snippet": row["snippet"],
                             "url": row["url"]} for row in rows]}
               for card, rows in batch]
    messages = [{"role": "system", "content": model.read_prompt("chain.md")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    try:
        raw = llm.complete_json(messages, purpose="opportunity_chain",
                                prompt_version=SEARCH_VERSION)
    except llm.LLMUnavailable as exc:
        log.info("opportunity: chain judgement did not answer: %r", exc)
        return None
    allowed = {card["slug"]: {row["url"]: row for row in rows} for card, rows in batch}
    cards = {card["slug"]: card for card, _rows in batch}
    out: dict[str, tuple[str, str, bool]] = {}
    for item in (raw or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "")
        evidence = str(item.get("evidence") or "")
        stated = str(item.get("chain") or "").strip()
        row = allowed.get(slug, {}).get(evidence)
        if stated.lower() in OFFCHAIN:
            # 不在链上是一条结论，也是一种结果：卡片该从列表上退下去，而不是继续挂着未判定。
            if row is not None:
                out[slug] = ("", evidence, True)
            continue
        chain = canonical_ecosystem(stated)
        # 没有出处、出处不在这次给出的结果里、或那条结果根本不支持这个链，都不是有依据的判断。
        if row is not None and chain != UNJUDGED and _supported(chain, row, cards[slug]):
            out[slug] = (chain, evidence, False)
    return out


def run(tell=None) -> dict:
    """Resolve the chain of every card that is still 未判定. Returns this pass's counters."""
    counters = {"cards": 0, "found": 0, "searched": 0, "unknown": 0, "failed": 0, "llm": 0,
                "offchain": 0, "reset": _reset_suspicious()}
    cards = db.opportunity_unjudged()
    steps = db.opportunity_steps_for([card["slug"] for card in cards])
    for card in cards:
        card["steps_text"] = steps.get(card["slug"], "")
    total = len(cards) or 1
    done = 0

    pending: list[dict] = []
    for card in cards:
        counters["cards"] += 1
        chain = _free_chain(card)
        if chain:
            counters["found"] += db.opportunity_set_ecosystem(card["slug"], chain)
        else:
            pending.append(card)
        done += 1
        if tell:
            tell(done, total)

    if not pending:
        return counters

    with ThreadPoolExecutor(max_workers=min(SEARCH_WORKERS, len(pending))) as pool:
        found = list(pool.map(lambda card: search(_query(card)), pending))

    leftovers: list[tuple[dict, list[dict]]] = []
    for card, rows in zip(pending, found):
        if not rows:
            # 没搜到不等于问过了：留 NULL，下一轮再试。
            counters["failed"] += 1
            continue
        counters["searched"] += 1
        chain = _from_results(rows)
        if not chain:
            leftovers.append((card, rows))
            continue
        if db.opportunity_set_ecosystem(card["slug"], chain, checked=True):
            counters["found"] += 1
        done += 1
        if tell:
            tell(min(done, total), total)

    for start in range(0, len(leftovers), LLM_BATCH):
        batch = leftovers[start:start + LLM_BATCH]
        counters["llm"] += 1
        decisions = _decide(batch)
        if decisions is None:
            continue
        for card, _rows in batch:
            landing = decisions.get(card["slug"])
            if landing:
                chain, _evidence, offchain = landing
                if offchain:
                    db.opportunity_retire(card["slug"], "offchain")
                    counters["offchain"] += 1
                elif db.opportunity_set_ecosystem(card["slug"], chain, checked=True):
                    counters["found"] += 1
            else:
                if db.opportunity_mark_ecosystem_checked(card["slug"]):
                    counters["unknown"] += 1
            done += 1
            if tell:
                tell(min(done, total), total)
    return counters


def _reset_suspicious() -> int:
    """Send back the cards whose chain is the project's own name and not a known chain."""
    reset = 0
    for row in db.opportunity_suspicious_ecosystems():
        chain = str(row.get("ecosystem") or "").lower()
        if chain in probe.NAMES:
            continue
        if _same(chain, row.get("project_name") or row.get("organizer")):
            reset += db.opportunity_reset_ecosystem(row["slug"])
    return reset
