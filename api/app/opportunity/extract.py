"""The expensive pass: one material read into entries, then judged and stored.

Three things happen per entry here and nowhere else: it is given a stable slug, it is told how
official it is (`trust.py`), and it is told whether it is on the list (`promote.py`). All three
are judgements about a material that was already read, which is why they live beside the
extraction rather than in the page: the page reads rows, it does not decide anything.

A material that yields nothing is marked read all the same. The alternative - leaving it for the
next scan - re-asks the most expensive question in the module about the material that has the
least to say, every scan, forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..llm import client as llm
from ..store import db
from . import heat as heat_scale
from . import settings
from . import topic as topics
from . import model, probe, promote, rules, trust
from .schema import (EXTRACT_VERSION, UNJUDGED, Entry, event_key, project_key,
                     read_entries, slug_for)

log = logging.getLogger("crypto-hunter.opportunity")

#: The two names the heat scale is called by here, so the import above reads as a module and
#: the call sites read as the thing being computed.
heat_score = heat_scale.score
heat_basis_text = heat_scale.basis

#: How many materials one scan will extract. The pass costs a call per material plus a repair
#: call for the ones that come back broken, so it is capped well below the classification pass.
#: 40 since 2026-10-19: the rebuild left ~200 classified topics waiting, and twelve a scan meant
#: seventeen scans to drain - long enough that the user noticed a project "disappearing" (it was
#: queued, not lost). The topic layer makes each unit one project rather than one post, so the same
#: number covers more ground than it used to.
MAX_EXTRACT = 40

#: Four calls overlap, matching the width the classification pass already used. The model's
#: slow tail is what this hides: one call taking a minute no longer holds the next three topics.
EXTRACT_WORKERS = max(1, int(os.environ.get("CH_OPPORTUNITY_EXTRACT_WORKERS", "4") or 4))

#: 卡片级的非机会语汇（2026-10-29, user）。查询只过滤**帖子正文**，而卡是读过链接页之后才写
#: 的：一场访谈里的 AMA、项目页上的 referral party，正文里一个相关词都没有，`-AMA` 也拦不住。
#: 中文按子串、英文按整词匹配 —— 后者是为了不把 Panama 当成 AMA、不把 third party 当成派对。
CARD_NOISE = (
    "游戏", "链游", "手游", "派对",
    "gamefi", "game-fi", "gameplay", "play-to-earn", "play to earn", "p2e", "web3 game",
    "ama", "访谈", "播客", "直播",
    "interview", "podcast", "livestream",
)

#: 成对出现的说法：单独一个 party 太常见（third party），只有跟拉新绑在一起才是推荐派对。
CARD_NOISE_PAIRS = (
    ("referral", "party"), ("推荐", "派对"), ("邀请", "返佣"), ("affiliate", "program"),
)


def card_noise(*parts: str) -> str | None:
    """Which non-opportunity word a card's own text carries, if any."""
    text = " ".join(part for part in parts if part).lower()
    for word in CARD_NOISE:
        if any(ord(char) > 127 for char in word):
            if word in text:
                return word
        elif re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text):
            return word
    for left, right in CARD_NOISE_PAIRS:
        if left in text and right in text:
            return left + " " + right
    return None


def extract(material: dict) -> tuple[list[Entry], list[str]]:
    """One material, one question, one repair at most."""
    chars = model.POST_CHARS if material.get("source_kind") == "post" else model.PAGE_CHARS
    value, issues = model.question("extract.md", EXTRACT_VERSION, "opportunity_extract",
                                   material, read_entries, chars=chars, repair=True)
    entries = [item for item in (value or []) if isinstance(item, Entry)]
    return entries, issues


def _store(material: dict, entries: list[Entry], issues: list[str], counters: dict,
           scan: str | None = None) -> None:
    """Judge and write one material. Called only on the coordinator thread."""
    counters["materials"] += 1
    counters["issues"] += len(issues)
    # 抽取之前先问一句“这是不是加密内容”。闸已经挡过一层，这里仍要看材料本身：一个没有
    # 任何链/代币/钱包/合约/NFT/DeFi 锚点的帖子，不该被写成一机会（演出、SaaS、硬件、订阅）。
    entry_text = " ".join(
        " ".join([entry.title or "", entry.summary or "", entry.ecosystem or "",
                  entry.official_url or "", " ".join(step.body for step in entry.steps)])
        for entry in entries)
    context_text = " ".join([
        str(material.get("text") or ""),
        " ".join(str(page.get("text") or "") for page in _pages_of(material)),
        " ".join(str(url) for url in material.get("urls") or []),
        entry_text,
    ])
    # A known chain on the entry is itself an anchor, even when the material is a terse post.
    has_chain = any(str(entry.ecosystem or "").strip() not in ("", UNJUDGED)
                    for entry in entries)
    if not has_chain and not rules.crypto_context(context_text):
        counters["offtopic"] = counters.get("offtopic", 0) + 1
        for ref in topics.consumed_refs(material):
            db.opportunity_mark(ref, "post", extracted=True)
        return
    source = _source_row(material.get("source_target"))
    trace_level, trace_basis = _trace_of(material)
    payload = _trace_payload(material)
    official_source = (source or {}).get("kind") == "official"
    source_ref = material.get("url") or material.get("ref")
    existing_official = (db.opportunity_official_by_source_ref(source_ref)
                         if source_ref else None)
    # 热度是来源帖自己的互动量，所以每条条目从它读到的那条材料上取；页面材料没有
    # 互动读数，heat 就是 None（未判定），不是 0。
    heat = heat_score(material.get("metrics"))
    heat_basis = heat_basis_text(material.get("metrics"))
    # The pages the trace already read answer the chain for free; everything else is the补链
    # stage's job, after the card (and its own title) exists.
    fallback = probe.from_pages([page["text"] for page in _pages_of(material)])
    for entry in entries:
        # 卡片自己的文字里带着非机会语汇就不写：这类条目没有可执行的动作，留在列表里只会
        # 稀释真正能参与的机会（2026-10-29, user）。
        noise = card_noise(entry.title or "", entry.summary or "",
                           *(step.body for step in entry.steps))
        if noise:
            counters["noise"] = counters.get("noise", 0) + 1
            continue
        computed_slug = slug_for(entry)
        project = topics.normalise(material.get("project") or material.get("project_name")
                                   or entry.organizer or entry.title)
        project_id = project_key(project)
        event_id = event_key(project, entry.kind, entry.title)
        existing_event = db.opportunity_by_event_key(event_id)
        existing = existing_official or existing_event
        slug = existing["slug"] if existing else computed_slug
        # 归档 / 删除的卡是读者冻结的：同一项目再有新材料也不覆盖它。
        if db.opportunity_state_of(slug) not in (None, "active"):
            counters["frozen"] += 1
            continue
        status, reason = promote.verdict(entry)
        how_official, basis = trust.verdict(entry, material, source)
        # 溯源的结果优先于 trust 自己的判断：A 级是"官网与官方号互证"，比"来自官方号"
        # 更硬；C 级是"没找到官方来源"，它不该出现在列表上（进未晋升池并写明理由）。
        if trace_level == "A":
            how_official, basis = "official", trace_basis or basis
        elif trace_level == "B":
            how_official, basis = "community", trace_basis or basis
        elif trace_level == "C":
            status, reason = "candidate", "no_official_source"
        official_account, official_url, sources = _card_sources(
            material, entry, payload, trace_level, official_source)
        if existing:
            was_official = how_official == "official"
            # The same post already produced an official card. Reuse it instead of creating a
            # second community card when a later search temporarily failed.
            if not was_official:
                how_official = "official"
                basis = existing.get("trust_basis") or basis
                if trace_level == "C":
                    status, reason = promote.verdict(entry)
            official_account = existing.get("official_account") or official_account
            official_url = existing.get("official_url") or official_url
            sources = _merge_sources(existing.get("sources_json"), sources,
                                     official_only=not was_official)
        row = {
            "slug": slug,
            "kind": entry.kind,
            "category": entry.category,
            "title": entry.title,
            "organizer": entry.organizer,
            "project_key": project_id,
            "event_key": event_id,
            "official_url": official_url,
            "official_account": official_account,
            "sources_json": json.dumps(sources, ensure_ascii=False),
            "summary": entry.summary,
            "trust": how_official,
            "trust_basis": basis,
            # 生态是**从材料读出来的**（抽取那一步的输出），不是从"谁发的帖"继承的：
            # 一个 Solana 账号宣布 Base 上的项目，条目就该写 base（2026-10-19, user）。
            # 来源类型仍是来源行的字段：它说的是"我们怎么找到它的"。
            "ecosystem": (entry.ecosystem if entry.ecosystem and entry.ecosystem != UNJUDGED
                          else fallback) or UNJUDGED,
            # 生态是从哪读出来的：有官方页就是官方来源，否则只有那条帖。
            "ecosystem_basis": ("官方来源" if material.get("official_pages") else "材料"),
            "source_type": (source or {}).get("kind"),
            "risk": entry.risk,
            "barrier": ",".join(entry.barrier),
            "maturity": entry.maturity,
            "started_at": entry.started_at,
            "deadline": entry.deadline,
            "status": status,
            "reason": reason,
            "heat": heat,
            "heat_basis": heat_basis,
            "discovered_at": db.now_iso(),
            "source_kind": material.get("source_kind"),
            # The URL, not the row key: a reader clicking through wants the post or the page,
            # and `extracted_at` - not this column - is what stops a second pass.
            "source_ref": material.get("url") or material.get("ref"),
            "extract_version": EXTRACT_VERSION,
            "novelty": entry.novelty or None,
            # 哪一轮动过这张卡：页面按它数「本轮更新」，与 `discovered_at`（哪一轮发现）分开。
            "updated_scan": scan,
        }
        steps = [step.model_dump() for step in entry.steps]
        db.opportunity_put(row, steps)
        if existing and computed_slug != slug:
            db.opportunity_delete_duplicate(computed_slug, source_ref)
        counters["entries"] += 1
        # 新增与更新分开数：`entries` 是「这一轮写了多少条」，这两个是其中各占多少。
        counted = "created" if existing is None else "updated"
        counters[counted] = counters.get(counted, 0) + 1
        counters["promoted" if status == promote.PROMOTED else "candidate"] += 1
    # 主题代表的那条帖与它的同组帖一起标记：否则下一轮会把它们各自再出一张卡，
    # 正是「一个项目七张卡」的成因（user, 2026-10-19）。
    for ref in topics.consumed_refs(material):
        db.opportunity_mark(ref, "post", extracted=True)


def run(limit: int | None = None, tell=None, scan: str | None = None) -> dict:
    """Read, judge and store. Model calls overlap; the coordinator owns every write."""
    counters = {"materials": 0, "entries": 0, "created": 0, "updated": 0,
                "promoted": 0, "candidate": 0, "issues": 0, "frozen": 0, "offtopic": 0,
                "noise": 0}
    # 一张卡 = 一个主题（user, 2026-10-19）：同一项目的多条帖合成一份材料，官方页附在后面。
    materials = topics.materials(
        limit=limit or settings.value("extract_per_scan", MAX_EXTRACT))
    total = len(materials) or 1
    done = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        futures = {pool.submit(extract, material): material for material in materials}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            material = futures[future]
            try:
                entries, issues = future.result()
            except llm.LLMUnavailable as exc:
                if not stopped:
                    stopped = True
                    log.info("opportunity: extraction stopped at %s: %r", material["ref"], exc)
                    for pending in futures:
                        pending.cancel()
                continue
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            _store(material, entries, issues, counters, scan)
            done += 1
            if tell:
                tell(done, total)
    return counters


def refresh_heat() -> int:
    """Recompute every entry's heat from its source post, under the current scale.

    Heat is derived (one post's metrics × one scale), so a change to the scale has to reach the
    rows that were written under the old one. This runs at the end of a scan, after the sweep -
    it is arithmetic over rows that already exist, and the alternative is a page where two
    entries' numbers were produced by two different definitions of the same word.
    """
    changed = 0
    for row in db.opportunity_heat_inputs():
        value = heat_scale.score(row["metrics"])
        text = heat_scale.basis(row["metrics"])
        db.opportunity_set_heat(row["slug"], value, text)
        changed += 1
    return changed


def _pages_of(material: dict) -> list[dict]:
    """The pages the trace read for this lead, as `[{url, text}]`. Empty when it read none."""
    import json

    raw = material.get("trace_json")
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    page_sources = payload.get("page_sources") or []
    if page_sources:
        return [{"url": str(page.get("url") or "溯源读到的官方页"),
                 "text": str(page.get("text") or "")}
                for page in page_sources if str(page.get("text") or "").strip()]
    pages = payload.get("pages") or []
    site = str(payload.get("site") or "")
    out: list[dict] = []
    for index, body in enumerate(pages):
        if not str(body or "").strip():
            continue
        out.append({"url": site if index == 0 and site else "溯源读到的官方页",
                    "text": str(body)})
    return out


def _trace_of(material: dict) -> tuple[str, str]:
    """The stored trace verdict, as `(level, basis)`. Empty when this lead was never traced."""
    import json

    raw = material.get("trace_json")
    if not raw:
        return str(material.get("trace_level") or ""), ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return str(material.get("trace_level") or ""), ""
    return str(payload.get("level") or ""), str(payload.get("basis") or "")


def _trace_payload(material: dict) -> dict:
    import json

    raw = material.get("trace_json")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _card_sources(material: dict, entry: Entry, payload: dict, level: str,
                  official_source: bool) -> tuple[str | None, str | None, list[dict]]:
    """The official account, primary URL, and readable source list for one card."""
    official = level == "A" or official_source
    account = str(payload.get("account") or entry.organizer or "").lstrip("@")
    site = str(payload.get("site") or entry.official_url or "").strip()
    out: list[dict] = []

    def add(kind: str, label: str, url: str) -> None:
        url = str(url or "").strip()
        if url and not any(item.get("url") == url for item in out):
            out.append({"kind": kind, "label": label, "url": url})

    if account and (official or level in ("B", "C")):
        add("official_account" if official else "candidate_account",
            "官方 X" if official else "候选 X（未互证）",
            "https://x.com/%s" % account)
    account_url = "https://x.com/%s" % account if account else ""
    for item in payload.get("sources") or []:
        kind = str(item.get("kind") or "")
        if kind in ("official_account", "candidate_account") and item.get("url") != account_url:
            continue
        if official:
            if kind in ("official_account", "candidate_account"):
                kind = "official_account"
            elif kind in ("official_site", "candidate_site"):
                kind = "official_site"
            elif kind in ("official_doc", "candidate_doc"):
                kind = "official_doc"
        add(kind, str(item.get("label") or "来源"), str(item.get("url") or ""))
    if site and official:
        add("official_site", "官网", site)
    original = str(material.get("url") or material.get("ref") or "")
    add("post", "原文", original)
    return (account if official else None,
            site if official else None,
            out)


def _merge_sources(raw: str | None, fresh: list[dict], *, official_only: bool = False) -> list[dict]:
    """Keep the official source list and add the new original-post link without duplicates."""
    try:
        old = json.loads(raw or "[]")
    except (TypeError, ValueError):
        old = []
    out = [item for item in old if isinstance(item, dict) and item.get("url")]
    for item in fresh:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        if official_only and item.get("kind") != "post":
            continue
        if not any(existing.get("url") == item["url"] for existing in out):
            out.append(item)
    return out


def _source_row(target: str | None) -> dict | None:
    """The watchlist row a material came from, when it is still on the list."""
    if not target:
        return None
    for row in db.opportunity_sources():
        if row["target"] == target:
            return row
    return None
