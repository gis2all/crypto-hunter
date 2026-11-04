"""One monitor, one sweep: search, gate, ask the model, write cards.

The sweep is deliberately small. A monitor is a standing question, not a census: the window in
four-hour slices, up to four extraction batches of gated posts, and cards upserted by slug.
Chain-owned fields are read from contracts, page prose is one model call per new project, and
refresh never calls a model. Nothing is shared with the opportunity pipeline except the X
reader, the gate and the session pool.
"""

from __future__ import annotations

import json
import logging
import os
import re
import html as html_lib
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ...llm import client as llm
from ...opportunity import rules, trace as trace_mod, watchlist, xread
from ...sources import http
from ...store import db
from . import chain, settings
from ..registry import Monitor, MONITORS, prompt_dir

log = logging.getLogger("crypto-hunter.monitors")

TZ = ZoneInfo("Asia/Shanghai")

#: 月名写成 "30 October, 12:00 UTC" 的页面不少，英文月名要能认出来。
MONTHS = {name.lower(): index for index, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"), 1)}

#: The stages the page prints, in order.
STAGES = (("collect", "搜 X"), ("gate", "过闸"), ("extract", "抽取"), ("trace", "溯源"),
          ("chain", "链上"), ("store", "落库"), ("refresh", "刷新官网"))

_worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitor")
_cancel: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(TZ)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _when(value) -> str:
    """One local timestamp. `15:00 UTC` becomes `23:00`, a bare date stays a date.

    The pages state their times in UTC (SWARM's "mint opens 15:00 UTC" was stored as a deadline
    and printed as 15:00 local, 8 hours early), so the zone is converted once, here, and the
    card only ever handles Asia/Shanghai (2026-10-30, user).
    """
    text = str(value or "").strip()
    if not text or text in ("未判定", "unknown"):
        return ""
    low = text.lower()
    local_label = bool(re.search(r"\b(?:utc|gmt)\s*\+\s*8\b", low))
    iso_zone = None if local_label else re.search(
        r"(\d{4})-(\d{2})-(\d{2})[ t](\d{2}):(\d{2})(?::\d{2})?\s*"
        r"(z|utc|gmt|[+-]\d{2}:?\d{2})", low)
    if iso_zone:
        year, month, day = int(iso_zone.group(1)), int(iso_zone.group(2)), \
            int(iso_zone.group(3))
        hour, minute = int(iso_zone.group(4)), int(iso_zone.group(5))
        zone = iso_zone.group(6)
        if zone in ("z", "utc", "gmt"):
            stamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        else:
            sign = 1 if zone[0] == "+" else -1
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            stamp = datetime(year, month, day, hour, minute,
                             tzinfo=timezone(sign * offset))
        return stamp.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    utc = bool(re.search(r"\b(utc|gmt)\b|\d{2}z\b", low)) \
        and not re.search(r"\b(utc|gmt)\s*\+\s*8\b", low)
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{2}):(\d{2}))?", low)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4)) if match.group(4) else None
        minute = int(match.group(5)) if match.group(5) else 0
    else:
        match = re.search(r"(\d{1,2})\s+([a-z]+),?\s+(\d{2}):(\d{2})", low)
        if not match or match.group(2) not in MONTHS:
            return text
        year, month, day = _now().year, MONTHS[match.group(2)], int(match.group(1))
        hour, minute = int(match.group(3)), int(match.group(4))
    if not utc:
        return (f"{year:04d}-{month:02d}-{day:02d}"
                + (f" {hour:02d}:{minute:02d}" if hour is not None else ""))
    moment = datetime(year, month, day, hour or 0, minute) + timedelta(hours=8)
    return moment.strftime("%Y-%m-%d %H:%M" if hour is not None else "%Y-%m-%d")


def _slug(text: str) -> str:
    low = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (low or "card")[:60]


def running(monitor_id: str) -> bool:
    scan = db.monitor_scan_latest(monitor_id)
    return bool(scan and scan["status"] == "running")


def state(scan_id: str) -> dict | None:
    return db.monitor_scan_get(scan_id)


def cancel(scan_id: str) -> dict:
    with _lock:
        flag = _cancel.get(scan_id)
    if flag is not None:
        flag.set()
    scan = db.monitor_scan_get(scan_id)
    return {"cancelled": flag is not None, "scan": scan}


def start(monitor_id: str, trigger: str = "manual") -> dict:
    """Create the scan row and hand the body to the worker. Raises on a second concurrent run."""
    monitor = MONITORS.get(monitor_id)
    if monitor is None:
        raise KeyError(monitor_id)
    if running(monitor_id):
        raise RuntimeError("monitor already running")
    scan = {
        "id": db._stable_id("ms", monitor_id + _stamp()),
        "monitor": monitor_id,
        "status": "running",
        "stage": "collect",
        "stages": [{"key": key, "label": label, "state": "wait" if index else "run",
                    "seconds": None, "done": None, "total": None}
                   for index, (key, label) in enumerate(STAGES)],
        "counters": {},
        "error": None,
        "started_at": _stamp(),
        "finished_at": None,
        "trigger": trigger,
    }
    db.monitor_scan_put(scan)
    with _lock:
        _cancel[scan["id"]] = threading.Event()
    _worker.submit(run, scan, monitor)
    return scan


def _save(scan: dict, stage: str | None = None, status: str | None = None,
          error: str | None = None, starts: dict[str, float] | None = None) -> None:
    """Move the marker to `stage`: the one that was running is done, the new one is running."""
    if stage is not None:
        now = time.monotonic()
        for item in scan["stages"]:
            if item["state"] == "run":
                item["state"] = "done"
                item["seconds"] = round(now - (starts or {}).get(item["key"], now), 1)
            if item["key"] == stage:
                item["state"] = "run"
        if starts is not None:
            starts[stage] = now
        scan["stage"] = stage
    if status is not None:
        scan["status"] = status
    if error is not None:
        scan["error"] = error
    db.monitor_scan_put(scan)


def _finish(scan: dict, status: str, error: str | None = None,
            starts: dict[str, float] | None = None) -> None:
    now = time.monotonic()
    for item in scan["stages"]:
        if item["state"] == "run":
            item["state"] = "done" if status == "done" else status
            item["seconds"] = round(now - (starts or {}).get(item["key"], now), 1)
    scan["status"] = status
    scan["error"] = error
    scan["stage"] = None
    scan["finished_at"] = _stamp()
    try:
        _record_llm_usage(scan["counters"], scan["id"])
    except Exception:  # noqa: BLE001 - missing accounting must not hide the scan result
        log.exception("monitor: could not record LLM usage for %s", scan["id"])
    db.monitor_scan_put(scan)
    with _lock:
        _cancel.pop(scan["id"], None)


def _read_prompt_file(monitor: Monitor, name: str) -> str:
    return (prompt_dir(monitor) / name).read_text(encoding="utf-8")


_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_TITLE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)", re.I)
#: 「agentsea — profile pre-registration」「$JAGENT Mint — BNB Smart Chain」：站点名在后半段。
_TITLE_SPLIT = re.compile(r"\s+[|·—–]\s+|\s+-\s+")
#: Cloudflare 挑战页、访问受限页：它们也有 <title>，但不是项目名。
_TITLE_NOISE = ("attention required", "just a moment", "access denied", "cloudflare",
                "are you a robot", "404 not found")


def _site_title(pages: list, names: list[str] | None = None) -> str:
    """The project's own name, off the page's <title> / og:title.

    The model renames a project every sweep ("JAGENT" → "JAGENT Mint Pass"), which made old
    cards look like new ones. The page title is stable, so it wins; the model's title is only
    the fallback when the site has none (2026-10-30, user).

    A title is only accepted when it shares a word with what we already call the project:
    marketing taglines ("Weave ideas into markets.") and Cloudflare pages ("Attention
    Required!") are otherwise indistinguishable from a name.
    """
    html = "\n".join(str(page) for page in pages)
    for pattern in (_PAGE_TITLE, _OG_TITLE):
        match = pattern.search(html)
        if not match:
            continue
        text = html_lib.unescape(match.group(1)).strip()
        text = _TITLE_SPLIT.split(text)[0].strip()
        text = text.split(",")[0].strip() if "," in text else text
        # 「BJAGENT 铸造」这种官网标题尾巴不是名字的一部分。
        text = re.sub(r"\s*(铸造|官网|官方|中文|社区)$", "", text).strip()
        if _title_ok(text, names or []):
            return text
    return ""


def _page_titles(pages: list) -> str:
    """`<title>` / `og:title` of whatever HTML was read, lowercased and unvalidated.

    Type reads this rather than the page body: "ChyAI · AI Agent Launchpad" is the project's own
    one-line description, while a launchpad link in the body may just be a partner (2026-10-30).
    """
    html = "\n".join(str(page) for page in pages or [])
    return " ".join(
        html_lib.unescape(match.group(1)).lower()
        for pattern in (_PAGE_TITLE, _OG_TITLE)
        if (match := pattern.search(html)))


def _title_ok(text: str, names: list[str]) -> bool:
    low = text.lower()
    if not (1 < len(text) <= 60) or any(noise in low for noise in _TITLE_NOISE):
        return False
    # A sentence is a tagline, not a name; an X profile title is not the project's own name.
    if text.rstrip().endswith((".", "!", "?")):
        return False
    if re.search(r"/\s*x\s*$", low) or low.endswith(" on x"):
        return False
    words = set(re.findall(r"[a-z0-9]{3,}", low))
    if not words or not names:
        return True
    for name in names:
        if words & set(re.findall(r"[a-z0-9]{3,}", str(name).lower())):
            return True
    return False


def _probe_https(url: str) -> bool:
    try:
        return http.head(url, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=8, tries=1) < 400
    except Exception:  # noqa: BLE001 - an https that will not answer keeps the http link
        return False


def _secure(url: str, probe=_probe_https) -> str:
    """Prefer https when the site answers it; keep the original link otherwise.

    Trace reports whatever scheme the page was reached on (`http://danta.ai/`), and the card
    should not keep serving a plain-http link when https works (2026-10-30, user).
    """
    text = str(url or "").strip()
    if not text.lower().startswith("http://"):
        return text
    candidate = "https://" + text[7:]
    return candidate if probe(candidate) else text


def _read_prompt(monitor: Monitor) -> str:
    return _read_prompt_file(monitor, monitor.prompt)


#: One sweep reads at most five batches of `max_posts` posts, and keeps at most 20 projects
#: across them. 宁缺毋滥: the model sees every gated post, the cards stay a shortlist
#: (2026-10-30, user).
MAX_BATCHES = 4
ROUND_PROJECTS = 20
POST_CHARS = 800
MONITOR_TOKEN_BUDGET = int(os.environ.get("CH_MONITOR_TOKEN_BUDGET", "200000") or 0)


def _extract_batch(monitor: Monitor, posts: list[dict], run_id: str = "") -> list[dict]:
    messages = [
        {"role": "system", "content": _read_prompt(monitor)},
        {"role": "user", "content": json.dumps(posts, ensure_ascii=False)},
    ]
    value = llm.complete_json(messages, purpose="monitor_extract",
                              prompt_version=monitor.version, run_id=run_id)
    projects = value.get("projects") if isinstance(value, dict) else None
    return [row for row in (projects or []) if isinstance(row, dict) and row.get("title")]


def _merge(batches: list[list[dict]]) -> list[dict]:
    """Same project from two batches: one row, first non-empty field wins.

    Batches are newest-first, so the first sighting keeps its place and the later one only
    fills blanks and adds facts (2026-10-30).
    """
    out: list[dict] = []
    index: dict[str, dict] = {}
    for rows in batches:
        for row in rows:
            key = str(row.get("handle") or "").lstrip("@").lower() \
                or str(row.get("title") or "").strip().lower()
            base = index.get(key)
            if base is None:
                index[key] = row
                out.append(row)
                continue
            for field, value in row.items():
                if not str(base.get(field) or "").strip() and str(value or "").strip():
                    base[field] = value
            facts = [str(item) for item in (base.get("facts") or [])]
            for item in row.get("facts") or []:
                if str(item) not in facts:
                    facts.append(str(item))
            base["facts"] = facts[:6]
            if len(str(row.get("summary") or "")) > len(str(base.get("summary") or "")):
                base["summary"] = row["summary"]
    return out


def _extract(monitor: Monitor, posts: list[dict], run_id: str = "") -> tuple[
        list[dict], int, int, list[int]]:
    """Read the gated posts in batches, merging what the model returns per batch.

    The third value is how many batches failed; the fourth names the batches that were
    successfully read. The caller retires those successful batches and leaves only failed
    ones in the queue.
    """
    limit = monitor.max_posts * MAX_BATCHES
    chunks = [posts[index:index + monitor.max_posts]
              for index in range(0, min(len(posts), limit), monitor.max_posts)]
    if not chunks:
        return [], 0, 0, []
    batches: list[list[dict]] = [[] for _ in chunks]
    successful: list[int] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_extract_batch, monitor, chunk, run_id): index
                   for index, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                batches[index] = future.result()
                successful.append(index)
            except Exception:  # noqa: BLE001 - one unreadable batch must not drop the sweep
                failed += 1
                log.exception("monitor: batch %s failed", index)
    return _merge(batches)[:ROUND_PROJECTS], len(chunks), failed, sorted(successful)


def _retire_batches(monitor_id: str, pending: list[dict], successful: list[int],
                    size: int) -> int:
    """Retire exactly the posts belonging to batches the model successfully read."""
    retired = 0
    for index in successful:
        chunk = pending[index * size:(index + 1) * size]
        retired += db.monitor_posts_mark(
            monitor_id, [str(row.get("tweet_id") or "") for row in chunk]
        )
    return retired


def _post_payload(rows: list[dict]) -> list[dict]:
    """Trim and dedupe identical posts from the same author before buying extraction."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        author = str(row.get("author") or "")
        text = str(row.get("text") or "")
        key = (author.lstrip("@").lower(), " ".join(text.split()).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "author": "@" + author.lstrip("@"),
            "at": row.get("created_at"),
            "url": "https://x.com/%s/status/%s" % (
                author.lstrip("@"), row.get("tweet_id") or ""),
            "text": text[:POST_CHARS],
        })
    return out


def _record_llm_usage(counters: dict, scan_id: str) -> dict:
    """Attach the ledger totals for one sweep and mark a budget overrun."""
    usage = db.llm_usage(scan_id)
    counters["llm"] = usage
    if MONITOR_TOKEN_BUDGET > 0 and usage["total_tokens"] > MONITOR_TOKEN_BUDGET:
        counters["llm_over_budget"] = True
        log.warning("monitor: scan %s used %d LLM tokens (budget %d)",
                    scan_id, usage["total_tokens"], MONITOR_TOKEN_BUDGET)
    return usage


PAGE_FIELDS = ("mint_unit", "mint_kind", "price", "deadline")
PAGE_VERSION = "monitor-page-v1"
PAGE_CHARS = 8000
#: 页面越长，计数越可能不在开头：按这些词取窗口，而不是把整页截前 6000 字。
#: 价格常常和进度挤在同一张表里（Phosphor 的 `Price 1 USDT`），只按铸造词取窗口会把它漏掉。
FILL_WORDS = ("minted", "shares", "supply", "progress", "confirmed",
              "price", "cost", "价格", "售价", "单价",
              "铸造", "已铸", "deadline", "ends", "closes", "until", "截止", "结束")


def _excerpt(text: str, *, width: int = 400, limit: int = 8) -> str:
    low = text.lower()
    seen: list[tuple[int, int]] = []
    for word in FILL_WORDS:
        start = 0
        while len(seen) < limit * 2:
            index = low.find(word, start)
            if index < 0:
                break
            span = (max(0, index - width), min(len(text), index + width))
            if not any(other[0] <= span[0] and span[1] <= other[1] for other in seen):
                seen.append(span)
            start = index + len(word)
    if not seen:
        return text[:6000]
    return "\n---\n".join(text[begin:end] for begin, end in seen[:limit])


_BLANK_VALUES = ("", "未判定", "未知", "unknown", "n/a", "none", "null")


def _blank(value) -> bool:
    """`未判定` is a placeholder the model writes when it did not find the field, not an answer.

    Treating it as present is what kept Phosphor's `Price 1 USDT` and JAGENT's price out of the
    fill pass (2026-10-30, user).
    """
    return str(value or "").strip().lower() in _BLANK_VALUES


def _missing_page_fields(project: dict) -> bool:
    return any(_blank(project.get(key)) for key in PAGE_FIELDS)


def _field_fingerprint(text: str) -> str:
    """Hash only the readings a refresh can actually change."""
    signature = {
        "mint": chain.parse_mint(text),
        "facts": chain.parse_page_facts(text),
    }
    return _fingerprint(json.dumps(signature, ensure_ascii=False, sort_keys=True))


SUMMARY_CHARS = 4000
_EMPTY_SUMMARY = ("官网原文未提供", "无法确认项目内容", "未提供", "未说明参与方式", "未说明参与")


def _page_card(monitor: Monitor, project: dict, *, run_id: str = "") -> dict:
    """One official-page call: prose/domain plus fields the chain cannot own."""
    body = chain.plain("\n".join(str(page) for page in project.get("pages") or []))
    if not body.strip():
        return {}
    head = body[:SUMMARY_CHARS]
    excerpt = _excerpt(body)
    text = (head + "\n---\n" + excerpt).strip()[:PAGE_CHARS]
    if not text.strip():
        return {}
    messages = [{"role": "system", "content": _read_prompt_file(monitor, "summary.md")},
                {"role": "user", "content": text}]
    value = llm.complete_json(
        messages, purpose="monitor_page", prompt_version=PAGE_VERSION, run_id=run_id
    )
    value = value if isinstance(value, dict) else {}
    summary = str((value or {}).get("summary") or "").strip()
    if any(marker in summary for marker in _EMPTY_SUMMARY):
        summary = ""
    domain = str(value.get("domain") or "").strip().lower()
    if domain not in ("crypto", "ai_agent", "other"):
        domain = ""
    out = {"summary": summary, "domain": domain}
    for key in PAGE_FIELDS:
        item = str(value.get(key) or "").strip()
        if item and not _blank(item):
            out[key] = item[:80] if key in ("price", "deadline") else item[:32]
    return out


def _apply_page_card(project: dict, answer: dict) -> bool:
    """Apply prose and page-owned fields without touching chain-owned values."""
    changed = False
    if answer.get("summary"):
        project["summary"] = answer["summary"]
        changed = True
    if answer.get("domain"):
        project["domain"] = answer["domain"]
        changed = True
    for key in PAGE_FIELDS:
        value = answer.get(key)
        generic_unit = key == "mint_unit" and str(project.get(key) or "") in ("NFT", "token")
        if (_blank(project.get(key)) or generic_unit) and not _blank(value):
            project[key] = (_when(value) if key == "deadline" else str(value).strip())
            changed = True
    return changed


def _apply_chain_info(project: dict) -> None:
    """Put contract-owned facts on the project before the page model is called."""
    info = project.get("chain_info") or {}
    if info.get("contract"):
        project["contract"] = str(info["contract"])
    if info.get("minted") is not None:
        project["mint_done"] = info["minted"]
    if info.get("mint_total") is not None:
        project["mint_total"] = info["mint_total"]
    if info.get("token_total") is not None:
        project["token_total"] = info["token_total"]
    if info.get("kind") == "erc20":
        project["mint_unit"] = "token"
    elif info.get("nft") is True:
        project["mint_unit"] = "NFT"
    if not _blank(info.get("mint_price")):
        project["price"] = str(info["mint_price"]).strip()
    if info.get("sale_deadline"):
        deadline = _when(info["sale_deadline"])
        if deadline:
            project["deadline"] = deadline


def _known() -> set[str]:
    """The handles this machine already treats as official, so a post from one needs no trace."""
    try:
        rows = watchlist.enabled()
    except Exception:  # noqa: BLE001 - a missing watchlist is not a reason to fail a sweep
        return set()
    return {str(row.get("target") or "").lstrip("@").lower()
            for row in rows if row.get("kind") == "official"}


def _trace_one(project: dict, known: set[str]) -> dict:
    """Official home for one project, by the same rule the opportunity line uses."""
    handle = str(project.get("handle") or "").lstrip("@")
    lead = {
        "text": " ".join([str(project.get("title") or ""), str(project.get("summary") or ""),
                          " ".join(str(item) for item in project.get("facts") or [])]),
        "handle": handle,
        "urls": [url for url in (project.get("official_url"), project.get("source")) if url],
        "name": str(project.get("title") or ""),
        "project_handle": handle,
    }
    answer = trace_mod.trace_lead(lead, known=known)
    if answer.get("level") != "C":
        return answer
    # 只有落 C 才补二次搜索：帖子里既没有 @ 也没有链接时（Synth Ape / HashOre / Niche
    # Society），这是唯一能跟的线。放在 C 之后而不是每次都给，是因为搜索会花 X 调用、也会
    # 把限流引到后面的资料页上，让本来能判 A 的卡一起掉成 C（2026-10-30 实测）。
    from ...opportunity import chainsearch, scan as opp_scan

    return trace_mod.trace_lead(lead, known=known, search=opp_scan.look_up,
                                web_search=chainsearch.search)


def _probe_first(candidates: list[str], chain_name: str = "") -> dict:
    """Probe the page's contracts until one answers with a minted count.

    A candidate only becomes the card's contract when the chain says something is deployed at
    that address: a node that did not answer is left for the next sweep, and an address with no
    code is not stored as if it were the project (2026-10-31).
    """
    chain_key = chain.chain_id(chain_name)
    if not chain_key:
        return {}
    winner = ""
    for ca in candidates[:6]:
        if chain.mint_only(ca, chain=chain_key).get("minted") is not None:
            winner = ca
            break
    if not winner:
        for ca in candidates[:6]:
            if chain.has_code(ca, chain=chain_key) is True:
                winner = ca
                break
    if not winner:
        return {}
    info: dict = chain.probe(winner, chain=chain_key)
    info.update(chain.sale_facts(winner, chain=chain_key))
    if not info.get("token"):
        extra = chain.token_first(*candidates, chain=chain_key)
        if extra:
            info.update(extra)
    if chain_key != "bsc":
        # The Agent ID registry is a BSC contract; its absence elsewhere says nothing.
        return info
    gate = info.get("agent_gated")
    if gate is True:
        info["agent_source"] = "bytecode"
        info["agent_checked_at"] = _stamp()
    # Backend-signed mints have no registry in bytecode; the wallets that minted are the other
    # side of the proof. Shallow first, then the 600k-block window when it cannot tell
    # (2026-10-31).
    if gate is False and info.get("contract"):
        contract = str(info["contract"])
        evidence = chain.agent_evidence(contract)
        if evidence.get("agent_samples", 0) < 5:
            deeper = chain.agent_evidence(contract, deep=True)
            if deeper.get("agent_samples", 0) >= evidence.get("agent_samples", 0):
                evidence = deeper
        info.update(evidence)
        if evidence.get("agent_source") == "callers":
            return info
    # A sale contract often does not embed the registry while its mint pass does. Check every
    # address on the page before saying this card is not Agent ID gated.
    if gate is not True:
        for ca in candidates[:6]:
            if ca == info.get("contract"):
                continue
            if chain.agent_gate(ca):
                gate = True
                break
    if gate is not None:
        info["agent_gated"] = gate
        if gate:
            info["agent_source"] = "bytecode"
            info["agent_checked_at"] = _stamp()
    return info

RUG_DROP = 0.8


def _mint_capability(info: dict) -> None:
    """Split `is_mintable` into "owner can inflate" and "the mint is simply still open".

    GoPlus answers 1 for both. On an ownerless contract (JAGENT, BabyJagent) or a renounced
    one (SWARM) there is nobody who can inflate at will - the public mint is the product, so
    calling that a red flag would paint every project in the list (2026-10-30, user).
    """
    perms = info.get("perms")
    if perms and "mintable" in perms and not info.get("owner"):
        info["perms"] = [key for key in perms if key != "mintable"]
        info["mint_public"] = True


#: 类型按「你买到什么」分。NFT / 代币份额能从链上验，其余只能看项目自己的说法。
INSCRIPTION_WORDS = ("runes", "rune", "runestone", "inscription", "inscriptions", "ordinals",
                     "brc-20", "brc20", "铭文", "符文")
LAUNCHPAD_WORDS = ("launchpad", "launch pad", "launch platform",
                   "发射台", "发射平台", "发币平台", "发行平台", "打新平台")
TASK_WORDS = ("quest", "quests", "check-in", "checkin", "签到", "打卡", "积分", "任务")
#: 份数单位写着这些说明卖的是代币份额，不是一张一张的 NFT。
SHARE_WORDS = ("share", "shares", "份额", "token", "代币")
#: 单位写着这些说明一份就是一个物件，哪怕同一张卡旁边还有 ERC-20（Tiny Farm 的 farmers、
#: Phosphor 的 cards，2026-10-30）。
NFT_WORDS = ("nft", "card", "cards", "egg", "eggs", "farmer", "farmers")
#: 正文里的 NFT 说法。`profile card` 单独列，是因为 agentsea 这类预注册页面全程只说 card，
#: 一个 `nft` 都不写（2026-10-30, user）。
NFT_TEXT_WORDS = ("nft", "profile card", "collectible", "collectibles")


def _hit(text: str, words: tuple[str, ...]) -> bool:
    """ASCII 词按整词匹配（`task` 不该命中 `multitask`），中文词按包含匹配。"""
    for word in words:
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", text):
                return True
        elif word in text:
            return True
    return False


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _type(info: dict, project: dict) -> str:
    """What the buyer gets: an NFT, a token share, an inscription, or nothing to buy.

    Order matters. A runes project may ship an ERC-721 under the hood and a task platform may
    hand out credential NFTs, so the project's own words come first; only then does the chain
    decide between a tradeable token (share) and a collection (NFT). A card that matches
    nothing stays `unknown` rather than being forced into a bucket (2026-10-30, user).
    """
    title = str(project.get("title") or "")
    summary = str(project.get("summary") or "")
    facts = " ".join(str(item) for item in project.get("facts") or [])
    strong = (title + " " + facts).lower()
    page = chain.plain(" ".join(str(text) for text in project.get("pages") or [])).lower()
    # 只看官网标题（<title> / og:title）：ChyAI、KOAI 的标题就是 "AI Agent Launchpad"，
    # 而正文首屏或页脚里随手一个 launchpad 链接不足以说明项目是什么（2026-10-30）。
    page_titles = str(project.get("page_title") or "").lower() \
        or _page_titles(project.get("pages") or [])
    lead = (title + " " + summary + " " + facts).lower()
    # 铭文只看标题 / facts。摘要会引用 KOL 的措辞：BNBS 的原帖把它叫「符文项目」，官网写的却是
    # `10 shares per wallet`；官网页脚里的 runes 同样不可靠（2026-10-30）。
    if _hit(strong, INSCRIPTION_WORDS):
        return "inscription"
    unit = str(project.get("mint_unit") or "").strip().lower()
    # 单位先于「发射台」：BIBIAI 按「份」卖、抽取摘要却把它叫发射平台；单位是更硬的事实
    # （2026-10-30, user）。
    if unit in ("份",) or _hit(unit, SHARE_WORDS):
        return "share"
    if _hit(strong, LAUNCHPAD_WORDS) or _hit(summary.lower(), LAUNCHPAD_WORDS) \
            or _hit(page_titles, LAUNCHPAD_WORDS):
        return "launchpad"
    hint = str(project.get("asset_type") or "").strip().lower()
    if str(project.get("mint_kind") or "").strip().lower() == "task":
        return "task"
    if hint == "task":
        return "task"
    # 单位比「附近有没有 ERC-20」更可信：Phosphor 的铸造合约 `token()` 指向 PHOS，卖的却是 cards。
    if _hit(unit, NFT_WORDS):
        return "nft"
    if info.get("nft") is True:
        return "nft"
    # 摘要写明是 NFT、且有按枚铸造的总量时，压过「旁边有个 ERC-20」：Moribundus 发的是 NFT，
    # 奖励币 SOUL 只是附带；只是提到「NFT 空投」的份额项目不受影响。
    if _hit(lead, NFT_TEXT_WORDS) and _int(project.get("mint_total")):
        return "nft"
    if info.get("token"):
        return "share"
    if hint in ("nft", "share", "inscription", "launchpad"):
        return hint
    # 「打卡 / 签到」这类动作词放在链上判定之后：Ptah 每天打卡 mint 的是 NFT，不是任务平台。
    if _hit(lead, TASK_WORDS):
        return "task"
    # 官网原文只在这一档参与：`profile card` / `collectible` 足够具体，不会被页脚带偏。
    if _hit(lead, NFT_TEXT_WORDS) or _hit(page, ("profile card", "collectible", "collectibles")):
        return "nft"
    # 只有摘要提到铭文、别的证据都没有：仍按铭文收，但排在所有强证据之后。
    if _hit(summary.lower(), INSCRIPTION_WORDS):
        return "inscription"
    # 最后一道：有分母就说明是按份铸造，单位写着 shares 就是份额，否则是一批 NFT
    # （AIMONSTERS 没有合约可验，靠这一条）。
    if _int(project.get("mint_total")):
        return "share" if _hit(str(project.get("mint_unit") or "").lower(), SHARE_WORDS) else "nft"
    return "unknown"


def _has_site(project: dict) -> bool:
    """A card must point at the project's own site; an account alone is not enough
    (Polka Agents: only a community handle, 2026-10-30, user)."""
    return bool(str(project.get("official_url") or "").strip())


CRYPTO_ANCHORS = ("agent id", "agentid", "agentic", "wallet", "mint", "nft", "token",
                  "contract", "onchain", "0x", "bnb", "bsc", "铸造", "链上")
AGENT_ID_ANCHORS = ("agent id", "agentid", "agent_id", "agentic wallet", "agentic wallets")


#: The project's own product, from its own page. A shared mint gate does not make a
#: fashion brand an AI project (neonoir, 2026-11-01, user).
CRYPTO_WORDS = re.compile(
    r"\b(?:nft|token|coin|chain|blockchain|onchain|on-chain|mint|wallet|defi|dex|"
    r"launchpad|iao|airdrop|staking|dao|bsc|bnb|web3|crypto)\b"
    r"|链上|代币|铸造|钱包|空投|公链|合约|质押|挖矿|发射台|铭文|符文", re.I)
AI_WORDS = re.compile(
    r"\b(?:ai|agent|agentic|llm|mcp|autonomous)\b|智能体|人工智能", re.I)


def _domain_of(project: dict) -> str:
    """crypto / ai_agent / other, read from the official page and the rewritten summary."""
    text = chain.plain(" ".join(str(page) for page in project.get("pages") or []))
    text = (str(project.get("summary") or "") + " " + text).lower()
    if not text.strip():
        return "other"
    if CRYPTO_WORDS.search(text):
        return "crypto"
    if AI_WORDS.search(text):
        return "ai_agent"
    return "other"


def _credible(project: dict, info: dict) -> bool:
    """Is this card about one of the monitor's projects?

    The summary is written by the model and can call an artist's anonymous Q&A page a "Prism
    Pass NFT" (prismshrimp, 2026-10-30). The official pages need an Agent ID relation and a
    crypto anchor, unless the chain itself proves the relation. Summary text is never evidence.
    """
    text = chain.plain(" ".join(str(page) for page in project.get("pages") or [])).lower()
    # The monitor asks for projects with a real Agent ID relation. A support / integration
    # notice is enough; a generic crypto contract is not (2026-11-01, user).
    relation = any(anchor in text for anchor in AGENT_ID_ANCHORS)
    share = info.get("agent_callers")
    relation = relation or info.get("agent_gated") is True
    relation = relation or (isinstance(share, (int, float)) and share >= 0.35)
    if not relation:
        return False
    if info.get("contract") or info.get("token") or project.get("contract"):
        return True
    if _int(info.get("mint_total")) or _int(project.get("mint_total")):
        return True
    return any(anchor in text for anchor in CRYPTO_ANCHORS)


REFRESH_WORKERS = 4


_TIME_NOISE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}(?::\d{2})?\b", re.I)
_ADDRESS_NOISE = re.compile(r"0x[0-9a-fA-F]{6,}")


def _fingerprint(text: str) -> str:
    """A page's readable text without clocks and addresses.

    A countdown would otherwise change every round and buy a model call each time; the mint
    counts themselves are digits we need, so only time-like and address-like runs are dropped.
    """
    body = _ADDRESS_NOISE.sub(" ", str(text or ""))
    body = _TIME_NOISE.sub(" ", body.lower())
    body = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _probe_card(card: dict) -> dict:
    """Re-read the contract a card already points at; an unanswered node keeps the old values."""
    onchain = (card.get("fields") or {}).get("onchain") or {}
    ca = str(onchain.get("contract") or "").strip()
    # A card built before the address filter can point at the registry or the zero address;
    # probing those would print someone else's numbers on this card (2026-10-31).
    if not ca or ca in chain.INFRA:
        return {}
    chain_key = chain.chain_id(str(card.get("chain") or ""))
    if not chain_key:
        return {}
    proxy = str(onchain.get("proxy") or "")
    try:
        return chain.probe(ca, chain=chain_key, verify_code=False, read=proxy)
    except Exception:  # noqa: BLE001 - a chain gap must not fail the refresh worker
        log.exception("monitor: chain refresh failed for %s", ca)
        return {}


def _dead_contract(fields: dict, card: dict) -> bool:
    """Drop a stored address the chain itself says is empty.

    A node that did not answer is not evidence: `has_code` returns None there and the card is
    left alone for the next sweep. A confirmed `"0x"` is. Cards made before the address filter
    can still carry the zero address or the registry; those are dropped too (2026-10-31).
    """
    onchain = dict(fields.get("onchain") or {})
    ca = str(onchain.get("contract") or "").strip().lower()
    if not ca:
        return False
    chain_key = chain.chain_id(str(card.get("chain") or ""))
    if ca in chain.INFRA:
        dead = True
    elif not chain_key:
        return False
    else:
        dead = chain.has_code(ca, chain=chain_key) is False
    if not dead:
        return False
    fields["onchain"] = {}
    fields.pop("mint", None)
    log.info("monitor: %s has no contract on %s; dropped", ca, chain_key or "this chain")
    return True


def _merge_chain(fields: dict, info: dict) -> bool:
    """Fold a fresh probe into a card. Progress only moves forward, never backwards."""
    if not info:
        return False
    onchain = dict(fields.get("onchain") or {})
    changed = False
    if not _blank(info.get("mint_price")) and fields.get("price") != info["mint_price"]:
        fields["price"] = str(info["mint_price"]).strip()
        changed = True
    if info.get("sale_deadline"):
        deadline = _when(info["sale_deadline"])
        if deadline and fields.get("deadline") != deadline:
            fields["deadline"] = deadline
            changed = True
    if info.get("pools") == 0:
        for key in ("pool", "pool_liquidity_usd", "lp_state", "lp_locked_pct",
                    "lp_burnt_pct", "lp_locked"):
            if key in onchain:
                onchain.pop(key, None)
                changed = True
    for key, value in info.items():
        if value is None or key in ("minted", "mint_total", "sale_start", "sale_deadline",
                                    "mint_price"):
            continue
        if onchain.get(key) != value:
            onchain[key] = value
            changed = True
    mint = dict(fields.get("mint") or {})
    old_done = _int(mint.get("done"))
    old_total = _int(mint.get("total"))
    done, total = _int(info.get("minted")), _int(info.get("mint_total"))
    if done is not None and (old_done is None or done >= old_done):
        if done != old_done:
            mint["done"] = done
            changed = True
    if total is not None and (old_total is None or total >= old_total):
        if total != old_total:
            mint["total"] = total
            changed = True
    if mint.get("done") is not None and mint.get("total"):
        state = "sold_out" if int(mint["done"]) >= int(mint["total"]) else "live"
        if mint.get("state") != state:
            mint["state"] = state
            changed = True
    if info.get("minted") is not None or info.get("mint_logs") is not None:
        if mint.get("source") != "chain":
            mint["source"] = "chain"
            changed = True
    if mint:
        fields["mint"] = mint
    fields["onchain"] = onchain
    return changed


#: Quote tokens a page prints as payment. They are never the project's own token.
QUOTE_TOKENS = frozenset({
    "0x55d398326f99059ff775485246999027b3197955",  # USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",  # FDUSD
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",  # DAI
})
#: The readings a token probe may add to a share card; its mint numbers belong to the sale.
TOKEN_READINGS = ("kind", "token_total", "decimals", "holders", "top10", "pools", "pool",
                  "pool_liquidity_usd", "lp_state", "lp_locked_pct", "lp_burnt_pct",
                  "lp_locked", "perms", "honeypot", "buy_tax", "sell_tax", "mint_public")


def _find_token(fields: dict, page: str, chain_key: str) -> bool:
    """Look for the project's token on a page that only ever named the mint contract.

    The page is already rendered for the site check; this reuses it. Quote tokens are
    skipped, and a token already on the card is never replaced (2026-11-01, user).
    """
    if not chain_key or fields.get("type") not in ("share", "inscription"):
        return False
    onchain = dict(fields.get("onchain") or {})
    if onchain.get("token"):
        return False
    addresses = []
    for found in re.findall(r"0x[0-9a-fA-F]{40}", page or ""):
        address = found.lower()
        if address in chain.INFRA or address in QUOTE_TOKENS or address in addresses:
            continue
        addresses.append(address)
        if len(addresses) >= 10:
            break
    if not addresses:
        return False
    facts = chain.token_first(*addresses, limit=10, chain=chain_key)
    token = str(facts.get("token") or "")
    if not token:
        return False
    onchain.update(facts)
    try:
        security = chain.probe(token, chain=chain_key)
    except Exception:  # noqa: BLE001 - a token page with no safety sheet is still a token
        security = {}
    for key in TOKEN_READINGS:
        if key in security and security[key] is not None:
            onchain[key] = security[key]
    fields["onchain"] = onchain
    log.info("monitor: found token %s on the card's own site", token)
    return True


def _refresh_card(monitor: Monitor, card: dict) -> str:
    """Re-read one card's chain facts and site, then write back progress / price / deadline.

    Only readings move: title, type, summary and trust are left to the X sweep. An unchanged
    page costs one render, no model call (2026-10-30, user). The chain is re-probed every sweep
    even when the page did not move, because mint progress lives there (2026-10-31).
    """
    fields = dict(card.get("fields") or {})
    if _dead_contract(fields, card):
        moved = True
    else:
        moved = _merge_chain(fields, _probe_card(card))
    onchain = dict(fields.get("onchain") or {})
    ca = str(onchain.get("contract") or "").strip()
    chain_key = chain.chain_id(str(card.get("chain") or ""))
    chain_sale: dict = {}
    if ca and chain_key and (_blank(fields.get("price")) or _blank(fields.get("deadline"))):
        chain_sale = chain.sale_facts(ca, chain=chain_key)
        if _merge_chain(fields, chain_sale):
            moved = True
    if ca and chain_key and not onchain.get("code_fp"):
        # Backfill the template identity once for cards written before 2026-10-31; the dedupe
        # pass cannot group a card that has no fingerprint yet.
        try:
            identity = chain.identify(str(onchain.get("proxy") or ca), chain=chain_key)
        except Exception:  # noqa: BLE001 - a missing fingerprint is not a refresh failure
            identity = {}
        if identity:
            onchain.update(identity)
            fields["onchain"] = onchain
            moved = True
    url = str(card.get("official_url") or "").strip()
    if not url:
        if moved:
            db.monitor_card_patch(monitor.id, str(card.get("slug") or ""), fields)
            return "changed"
        return "skip"
    from ...sources import browser

    try:
        page = browser.render(url) or ""
    except Exception:  # noqa: BLE001 - a site that will not answer keeps the card as it was
        if moved:
            db.monitor_card_patch(monitor.id, str(card.get("slug") or ""), fields)
            return "changed"
        return "skip"
    if len(page) < 200:
        if moved:
            db.monitor_card_patch(monitor.id, str(card.get("slug") or ""), fields)
            return "changed"
        return "skip"
    text = chain.plain(page)
    digest = _field_fingerprint(text)
    # A share card written before its token was known can still recover it from this page.
    if _find_token(fields, page, chain_key):
        moved = True
    if fields.get("site_hash") == digest:
        fields["site_checked_at"] = _stamp()
        db.monitor_card_patch(monitor.id, str(card.get("slug") or ""), fields)
        return "changed" if moved else "unchanged"
    mint = dict(fields.get("mint") or {})

    parsed = chain.parse_mint(text)
    facts = chain.parse_page_facts(text)
    price = str(fields.get("price") or "")
    deadline = str(fields.get("deadline") or "")
    if not chain_sale.get("mint_price") and not _blank(facts.get("price")):
        price = str(facts["price"]).strip()
        fields["price"] = price
    if not chain_sale.get("sale_deadline") and not _blank(facts.get("deadline")):
        deadline = _when(facts["deadline"])
        if deadline:
            fields["deadline"] = deadline
    old_done = _int(mint.get("done"))
    old_total = _int(mint.get("total"))
    re_done, re_total = _int(parsed.get("done")), _int(parsed.get("total"))

    def sane(done: int | None, total: int | None) -> bool:
        if done is None and total is None:
            return False
        if done is not None and old_done is not None and done < old_done:
            # 进度只在往前：变小说明抓到的是“每钱包上限 / 关卡数”这类别的表。
            return False
        if done is not None and total is not None and done > total:
            return False
        if old_total and total and total < old_total * 0.5:
            return False
        return True

    # The chain probe has already moved progress forward; page parsing only adds a larger,
    # internally consistent reading when the node did not expose one.
    done, total = re_done, re_total
    if not sane(done, total):
        done, total = old_done, old_total
    if done is not None:
        mint["done"] = done
    if total is not None:
        mint["total"] = total
    if mint.get("done") is not None and mint.get("total"):
        mint["state"] = "sold_out" if int(mint["done"]) >= int(mint["total"]) else "live"
        if (done, total) != (old_done, old_total):
            mint["source"] = "site"
    if mint:
        fields["mint"] = mint
    fields["site_hash"] = digest
    fields["site_checked_at"] = _stamp()
    db.monitor_card_patch(
        monitor.id, str(card.get("slug") or ""), fields,
        price=None if _blank(price) else price,
        deadline=None if _blank(deadline) else deadline,
    )
    return "changed"


#: How many unresolved cards one sweep may spend chain calls on. The queue is the library,
#: not the sweep's new cards: three a round drains a few dozen cards in a fortnight without
#: turning one refresh into an audit (2026-10-31, user).
AGENT_BACKFILL = 3
AGENT_RECHECK_DAYS = 30


def _agent_evidence_missing(card: dict) -> bool:
    onchain = (card.get("fields") or {}).get("onchain") or {}
    if not onchain.get("contract") or onchain.get("agent_gated") is True:
        return False
    if onchain.get("agent_source") in ("bytecode", "callers"):
        return False
    checked = str(onchain.get("agent_checked_at") or "")
    if not checked:
        return True
    try:
        at = datetime.fromisoformat(checked)
    except ValueError:
        return True
    return datetime.now().astimezone() - at >= timedelta(days=AGENT_RECHECK_DAYS)


def _agent_backfill(monitor: Monitor, cards: list[dict]) -> int:
    """Spend this sweep's evidence budget on the oldest unresolved BSC cards.

    A card whose mints cannot be sampled is stamped with `agent_checked_at`; the backoff
    keeps the same dead end from being re-read every hour (2026-10-31).
    """
    todo = [card for card in cards
            if _agent_evidence_missing(card)
            and chain.chain_id(str(card.get("chain") or "")) == "bsc"]
    todo.sort(key=lambda card: str(card.get("first_seen") or ""))
    done = 0
    for card in todo[:AGENT_BACKFILL]:
        onchain = (card.get("fields") or {}).get("onchain") or {}
        ca = str(onchain.get("contract") or "")
        evidence = chain.agent_evidence(ca)
        if evidence.get("agent_samples", 0) < 5:
            deeper = chain.agent_evidence(ca, deep=True)
            if deeper.get("agent_samples", 0) >= evidence.get("agent_samples", 0):
                evidence = deeper
        fields = dict(card.get("fields") or {})
        merged = dict(fields.get("onchain") or {})
        merged.update(evidence)
        fields["onchain"] = merged
        db.monitor_card_patch(monitor.id, str(card.get("slug") or ""), fields, touch=False)
        done += 1
    return done


def _rug(prev: dict, now: dict) -> dict:
    """Did the pool go away since the last sweep? Only two rounds can answer that.

    The first sweep has nothing to compare, so it says nothing at all rather than 未判定: an
    empty pool at mint time is normal, and calling that a rug would be wrong every time.
    """
    if not prev or not now:
        return {}
    before_pool = str(prev.get("pool") or "")
    before_liq = prev.get("pool_liquidity_usd") or prev.get("liquidity_usd")
    pool = str(now.get("pool") or "")
    liq = now.get("pool_liquidity_usd") or now.get("liquidity_usd")
    if not before_pool and not isinstance(before_liq, (int, float)):
        return {}
    # 这一轮什么都没答（既没池子列表也没流动性）时不能判撤池 —— 那是没问到，不是没了。
    if "pools" not in now and not isinstance(liq, (int, float)) and not pool:
        return {}
    if before_pool and not pool:
        return {"state": "gone", "why": "池子不在了", "before": before_pool}
    if (isinstance(before_liq, (int, float)) and before_liq > 0
            and isinstance(liq, (int, float))):
        if liq <= before_liq * (1 - RUG_DROP):
            return {"state": "drained", "before": before_liq, "after": liq}
        return {"state": "live", "before": before_liq, "after": liq}
    return {"state": "live"} if pool else {}


def _same_title(left: str, right: str) -> bool:
    a, b = str(left or "").strip().lower(), str(right or "").strip().lower()
    if len(a) >= 4 and a == b:
        return True

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    norm_a = norm(a)
    return len(norm_a) >= 4 and norm_a == norm(b)


def _same_project(info: dict, slug: str, title: str,
                  before: dict[str, dict] | None) -> dict:
    """The earliest card that is the same project, not merely the same template.

    The fingerprint alone can match two projects built from one template, so it only counts
    when a second signal agrees: the contract, the token, or a specific name. A shared site
    host is not enough - one launchpad hosts dozens of projects. The new card is absorbed
    into this row at write time; the sweep never inserts a second row for one project
    (2026-10-31, user).
    """
    fingerprint = str((info or {}).get("code_fp") or "")
    contract = str((info or {}).get("contract") or "").lower()
    token = str((info or {}).get("token") or "").lower()
    if (not fingerprint and not contract) or not before:
        return {}
    rows = []
    for key, row in before.items():
        if key == slug:
            continue
        other = ((row.get("fields") or {}).get("onchain") or {})
        same_contract = bool(contract) and \
            str(other.get("contract") or "").lower() == contract
        same_fp = bool(fingerprint) and str(other.get("code_fp") or "") == fingerprint
        if not (same_fp or same_contract):
            continue
        same = (same_contract
                or (token and str(other.get("token") or "").lower() == token)
                or _same_title(title, row.get("title")))
        if same:
            rows.append(row)
    if not rows:
        return {}
    return min(rows, key=lambda row: str(row.get("first_seen") or ""))


def _settle_title(row: dict, head: dict | None, site_title: str) -> None:
    """Keep a card's name stable; the official site may rename it only after two rounds."""
    fields = dict(row.get("fields") or {})
    if head is None:
        if site_title:
            row["title"] = site_title
            fields["site_title"] = site_title
            fields["title_source"] = "site"
        else:
            fields["title_source"] = "model"
        fields.pop("site_title_pending", None)
        row["fields"] = fields
        return
    old = dict(head.get("fields") or {})
    accepted = str(old.get("site_title") or "")
    pending = str(old.get("site_title_pending") or "")
    if not site_title:
        row["title"] = str(head.get("title") or row.get("title") or "")
        fields["title_source"] = old.get("title_source") or ("site" if accepted else "model")
        if accepted:
            fields["site_title"] = accepted
        if pending:
            fields["site_title_pending"] = pending
        row["fields"] = fields
        return
    if not accepted:
        # A card created before this rule, or from a model name: the site name wins once.
        row["title"] = site_title
        fields["site_title"] = site_title
        fields["title_source"] = "site"
        fields.pop("site_title_pending", None)
    elif site_title == accepted:
        row["title"] = str(head.get("title") or accepted)
        fields["site_title"] = accepted
        fields["title_source"] = "site"
        fields.pop("site_title_pending", None)
    elif pending == site_title:
        # Seen on two consecutive sweeps: a real rename.
        row["title"] = site_title
        fields["site_title"] = site_title
        fields["title_source"] = "site"
        fields.pop("site_title_pending", None)
    else:
        row["title"] = str(head.get("title") or accepted)
        fields["site_title"] = accepted
        fields["title_source"] = "site"
        fields["site_title_pending"] = site_title
    row["fields"] = fields


def _known_value(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and text not in ("未判定", "unknown", "N/A", "-")


def _later_mint(old, new) -> dict:
    old, new = dict(old or {}), dict(new or {})
    if not old:
        return new
    if not new:
        return old
    out = {**old, **new}
    done = [value for value in (_int(old.get("done")), _int(new.get("done")))
            if value is not None]
    if done:
        out["done"] = max(done)
    total = _int(new.get("total"))
    if total is None:
        total = _int(old.get("total"))
    if total is not None:
        out["total"] = total
    if out.get("done") is not None and out.get("total"):
        out["state"] = "sold_out" if out["done"] >= out["total"] else "live"
    return out


def _absorb(head: dict, card: dict) -> dict:
    """Fold a newly found event into the project's existing card, keeping its identity."""
    out = dict(head)
    fields = dict(head.get("fields") or {})
    new_fields = dict(card.get("fields") or {})
    info = dict(new_fields.get("onchain") or {})
    _merge_chain(fields, info)
    for key, value in new_fields.items():
        if key in ("onchain", "mint", "dup_of",
                   "site_title", "site_title_pending", "title_source") \
                or value in (None, "", {}):
            continue
        if key == "page_excerpt":
            if len(str(value)) > len(str(fields.get(key) or "")):
                fields[key] = value
            continue
        fields[key] = value
    fields["mint"] = _later_mint(fields.get("mint"), new_fields.get("mint"))
    out["fields"] = fields
    sources = [row for row in (head.get("sources") or []) if isinstance(row, dict)]
    seen = {str(row.get("url") or "") for row in sources}
    for row in card.get("sources") or []:
        if isinstance(row, dict) and str(row.get("url") or "") not in seen:
            sources.append(row)
            seen.add(str(row.get("url") or ""))
    post = str(card.get("source_url") or "")
    if post and post not in seen:
        sources.append({"kind": "post", "label": "来源帖", "url": post})
    out["sources"] = sources
    if _known_value(card.get("price")):
        out["price"] = card["price"]
    if card.get("deadline"):
        out["deadline"] = card["deadline"]
    if len(str(card.get("summary") or "")) > len(str(head.get("summary") or "")):
        out["summary"] = card["summary"]
    rank = {"unjudged": 0, "community": 1, "official": 2}
    if rank.get(str(card.get("trust")), 0) > rank.get(str(head.get("trust")), 0):
        out["trust"] = card.get("trust")
        out["trust_basis"] = card.get("trust_basis")
    out["updated_at"] = card.get("updated_at")
    return out


def _card(monitor: Monitor, project: dict, scan_id: str,
          before: dict[str, dict] | None = None) -> dict:
    # 官网标题优先：模型每轮会换叫法，站点名不会（JAGENT → JAGENT Mint Pass）。
    # 前提是和模型给的名字有共同词，否则营销标语 / Cloudflare 页会被当成项目名。
    model_title = str(project.get("title") or "").strip()
    # The official page is the authority; the model's name is only the fallback when the site
    # has no usable title (2026-11-01, user).
    site_title = _site_title(
        project.get("pages") or [],
        [model_title, str(project.get("handle") or ""),
         str(project.get("official_account") or "")],
    )
    title = site_title or model_title
    # 账号优先：模型每轮给的项目名会变（"Tiny Farming" / "Tiny Farming Farmer NFT"），
    # 按名字做 slug 会让同一个项目每轮多一张卡；handle 稳定得多（2026-10-29）。
    # A stable identity: the handle first, then the contract; the name is the last resort.
    slug = _slug(str(project.get("handle") or project.get("official_account") or "")
                 .lstrip("@") or str(project.get("contract") or "") or title)
    previous = (before or {}).get(slug)
    prev_fields = (previous or {}).get("fields") or {}
    # 资料页认领过的站只在资料页又读到时才允许换：某轮读不到资料页，不能把它降级回帖子里的
    # 链接（danta pixel 从 dantapixel.deed.page 掉回 danta.ai，2026-10-30）。
    sticky = bool(previous and prev_fields.get("site_source") == "profile"
                  and not project.get("site_from_profile"))
    if sticky:
        prev_url = str(previous.get("official_url") or "").strip()
        if prev_url:
            project["official_url"] = prev_url
            for source in project.get("sources") or []:
                if isinstance(source, dict) and source.get("kind") == "official_site":
                    source["url"] = prev_url
    # 模型偶尔漏填 chain，而摘要里明明写着「Robinhood Chain 上的 launchpad」：从卡自己的文字
    # 里认一次，认不出才是未判定（2026-10-30）。
    chain_name = str(project.get("chain") or "").strip()
    if chain_name in ("", "未判定", "unknown"):
        chain_name = chain.guess_chain(
            title, str(project.get("summary") or ""),
            " ".join(str(item) for item in project.get("facts") or []),
            # 帖子和摘要常常不写链，官网正文通常写着（AIMONSTERS 的 BSC mainnet，2026-10-30）。
            *[str(page) for page in project.get("pages") or []]) or "未判定"
    if chain_name == "未判定" and (project.get("chain_info") or {}).get("contract"):
        # 只探了 BSC 节点，能探到就是这个链上的地址。
        chain_name = "BSC"
    fields: dict = {key: str(project.get(key) or "未判定") for key in monitor.fields}
    fields["chain"] = chain_name
    fields["site_source"] = ("profile" if project.get("site_from_profile") or sticky
                             else "trace")
    fields["domain"] = str(project.get("domain") or "other")
    # 类型判定读过官网正文，留一段摘录：回填或事后复查时不必再抓一次站
    # （agentsea 全程只说 profile card，2026-10-30）。
    pages = chain.plain("\n".join(str(page) for page in project.get("pages") or []))
    if pages:
        fields["page_excerpt"] = pages[:2000]
    page_title = _page_titles(project.get("pages") or [])
    if page_title:
        fields["page_title"] = page_title
    # 抽取阶段的摘要留档：类型判定吃的是它，事后要能回答「当初为什么这么判」
    # （2026-10-30）。
    if project.get("summary_extract"):
        fields["summary_extract"] = str(project["summary_extract"])
    # 开始和截止是两个时间，不再互相顶替；带 UTC 的在这里统一换成北京时间（2026-10-30）。
    fields["start"] = _when(project.get("start"))
    fields["deadline"] = _when(project.get("deadline"))
    if project.get("chain_info"):
        # 监控自己的字段在上面；链上那一组单独挂在 `onchain` 下 —— 不能叫 `chain`，那是这张卡
        # 自己的「链名」字段，会被覆盖（2026-10-30）。
        fields["onchain"] = project["chain_info"]
    info = project.get("chain_info") or {}
    head = _same_project(info, slug, title, before)
    _mint_capability(info)
    fields["type"] = str(project.get("type") or _type(info, project))
    rug = _rug(((previous or {}).get("fields") or {}).get("onchain") or {}, info)
    if rug:
        fields.setdefault("onchain", {})["rug"] = rug

    # 进度以链上为准：721 的 totalSupply 就是已铸造份数；总量先用合约给的，合约没有才用官网的。
    done = _int(info.get("minted") if info.get("minted") is not None
                else project.get("mint_done"))
    total = _int(info.get("mint_total")) or _int(project.get("mint_total"))
    # 代币总量不挂在进度里：它和「铸造总量」是两个量，摆在一起只会被读混（2026-10-30, user）。
    if done is not None or total is not None:
        fields["mint"] = {
            "done": done,
            "total": total,
            "unit": str(project.get("mint_unit") or ""),
            "state": str(project.get("mint_state") or ""),
            "kind": str(project.get("mint_kind") or ""),
            "source": "chain" if info.get("minted") is not None
            or info.get("token_total") is not None else "site",
        }
    # 来源为空时至少补上账号 / 官网：B 级（社区）常只有账号，页脚不能是空的
    # （Polka Agents，2026-10-30）。
    sources = list(project.get("sources") or [])
    if not sources:
        account = str(project.get("official_account") or project.get("handle") or "").lstrip("@")
        if account:
            label = "官方 X" if project.get("trust") == "official" else "社区 X"
            sources.append({"kind": "official_account", "label": label,
                            "url": "https://x.com/%s" % account})
        if project.get("official_url"):
            sources.append({"kind": "official_site", "label": "官网",
                            "url": str(project["official_url"])})
    return {
        "slug": slug,
        "title": title,
        "project": project.get("project") or title,
        "handle": str(project.get("handle") or "").lstrip("@") or None,
        "chain": chain_name,
        "status": project.get("status") or "unknown",
        "price": fields.get("price"),
        "supply": fields.get("supply"),
        "deadline": fields.get("deadline") or None,
        "summary": str(project.get("summary") or "").strip(),
        "official_url": project.get("official_url") or None,
        "official_account": str(project.get("official_account") or "").lstrip("@") or None,
        "source_url": project.get("source") or None,
        "trust": project.get("trust") or "unjudged",
        "trust_basis": project.get("trust_basis") or None,
        "sources": sources,
        "fields": fields,
        "_absorb": str(head.get("slug") or "") if head else "",
        "_site_title": site_title,
        "version": monitor.version,
        "first_seen": _stamp(),
        "updated_at": _stamp(),
        "scan": scan_id,
    }



def run(scan: dict, monitor: Monitor) -> None:
    """The body of one sweep. Every failure ends the scan with a reason, never silently."""
    flag = _cancel.get(scan["id"])
    counters = scan["counters"]
    starts: dict[str, float] = {}

    try:
        if not llm.configured():
            _finish(scan, "failed", "未配置模型：监控需要模型把帖子读成卡片", starts)
            return

        _save(scan, stage="collect", starts=starts)
        rows = []
        calls = 0
        failed = 0
        # 采集与过闸的三个数来自 `monitors/<id>/settings.yaml`（缺了就用 registry 的默认值）。
        window_hours = int(settings.value(monitor, "window_hours"))
        slice_hours = int(settings.value(monitor, "slice_hours"))
        for query in settings.value(monitor, "queries"):
            if flag is not None and flag.is_set():
                raise RuntimeError("scan cancelled")
            result = xread.search_sliced(query, window_hours=window_hours,
                                         slice_hours=slice_hours, cancel=flag)
            rows.extend(result["rows"])
            calls += int(result.get("slices") or 0)
            failed += len(result.get("failed") or [])
        # The same post can be returned by two queries in one sweep.
        unique, seen = [], set()
        for row in rows:
            key = str(getattr(row, "id", "") or "")
            if key and key not in seen:
                seen.add(key)
                unique.append(row)
        counters["calls"] = calls
        counters["posts"] = len(unique)
        counters["failed"] = failed

        _save(scan, stage="gate", starts=starts)
        fresh: list[dict] = []
        floor = int(settings.value(monitor, "min_score"))
        stamped = _stamp()
        for row in unique:
            verdict = rules.judge(str(getattr(row, "text", "") or ""),
                                  author=str(getattr(row, "handle", "") or ""),
                                  metrics={"likes": getattr(row, "likes", 0),
                                           "retweets": getattr(row, "retweets", 0),
                                           "replies": getattr(row, "replies", 0),
                                           "views": getattr(row, "views", 0)},
                                  created_at=getattr(row, "created_at", None),
                                  min_score=floor)
            if not verdict["pass"]:
                continue
            fresh.append({
                "tweet_id": str(getattr(row, "id", "") or ""),
                "author": str(getattr(row, "handle", "") or ""),
                "created_at": str(getattr(row, "created_at", None) or ""),
                "text": str(getattr(row, "text", "") or ""),
                "score": int(verdict.get("score") or 0),
                "first_seen": stamped,
            })
        counters["gated"] = len(fresh)
        # 帖子先入队：这一轮读过的下一轮不会再读，只有真正的新帖子才占抽取名额。
        counters["queued"] = db.monitor_post_add(monitor.id, fresh)

        _save(scan, stage="extract", starts=starts)
        pending = db.monitor_posts_pending(monitor.id, monitor.max_posts * MAX_BATCHES)
        payload = _post_payload(pending)
        projects, batches, failed, successful = (
            _extract(monitor, payload, scan["id"]) if payload else ([], 0, 0, [])
        )
        counters["retired"] = _retire_batches(
            monitor.id, pending, successful, monitor.max_posts
        ) if payload else 0
        counters["batches"] = batches
        counters["extract_failed"] = failed
        counters["posts_sent"] = len(payload)
        counters["queue"] = len(pending)
        counters["projects"] = len(projects)

        _save(scan, stage="trace", starts=starts)
        known = _known()
        traced: list[dict] = []
        levels = {"A": 0, "B": 0, "C": 0}
        skipped_items: list[dict] = []
        trace_failed = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_trace_one, project, known): project
                       for project in projects}
            for future in as_completed(futures):
                project = futures[future]
                try:
                    answer = future.result()
                except Exception:  # noqa: BLE001 - one unreadable home must not drop the sweep
                    trace_failed += 1
                    traced.append(project)
                    continue
                level = str(answer.get("level") or "C")
                levels[level] = levels.get(level, 0) + 1
                if level == "C":
                    # 诊断用：为什么落 C —— 找到站点没有、读到页面没有、二次搜索跑没跑。
                    if len(skipped_items) < 10:
                        skipped_items.append({
                            "title": str(project.get("title") or ""),
                            "handle": str(project.get("handle") or ""),
                            "site": str(answer.get("site") or ""),
                            "pages": len(answer.get("pages") or []),
                            "searched": bool(answer.get("searched")),
                        })
                    continue
                project["official_url"] = answer.get("site") or project.get("official_url")
                project["official_account"] = answer.get("account") or project.get("handle") or ""
                project["trust"] = answer.get("trust") or "unjudged"
                project["trust_basis"] = answer.get("basis") or ""
                project["sources"] = answer.get("sources") or []
                project["pages"] = answer.get("pages") or []
                # 官网统一走 https（trace 给的是它读到的 scheme，http://danta.ai/）。
                project["official_url"] = _secure(str(project.get("official_url") or ""))
                project["site_from_profile"] = bool(answer.get("site_from_profile"))
                for source in project["sources"]:
                    if isinstance(source, dict) and source.get("kind") == "official_site":
                        source["url"] = _secure(str(source.get("url") or ""))
                # 静态 HTML 只有几百字节说明这站是客户端渲染的（HODLER 的 Minted 650 / 7,221
                # 就只在渲染后的页面里），这时再用浏览器读一次（2026-10-30）。
                # 计数常常只在 mint 子页上（Tiny Farm 的「469 / 1000」在 /mint，首页一个数都没有），
                # 而且都是客户端渲染的，所以首页和常见的 mint 路径都用浏览器读一遍（2026-10-30）。
                if project.get("official_url"):
                    from ...sources import browser
                    base = str(project["official_url"]).rstrip("/")
                    for suffix in ("", "/mint", "/mint.html"):
                        try:
                            rendered = browser.render(base + suffix)
                        except Exception:  # noqa: BLE001
                            continue
                        if rendered:
                            project["pages"].append(rendered)
                traced.append(project)
        counters["traced"] = levels
        counters["skipped"] = levels.get("C", 0)
        counters["skipped_items"] = skipped_items
        counters["trace_failed"] = trace_failed

        # 判定吃抽取阶段（帖子）的摘要，展示文案才用官网重写：先把原摘要留一份
        # （2026-10-30）。
        for project in traced:
            project["summary_extract"] = str(project.get("summary") or "")

        # Deterministic readings run before the model. Chain probing below can replace them.
        for project in traced:
            parsed = chain.parse_mint("\n".join(str(page) for page in project.get("pages") or []))
            if parsed:
                done, total = parsed.get("done"), parsed.get("total")
                if done is not None:
                    project["mint_done"] = done
                if total:
                    project["mint_total"] = total
                    project["mint_state"] = "live" if (done or 0) < total else "sold_out"
                elif done:
                    # 只有分子（`3,073 cards claimed`）：说明已经开卖，上限还没读到。
                    project["mint_state"] = "live"

        # 链上：合约地址来自模型给的字段、摘要 / 事实，或溯源读到的官网正文。没有地址就不查。
        _save(scan, stage="chain", starts=starts)
        probed = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for project in traced:
                candidates = []
                ca = chain.first_address(
                    str(project.get("contract") or ""),
                    str(project.get("summary") or ""),
                    " ".join(str(item) for item in project.get("facts") or []),
                    *[str(page) for page in project.get("pages") or []])
                if ca:
                    candidates.append(ca)
                # 页面上常同时印着 mint pass / forge / emitter / 代币：都收进来，交给
                # `token_first` 逐个验证，别只信第一个（2026-10-30）。
                for addr in re.findall(r"0x[0-9a-fA-F]{40}",
                                       "\n".join(str(page) for page in project.get("pages") or [])):
                    addr = addr.lower()
                    if addr not in candidates and addr not in chain.INFRA:
                        candidates.append(addr)
                        if len(candidates) >= 8:
                            break
                if not ca and project.get("official_url"):
                    # 官网正文把地址缩写成「0xDd8B … 0ACf」，整条地址只在原始 HTML / 浏览器链接里，
                    # 所以缺地址时再读一次原始页面（2026-10-30）。
                    try:
                        raw = trace_mod.read_site(str(project["official_url"]), raw=True)
                        for addr in re.findall(r"0x[0-9a-fA-F]{40}", raw):
                            addr = addr.lower()
                            if addr not in candidates and addr not in chain.INFRA:
                                candidates.append(addr)
                    except Exception:  # noqa: BLE001
                        pass
                if candidates:
                    futures[pool.submit(_probe_first, candidates,
                                        str(project.get("chain") or ""))] = project
            for future in as_completed(futures):
                project = futures[future]
                try:
                    project["chain_info"] = future.result()
                    probed += 1
                except Exception:  # noqa: BLE001 - an unanswered chain is a gap, not a failure
                    log.exception("monitor: chain probe failed")
        counters["probed"] = probed

        # Chain facts now own contract / progress / token supply. One page call only adds
        # the prose and the fields the chain cannot answer.
        for project in traced:
            _apply_chain_info(project)
        filled = rewritten = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(
                _page_card, monitor, project, run_id=scan["id"]): project
                for project in traced if project.get("pages")}
            for future in as_completed(futures):
                project = futures[future]
                try:
                    answer = future.result()
                except Exception:  # noqa: BLE001 - a failed page call keeps extracted text
                    log.exception("monitor: page extraction failed")
                    continue
                if any(not _blank(answer.get(key)) for key in PAGE_FIELDS):
                    filled += 1
                if answer.get("summary"):
                    rewritten += 1
                _apply_page_card(project, answer)
        counters["page_llm"] = len(futures)
        counters["filled"] = filled
        counters["rewritten"] = rewritten
        for project in traced:
            if not project.get("domain"):
                project["domain"] = _domain_of(project)

        _save(scan, stage="store", starts=starts)
        written = 0
        merged = 0
        touched: set[str] = set()
        # 上一轮的链上那组用来判撤池：池子在不在、流动性掉了多少（2026-10-30）。
        before = {row["slug"]: row for row in db.monitor_cards(monitor.id)}
        no_site = 0
        no_anchor = 0
        no_domain = 0
        for project in traced:
            # 项目自己的正文必须在加密 / AI / Agent 领域里；品牌拿 Agent ID 做一次 mint 不算
            # （neonoir，2026-11-01, user）。
            if str(project.get("domain") or "") == "other":
                no_domain += 1
                continue
            # 一张卡必须有官网：只有社区号、没有站的项目不建卡（Polka Agents，2026-10-30, user）。
            if not _has_site(project):
                no_site += 1
                continue
            # 官网正文得有加密锚点，或拿得出链上证据；模型摘要不算证据（prismshrimp）。
            if not _credible(project, project.get("chain_info") or {}):
                no_anchor += 1
                continue
            project["type"] = _type(project.get("chain_info") or {},
                                    {**project, "summary": project.get("summary_extract") or ""})
            card = _card(monitor, project, scan["id"], before)
            if not card["title"]:
                continue
            site_title = str(card.pop("_site_title", "") or "")
            head = str(card.pop("_absorb", "") or "")
            if head and head in before:
                # The same project from another post updates its card; it does not become one.
                folded = _absorb(before[head], card)
                _settle_title(folded, before[head], site_title)
                db.monitor_card_put(monitor.id, folded, scan["id"])
                before[head] = folded
                touched.add(head)
                merged += 1
                continue
            _settle_title(card, before.get(card["slug"]), site_title)
            db.monitor_card_put(monitor.id, card, scan["id"])
            # Two new posts for one project in the same sweep: the second sees the first.
            before[card["slug"]] = card
            touched.add(card["slug"])
            written += 1
        counters["written"] = written
        counters["merged"] = merged
        counters["skipped_no_site"] = no_site
        counters["skipped_no_anchor"] = no_anchor
        counters["skipped_no_domain"] = no_domain
        _save(scan, stage="refresh", starts=starts)
        # 官网刷新是每轮的一段：本轮刚写的卡跳过；页面没变的卡只记时间，不调模型。
        refreshed = unchanged = skipped = 0
        cards = [card for card in db.monitor_cards(monitor.id) if card["slug"] not in touched]
        if cards:
            with ThreadPoolExecutor(max_workers=REFRESH_WORKERS) as pool:
                futures = [pool.submit(_refresh_card, monitor, card) for card in cards
                           if str(card.get("official_url") or "").strip()]
                for future in as_completed(futures):
                    try:
                        status = future.result()
                    except Exception:  # noqa: BLE001 - one bad site must not fail the sweep
                        log.exception("monitor: site refresh failed")
                        continue
                    if status == "changed":
                        refreshed += 1
                    elif status == "unchanged":
                        unchanged += 1
                    else:
                        skipped += 1
        counters["refreshed"] = refreshed
        counters["unchanged"] = unchanged
        counters["refresh_skipped"] = skipped
        counters["agent_checked"] = _agent_backfill(monitor, db.monitor_cards(monitor.id))
        # 队列是工作集，不是档案：只留 30 天（2026-10-30, user）。
        counters["pruned"] = db.monitor_posts_prune(
            monitor.id, (_now() - timedelta(days=30)).isoformat(timespec="seconds"))
        _finish(scan, "done", starts=starts)
    except RuntimeError as exc:
        if "cancel" in str(exc):
            _finish(scan, "cancelled", starts=starts)
        else:
            log.exception("monitor: sweep failed")
            _finish(scan, "failed", str(exc), starts)
    except Exception as exc:  # noqa: BLE001 - the scan row must say why it stopped
        log.exception("monitor: sweep failed")
        _finish(scan, "failed", str(exc) or exc.__class__.__name__, starts)
