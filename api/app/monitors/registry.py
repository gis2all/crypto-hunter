"""The monitors this build knows: one directory per monitor, discovered at import time.

A monitor is a question the reader wants answered again and again - "which projects take a
Binance Agent ID right now" - not a single project. Each one lives in `monitors/<id>/`:

    <id>/monitor.py      the Monitor row (name, queries, card fields, defaults)
    <id>/prompts/*.md    its extraction prompt, and the fill / summary prompts it uses
    <id>/settings.yaml   its queries, switches, clock and 采集与过闸 numbers

Adding a monitor is then a folder, not an edit to this file (2026-10-30, user). The engine
lives in `monitors/core/` and is shared, so a new project does not copy a sweep implementation.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

#: `monitors/` itself; every subdirectory with a `monitor.py` is one monitor.
ROOT = Path(__file__).parent


@dataclass(frozen=True)
class Monitor:
    id: str
    name: str
    tagline: str
    #: One X advanced search per entry; the slices come from the window below.
    queries: tuple[str, ...]
    #: System prompt file name, read from `monitors/<id>/prompts/<prompt>` at call time.
    prompt: str
    #: Stamped on every card so a prompt change is traceable.
    version: str
    #: Which card fields this monitor's page prints, in order.
    fields: tuple[str, ...]
    window_hours: int = 24
    slice_hours: int = 4
    #: The gate's floor. `rules.judge` scores the post; anything under this never reaches the model.
    min_score: int = 30
    #: How many gated posts one model call may see. The monitor is a sweep, not a census.
    max_posts: int = 100
    #: Defaults the settings page starts from; `monitors/<id>/settings.yaml` overrides them.
    interval_hours: int = 8
    start_hour: int = 1
    start_minute: int = 15
    notes: tuple[str, ...] = field(default_factory=tuple)


def package(monitor: Monitor) -> Path:
    """This monitor's own folder."""
    return ROOT / monitor.id


def prompt_dir(monitor: Monitor) -> Path:
    return package(monitor) / "prompts"


def _discover() -> dict[str, Monitor]:
    out: dict[str, Monitor] = {}
    for path in sorted(ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith(("_", ".")):
            continue
        if not (path / "monitor.py").is_file():
            continue
        module = importlib.import_module(f"{__package__}.{path.name}.monitor")
        monitor = getattr(module, "MONITOR", None)
        if isinstance(monitor, Monitor):
            out[monitor.id] = monitor
    return out


MONITORS: dict[str, Monitor] = _discover()

#: Kept because tests and a few call sites ask for this one by name.
BINANCE_AGENT_ID: Monitor = MONITORS["binance-agent-id"]


def get(monitor_id: str) -> Monitor | None:
    return MONITORS.get(monitor_id)
