"""Each monitor on its own clock (2026-10-29).

The shape is the opportunity line's: `anchor + k x interval` in Asia/Shanghai, and a slot counts
as used once any scan of that monitor started at or after it. Monitors are offset from the
opportunity clock by default (01:15) so two sweeps do not ask X at the same minute.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ...store import db
from . import job, settings
from ..registry import MONITORS

log = logging.getLogger("crypto-hunter.monitors")

TZ = ZoneInfo("Asia/Shanghai")
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
    return anchor + step * int((now - anchor) / step)


def due(monitor_id: str, now: datetime | None = None) -> datetime | None:
    """The slot this monitor owes, or None when there is nothing to do."""
    monitor = MONITORS.get(monitor_id)
    if monitor is None:
        return None
    if not settings.value(monitor, "enabled", True):
        return None
    # 项目开着但计划任务关着：不按点跑，手动「立即运行」照常。
    if not settings.value(monitor, "schedule", True):
        return None
    at = slot(now or _now(), settings.value(monitor, "start_hour", monitor.start_hour),
              settings.value(monitor, "start_minute", monitor.start_minute),
              settings.value(monitor, "interval_hours", monitor.interval_hours))
    if at is None:
        return None
    latest = db.monitor_scan_latest(monitor_id)
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
                return None
    return at


def tick(now: datetime | None = None) -> list[str]:
    """One turn of the clock. Returns the monitors this turn started."""
    started: list[str] = []
    for monitor_id in MONITORS:
        try:
            at = due(monitor_id, now)
            if at is None or job.running(monitor_id):
                continue
            job.start(monitor_id, trigger="auto")
            log.info("monitor: %s auto sweep started for slot %s", monitor_id, at)
            started.append(monitor_id)
        except Exception:  # noqa: BLE001 - one broken monitor must not stop the others
            log.exception("monitor: %s auto tick failed", monitor_id)
    return started


def _loop(stop: threading.Event) -> None:
    while not stop.wait(TICK_SECONDS):
        tick()


def start() -> None:
    global _thread, _stop
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_loop, args=(_stop,), name="monitor-clock",
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
