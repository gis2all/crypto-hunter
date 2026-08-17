"""One scan: the five stages, the row the page polls while they run, and the flag that stops it.

A scan is not a round. It belongs to no chain, no set of settings and no report library, so it
does not touch `runs` and does not queue behind one - the same argument `lookup.py` makes for its
own worker, and the reason this module carries its own single thread rather than borrowing the
scheduler's. The two can overlap, which is what the per-host pacers and this module's own X
throttle are for.

Six stages, and they are the pipeline's own vocabulary: read the watchlist, gather material, group
it into project candidates, trace their official homes, extract and store, then sweep (retire what
the material said was over, and recompute heat). The page draws them from the scan row it is
polling, so a scan that dies on the third stage keeps the first two stages' readings and says where
it stopped.

**Cancel.** The reader can stop a scan, and it stops at the next X call rather than at the end of
one: the flag is polled inside the subprocess loop (`xread.run`), so a scan that is waiting on a
rate limit stops waiting. Nothing is rolled back - what was read is stored, and the row says
`cancelled` rather than pretending it finished.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from ..llm import client as llm
from ..store import db
from . import extract, group, lifecycle, scan as scanner, trace, watchlist

log = logging.getLogger("crypto-hunter.opportunity")

#: The stages, in order, with the words the page prints.
STAGES = (
    ("sources", "读 watchlist"),
    ("collect", "抓材料"),
    ("classify", "分组"),
    ("trace", "溯源到官方家"),
    ("extract", "抽取与落库"),
    ("sweep", "生命周期"),
)

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opportunity")

#: The flag the running scan polls. One at a time by construction (`running()` refuses a second),
#: so one flag is enough - and it is keyed by scan id so a late cancel cannot stop the next one.
_CANCEL: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def _blank_stages() -> list[dict]:
    """The stage list as a scan starts: every stage waiting, none of them timed."""
    return [{"key": key, "label": label, "state": "pending", "seconds": None}
            for key, label in STAGES]


def start() -> dict:
    """Queue one scan and answer with the row the page watches.

    Seeding happens inside the sources stage rather than here: the page should count the
    one-time fill as part of reading the watchlist, not hide it before the scan starts.
    """
    job = {
        "id": uuid4().hex[:12],
        "status": "running",
        "stage": STAGES[0][0],
        "stages": _blank_stages(),
        "counters": {},
        "error": None,
        "started_at": db.now_iso(),
        "finished_at": None,
    }
    with _CANCEL_LOCK:
        _CANCEL[job["id"]] = threading.Event()
    db.opportunity_scan_put(job)
    submit(job)
    return job


def cancel(scan_id: str) -> dict:
    """Ask the running scan to stop. Answers with what happened, never raises.

    A cancel for a scan that already ended is not an error: the page may have been polling a row
    that finished between the click and the call, and "it is not running any more" is the answer
    the reader wanted.
    """
    with _CANCEL_LOCK:
        flag = _CANCEL.get(scan_id)
    if flag is None:
        return {"cancelled": False, "reason": "not running"}
    flag.set()
    return {"cancelled": True, "reason": "stopping at the next call"}


def submit(job: dict) -> None:
    """Hand a scan to the worker. Its own function so a test can drive one inline instead."""
    _worker.submit(drive, job)


def drive(job: dict) -> None:
    """Run a queued scan to the end, writing each stage down as it ends."""
    mark = time.monotonic()

    def say(key: str) -> None:
        nonlocal mark
        now = time.monotonic()
        for stage in job["stages"]:
            if stage["state"] == "run":
                stage["state"], stage["seconds"] = "done", round(now - mark, 2)
            if stage["key"] == key:
                stage["state"] = "run"
        mark = now
        job["stage"] = key
        db.opportunity_scan_put(job)

    try:
        run(job, say)
    except Cancelled:
        now = time.monotonic()
        for stage in job["stages"]:
            if stage["state"] == "run":
                stage["state"], stage["seconds"] = "done", round(now - mark, 2)
        job.update(status="cancelled", stage=None, finished_at=db.now_iso())
        db.opportunity_scan_put(job)
        log.info("opportunity: scan %s cancelled after %s", job["id"], job["counters"])
        return
    except Exception as exc:
        for stage in job["stages"]:
            if stage["state"] == "run":
                stage["state"] = "failed"
        job.update(status="failed", error=repr(exc)[:300], stage=None,
                   finished_at=db.now_iso())
        db.opportunity_scan_put(job)
        log.exception("opportunity: scan %s failed", job["id"])
        return
    finally:
        with _CANCEL_LOCK:
            _CANCEL.pop(job["id"], None)
    now = time.monotonic()
    for stage in job["stages"]:
        if stage["state"] == "run":
            stage["state"], stage["seconds"] = "done", round(now - mark, 2)
    job.update(status="done", stage=None, finished_at=db.now_iso())
    db.opportunity_scan_put(job)
    log.info("opportunity: scan %s done %s", job["id"], job["counters"])


def run(job: dict, say) -> None:
    """The body of one scan. Every stage's counters land on the scan row as it goes."""
    counters = job["counters"]

    say("sources")
    watchlist.ensure_seeded()
    sources = watchlist.enabled()
    counters["watchlist"] = len(sources)
    counters["accounts"] = len([row for row in sources if row["kind"] == "official"])
    counters["queries"] = len([row for row in sources if row["kind"] == "query"])

    say("collect")
    try:
        counters["collect"] = scanner.collect(
            job["id"], sources, tell=lambda done, total: _tick(job, "collect", done, total),
            cancel=_flag(job["id"]))
    except RuntimeError as exc:
        # The reader says "scan cancelled"; this layer decides that it is an outcome, not a
        # failure. Anything else it raises is a real error and keeps travelling.
        if "cancel" not in str(exc):
            raise
        raise Cancelled() from exc

    if not llm.configured():
        # 没配模型时仍然抓材料：材料本身有用，而"没配"必须写在扫描行上，不能表现成"什么都没发现"。
        counters["note"] = "未配置模型：只抓材料，未分组、未抽取"
        return

    say("classify")
    counters["classify"] = group.run(
        tell=lambda done, total: _tick(job, "classify", done, total))

    # 溯源在分组之后：名字是分组那一步给出的（模型读完整条帖），而第一版把它放在抓材料
    # 阶段用正则猜名字，八条线索全猜错、全部溯源失败（2026-10-19 实测）。
    say("trace")
    counters["trace"] = trace.run(
        tell=lambda done, total: _tick(job, "trace", done, total),
        known={str(row["target"]).lstrip("@").lower() for row in sources
               if row["kind"] == "official"},
        cancel=_flag(job["id"]),
        # 帖里既没有账号也没有链接时的那次搜索（user 补的那一步）。一片就够：问的是"现在谁在
        # 说这个名字"，而每一次调用都算进扫描的调用预算（`trace.run` 自己记）。
        search=scanner.look_up)

    say("extract")
    counters["extract"] = extract.run(
        tell=lambda done, total: _tick(job, "extract", done, total))

    # 生命周期最后一步，而且只在有材料之后：一个活动的结束时间是从它自己的材料里读出来的，
    # 所以这一趟必须先有材料、有抽取，才谈得上"到点了"。热度也在这一步重算：它是派生的，
    # 刻度改了就该跟着改，否则同一页上两个数字出自两套定义。
    say("sweep")
    counters["sweep"] = lifecycle.sweep()
    counters["sweep"]["heat_refreshed"] = extract.refresh_heat()


class Cancelled(Exception):
    """Raised when the reader stopped the scan. Not an error: a different outcome."""


def _flag(scan_id: str) -> threading.Event | None:
    with _CANCEL_LOCK:
        return _CANCEL.get(scan_id)


def _tick(job: dict, stage_key: str, done: int, total: int) -> None:
    """Write progress inside a stage, so a scan of a hundred sources is not a frozen row."""
    for stage in job["stages"]:
        if stage["key"] == stage_key:
            stage["done"], stage["total"] = done, total
    db.opportunity_scan_put(job)


def state(scan_id: str) -> dict | None:
    """One scan as the page polls it, or None when the id is not one this app made."""
    return db.opportunity_scan_get(scan_id)


def latest() -> dict | None:
    """The newest scan, so a page that did not start one still shows the last result."""
    return db.opportunity_scan_latest()


def running() -> bool:
    """Is a scan in flight? Asked before starting another one.

    Two scans at once would read the same timelines twice and spend two classification budgets
    on the same posts; the X throttle would serialise them anyway, so the second one would only
    arrive later and cost more.
    """
    scan = latest()
    return bool(scan and scan["status"] == "running")
