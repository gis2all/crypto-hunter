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

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..llm import client as llm
from ..store import db
from . import heat as heat_scale
from . import settings
from . import topic as topics
from . import model, probe, promote, trust
from .schema import EXTRACT_VERSION, UNJUDGED, Entry, read_entries, slug_for

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


def extract(material: dict) -> tuple[list[Entry], list[str]]:
    """One material, one question, one repair at most."""
    chars = model.POST_CHARS if material.get("source_kind") == "post" else model.PAGE_CHARS
    value, issues = model.question("extract.md", EXTRACT_VERSION, "opportunity_extract",
                                   material, read_entries, chars=chars, repair=True)
    entries = [item for item in (value or []) if isinstance(item, Entry)]
    return entries, issues


def _store(material: dict, entries: list[Entry], issues: list[str], counters: dict) -> None:
    """Judge and write one material. Called only on the coordinator thread."""
    counters["materials"] += 1
    counters["issues"] += len(issues)
    source = _source_row(material.get("source_target"))
    trace_level, trace_basis = _trace_of(material)
    # 热度是来源帖自己的互动量，所以每条条目从它读到的那条材料上取；页面材料没有
    # 互动读数，heat 就是 None（未判定），不是 0。
    heat = heat_score(material.get("metrics"))
    heat_basis = heat_basis_text(material.get("metrics"))
    # The pages the trace already read answer the chain for free; everything else is the补链
    # stage's job, after the card (and its own title) exists.
    fallback = probe.from_pages([page["text"] for page in _pages_of(material)])
    for entry in entries:
        slug = slug_for(entry)
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
        row = {
            "slug": slug,
            "kind": entry.kind,
            "category": entry.category,
            "title": entry.title,
            "organizer": entry.organizer,
            "official_url": entry.official_url,
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
        }
        steps = [step.model_dump() for step in entry.steps]
        db.opportunity_put(row, steps)
        counters["entries"] += 1
        counters["promoted" if status == promote.PROMOTED else "candidate"] += 1
    # 主题代表的那条帖与它的同组帖一起标记：否则下一轮会把它们各自再出一张卡，
    # 正是「一个项目七张卡」的成因（user, 2026-10-19）。
    for ref in topics.consumed_refs(material):
        db.opportunity_mark(ref, "post", extracted=True)


def run(limit: int | None = None, tell=None) -> dict:
    """Read, judge and store. Model calls overlap; the coordinator owns every write."""
    counters = {"materials": 0, "entries": 0, "promoted": 0, "candidate": 0, "issues": 0,
                "frozen": 0}
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
            _store(material, entries, issues, counters)
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


def _source_row(target: str | None) -> dict | None:
    """The watchlist row a material came from, when it is still on the list."""
    if not target:
        return None
    for row in db.opportunity_sources():
        if row["target"] == target:
            return row
    return None
