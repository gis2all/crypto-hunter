"""How full the bar is.

A round is not five equal steps. The source pull is seconds to a minute, the per-token
top-up and the safety gate are network waits, and the writing is the model answering - so
the writing is about half a round and the gate about a fifth. A bar that counted phases
equally would sit still and then jump - which is the same thing as no bar.

So each phase owns a band of the bar, and inside a phase the band fills with the work
actually finished (sources pulled, candidates judged, entries written). The bands are
shares of a round's wall clock measured on both chains (12.27), and they run in the order
the round walks, so the bar never has to walk backwards. `decide` logs the real split every
round, so the next change to these numbers can cite a measurement instead of a feeling.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("crypto-hunter.pipeline")

#: phase -> (low, high) percent, and the words the run panel already uses for that row.
PHASES: dict[str, tuple[int, int, str]] = {
    "pull": (0, 19, "拉取扫描源"),
    # Between the two halves of the hard filter: the per-token reads that fill the chip
    # numbers the bulk pull left blank. A real phase on this chain - minutes of network
    # wait that used to be absorbed into the gate - so it gets its own band rather than
    # letting the bar sit still and then jump.
    "topup": (19, 30, "补齐链上数据"),
    "gate": (30, 51, "安全判定"),
    # The writing owns the biggest band: it is the only pass that waits on a search plus a
    # model answer per token, and it runs after the filter, so its band has to be wide or
    # the bar sits on one number for minutes.
    "draft": (51, 96, "生成报告"),
    "score": (96, 100, "打分排序")
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
