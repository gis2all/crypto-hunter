"""How full the bar is.

A round is not seven equal steps. On this chain the source pull is about two minutes, the
safety gate (every survivor asked of the chain) is three quarters of the round, scoring is
arithmetic, and writing is the model answering. A bar that counted phases equally would sit
still for twenty minutes and then jump - which is the same thing as no bar.

So each phase owns a band of the bar, and inside a phase the band fills with the work
actually finished (sources pulled, candidates judged, batches written). The bands are shares
of a round's wall clock measured on this chain; `decide` logs the real split every round, so
the next change to these numbers can cite a measurement instead of a feeling.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("crypto-hunter.pipeline")

#: phase -> (low, high) percent, and the words the run panel already uses for that row.
PHASES: dict[str, tuple[int, int, str]] = {
    "pull": (0, 8, "拉取扫描源"),
    # Between the two halves of the hard filter: the per-token reads that fill the chip
    # numbers the bulk pull left blank. A real phase on this chain - minutes of network
    # wait that used to be absorbed into the gate - so it gets its own band rather than
    # letting the bar sit still and then jump.
    "topup": (8, 14, "补齐链上数据"),
    "gate": (14, 82, "安全判定"),
    "score": (82, 86, "打分排序"),
    "draft": (86, 100, "生成报告")
}


def label(phase: str) -> str:
    return PHASES.get(phase, (0, 0, ""))[2]


def pct(phase: str, done: float, total: float) -> int:
    """Where the bar sits: the phase's band, filled by the work done inside it."""
    low, high, _words = PHASES.get(phase, (0, 0, ""))
    if not total:
        return high
    share = max(0.0, min(1.0, float(done) / float(total)))
    return int(low + round((high - low) * share))


class Timer:
    """Wall clock per phase, logged once per round.

    The bar's bands are only defensible if someone checks them, and the log is the only
    place a round's shape survives: the database keeps what the round decided, not how long
    each part of it took.
    """

    def __init__(self, run_id: str,
                 on_span: Callable[[str, float], None] | None = None) -> None:
        self.run_id = run_id
        self.spans: list[tuple[str, float]] = []
        self._last = time.monotonic()
        self.on_span = on_span

    def mark(self, phase: str) -> None:
        now = time.monotonic()
        seconds = now - self._last
        self.spans.append((phase, seconds))
        self._last = now
        if self.on_span:
            self.on_span(phase, seconds)

    def log_line(self) -> str:
        parts = " ".join("%s=%.0fs" % (name, secs) for name, secs in self.spans)
        return "round %s phase timings: %s" % (self.run_id, parts)

    def emit(self) -> None:
        if self.spans:
            log.info(self.log_line())
