"""The clock the opportunity scan never had (2026-10-28, user).

Arc already runs its chains on a grid: every three hours from 02:00. The opportunity scan was
manual-only, so "nothing new today" and "nobody pressed the button" looked the same. This module
adds the same shape - interval + anchor - with one rule the chains do not need:

**Yield instead of collide.** The anchor is 02:30 so the two tight slots (02:30 / 10:30) sit half
an hour away from the chains' :00 grid, but a chain round can still be running when the slot
comes. The ticker then leaves the slot alone and looks again a minute later; it never starts a
scan beside a round, and never spends an X call while the pipeline is reading the same hosts.

Slots are `anchor + k x interval` in Asia/Shanghai, and a slot counts as used when *any* scan
started at or after it - a manual run is not something the clock should duplicate.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..pipeline import schedule as pipeline_schedule
from ..store import db
from . import job, settings

log = logging.getLogger("crypto-hunter.opportunity")

TZ = ZoneInfo("Asia/Shanghai")
#: One minute is enough: a scan is nineteen, the grid moves in hours.
TICK_SECONDS = 60

_thread: threading.Thread | None = None
_stop: threading.Event | None = None
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(TZ)


def slot(now: datetime, hour: int, minute: int, interval_hours: int) -> datetime | None:
    """The latest scheduled slot at or before `now`. None when the clock is off."""
    if interval_hours <= 0:
        return None
    anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    step = timedelta(hours=interval_hours)
    steps = int((now - anchor) / step)
    return anchor + step * steps


def due(now: datetime | None = None) -> tuple[bool, datetime | None]:
    """Ready when the clock is on and no scan has started since the slot."""
    interval = int(settings.value("auto_interval_hours",
                                  settings.default("auto_interval_hours")))
    hour = int(settings.value("auto_start_hour", settings.default("auto_start_hour")))
    minute = int(settings.value("auto_start_minute",
                                settings.default("auto_start_minute")))
    moment = now or _now()
    at = slot(moment, hour, minute, interval)
    if at is None:
        return False, None
    latest = db.opportunity_scan_latest()
    started = str((latest or {}).get("started_at") or "")
    if started:
        try:
            last = datetime.fromisoformat(started)
        except ValueError:
            last = None
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=TZ)
            if last >= at:
                return False, at
    return True, at


def tick(now: datetime | None = None) -> bool:
    """One turn of the clock. True when it started a scan."""
    ready, at = due(now)
    if not ready:
        return False
    if job.running():
        return False
    busy = pipeline_schedule.busy_chains()
    if busy:
        # The chains are reading the same hosts; this slot waits for them (a minute at a time).
        log.info("opportunity: auto slot %s waits for %s", at, ",".join(sorted(busy)))
        return False
    job.start(trigger="auto")
    log.info("opportunity: auto scan started for slot %s", at)
    return True


def _loop(stop: threading.Event) -> None:
    while not stop.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:  # noqa: BLE001 - a broken tick must not kill the clock
            log.exception("opportunity: auto-run tick failed")


def start() -> None:
    """Start the ticker once; a second call is a no-op."""
    global _thread, _stop
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_loop, args=(_stop,), name="opportunity-clock",
                                   daemon=True)
        _thread.start()


def stop() -> None:
    global _thread, _stop
    with _lock:
        if _stop is not None:
            _stop.set()
        if _thread is not None:
            _thread.join(timeout=2)
        _thread, _stop = None, None
