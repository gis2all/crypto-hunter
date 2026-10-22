"""X sessions: one per account, shared by every lane (2026-10-28, user).

One cookie meant one lane. X paces per account, so N sessions are N lanes - but only if nothing
else in the process is spending the same one at the same time. This module is that "nothing
else": every call asks the pool for a session, gets it exclusively, and returns it. Per-session
spacing (`x_interval`) is applied at hand-out, so two lanes never share one account's rate limit.

Health is a reading, not an exception. A 429 rests that session for ten minutes and the slice is
retried on another lane; a 401 turns it off until the file changes; anything else is a failure
count. Only when no session can answer at all does the caller see an error.

Where they come from: `config/system.yaml`. `x.cookies` is the list - every entry a peer, in the
order the file has them. The legacy `x.auth_token` / `x.ct0` pair is read only when that list is
empty, as one session called `main`, so an old file keeps working. The file is re-read when its
mtime moves, so adding a cookie needs no restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import yaml

from .. import config

log = logging.getLogger("crypto-hunter.x")

DEFAULT_INTERVAL = 5.0
COOLDOWN_SECONDS = 600.0
FAILURE_COOLDOWN = 120.0
MAX_FAILURES = 3


@dataclass
class Session:
    """One account's session, and the numbers that say whether it still works."""

    label: str
    token: str
    ct0: str
    enabled: bool = True
    cooldown_until: float = 0.0
    failures: int = 0
    calls_ok: int = 0
    calls_failed: int = 0
    rate_limited: int = 0
    busy: bool = False
    next_at: float = 0.0
    last_used_at: float = 0.0
    note: str = ""

    def usable(self, now: float, allow_cooling: bool = False) -> bool:
        return self.enabled and not self.busy and (allow_cooling or self.cooldown_until <= now)


class Pool:
    """The process's sessions: load, hand out, record health."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._sessions: list[Session] = []
        self._signature: tuple = ()
        self._interval = DEFAULT_INTERVAL
        self._high_waiting = 0

    def _signature_of_file(self) -> tuple:
        path = config.SYSTEM_PATH
        try:
            stat = path.stat()
            return (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (str(path), 0, 0)

    def _parse(self, document: dict[str, Any]) -> list[Session]:
        block = document.get("x") if isinstance(document.get("x"), dict) else {}
        out: list[Session] = []
        # 三条 cookie 是平级的（2026-10-28, user）：写在 x.cookies 里就是一个列表，
        # 不再有「第一对特殊、后面是附加」的结构。旧的 x.auth_token / x.ct0 只在没有列表时
        # 兜底，读成一条 main，老配置不用改也能跑。
        listed = block.get("cookies") if isinstance(block.get("cookies"), list) else []
        for index, item in enumerate(listed):
            if not isinstance(item, dict):
                continue
            token = str(item.get("auth_token") or "").strip()
            ct0 = str(item.get("ct0") or "").strip()
            if not token or not ct0:
                continue
            label = str(item.get("label") or "cookie%d" % (index + 1)).strip()
            out.append(Session(label, token, ct0, enabled=bool(item.get("enabled", True))))
        if not out:
            token = str(block.get("auth_token") or "").strip()
            ct0 = str(block.get("ct0") or "").strip()
            if token and ct0:
                out.append(Session("main", token, ct0))
        seen: set[str] = set()
        unique: list[Session] = []
        for session in out:
            if session.token in seen:
                continue
            seen.add(session.token)
            unique.append(session)
        return unique

    def _refresh(self) -> None:
        """Re-read the file when it moved; keep health for labels that are still there."""
        signature = self._signature_of_file()
        if signature == self._signature and self._sessions:
            return
        self._signature = signature
        document: dict[str, Any] = {}
        try:
            if config.SYSTEM_PATH.is_file():
                document = yaml.safe_load(config.SYSTEM_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            document = {}
        if not isinstance(document, dict):
            document = {}
        loaded = self._parse(document)
        if not loaded:
            token = os.environ.get("CH_X_AUTH_TOKEN", "").strip()
            ct0 = os.environ.get("CH_X_CT0", "").strip()
            if token and ct0:
                loaded = [Session("env", token, ct0)]
        old = {session.label: session for session in self._sessions}
        for session in loaded:
            previous = old.get(session.label)
            if previous is not None and previous.token == session.token:
                session.calls_ok = previous.calls_ok
                session.calls_failed = previous.calls_failed
                session.rate_limited = previous.rate_limited
                session.failures = previous.failures
                session.cooldown_until = previous.cooldown_until
                # The file wins on `enabled`: a 401 stops a session until the file changes, which
                # is what the log line promises. Cooldowns and counters are time-based and survive
                # an edit that has nothing to do with this session.
                if session.enabled and not previous.enabled:
                    session.note = ""
                session.last_used_at = previous.last_used_at
                session.next_at = previous.next_at
        self._sessions = loaded

    def set_interval(self, seconds: float) -> None:
        with self._cond:
            self._interval = max(0.0, float(seconds or DEFAULT_INTERVAL))

    def interval(self) -> float:
        return self._interval

    def acquire(self, priority: int = 1, timeout: float | None = None,
                allow_cooling: bool = False) -> Session:
        """One session, held until `release()`. Raises only when nothing can ever answer."""
        high = priority <= 0
        with self._cond:
            if high:
                self._high_waiting += 1
            try:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    self._refresh()
                    if not self._sessions:
                        raise RuntimeError(
                            "x search needs CH_X_AUTH_TOKEN and CH_X_CT0 (or x.cookies in "
                            "config/system.yaml)")
                    if not any(session.enabled for session in self._sessions):
                        raise RuntimeError("x 会话全部停用（401）：检查 config/system.yaml")
                    if not high and self._high_waiting:
                        # Pipeline / trace calls go first: they are one call, not six slices.
                        self._cond.wait(0.2)
                        continue
                    now = time.time()
                    usable = [session for session in self._sessions
                              if session.usable(now, allow_cooling)]
                    if usable:
                        session = min(usable, key=lambda item: (max(item.next_at, now),
                                                                item.last_used_at))
                        wait = session.next_at - now
                        if wait <= 0:
                            session.busy = True
                            session.next_at = now + self._interval
                            session.last_used_at = now
                            return session
                        self._cond.wait(min(wait, 1.0))
                        continue
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError("x 会话都在冷却或占用中")
                    soonest = min((session.cooldown_until for session in self._sessions
                                   if session.enabled), default=now + COOLDOWN_SECONDS)
                    self._cond.wait(max(0.5, min(30.0, soonest - now)))
            finally:
                if high:
                    self._high_waiting -= 1

    def release(self, session: Session) -> None:
        with self._cond:
            session.busy = False
            self._cond.notify_all()

    def ok(self, session: Session) -> None:
        with self._cond:
            session.calls_ok += 1
            session.failures = 0

    def rate_limited(self, session: Session) -> None:
        with self._cond:
            session.calls_failed += 1
            session.rate_limited += 1
            session.cooldown_until = time.time() + COOLDOWN_SECONDS
            session.note = "429 · 冷却 10 分钟"
            log.info("x: session %s rate limited, resting %.0fs", session.label,
                     COOLDOWN_SECONDS)

    def dead(self, session: Session) -> None:
        with self._cond:
            session.calls_failed += 1
            session.enabled = False
            session.note = "401 · 已停用"
            log.warning("x: session %s refused (401), disabled until the file changes",
                        session.label)

    def failed(self, session: Session) -> None:
        with self._cond:
            session.calls_failed += 1
            session.failures += 1
            if session.failures >= MAX_FAILURES:
                session.cooldown_until = time.time() + FAILURE_COOLDOWN
                session.note = "连续 %d 次失败 · 冷却 2 分钟" % session.failures

    def snapshot(self) -> list[dict]:
        with self._cond:
            self._refresh()
            now = time.time()
            return [{"label": session.label, "enabled": session.enabled,
                     "usable": session.usable(now),
                     "cooling": session.cooldown_until > now,
                     "calls_ok": session.calls_ok, "calls_failed": session.calls_failed,
                     "rate_limited": session.rate_limited, "note": session.note}
                    for session in self._sessions]

    def count(self) -> int:
        with self._cond:
            self._refresh()
            now = time.time()
            return sum(1 for session in self._sessions if session.usable(now))

    def first(self, allow_cooling: bool = True) -> Session | None:
        with self._cond:
            self._refresh()
            for session in self._sessions:
                # A session a lane is holding is not the one to spend on a probe: two calls on one
                # account at once is exactly what the pool exists to prevent.
                if (session.enabled and not session.busy
                        and (allow_cooling or session.cooldown_until <= time.time())):
                    return session
            return None


_POOL = Pool()


def pool() -> Pool:
    return _POOL


def classify(detail: str) -> str:
    """What one CLI failure means for the session that ran it."""
    text = str(detail or "").lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate"
    if "401" in text or "unauthorized" in text or "could not authenticate" in text \
            or "cookie" in text:
        return "dead"
    return "other"
