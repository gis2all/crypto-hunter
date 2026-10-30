"""Per-monitor settings: the X queries, the switches, the clock and the 采集与过闸 numbers.

Each monitor keeps its own file at `monitors/<id>/settings.yaml`. It is not in `config/`:
these are the monitor's own parameters, not this machine's system settings (2026-10-30, user).
The file is materialised with every key at its default, so a parameter you cannot see is a
parameter you cannot change; a missing key still falls back to the registry default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .. import registry
from ..registry import Monitor

#: `monitors/`; tests point this at a temporary directory.
ROOT: Path = registry.ROOT

#: Every key the file may hold, in the order it writes them.
KEYS = ("enabled", "schedule", "interval_hours", "start_hour", "start_minute",
        "window_hours", "slice_hours", "min_score", "queries")


def path(monitor: Monitor) -> Path:
    return ROOT / monitor.id / "settings.yaml"


def _file(monitor: Monitor) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path(monitor).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def _rows(value: Any) -> tuple[str, ...]:
    """Queries as a YAML string or a list, trimmed and without blanks."""
    if isinstance(value, str):
        value = [value]
    return tuple(str(item).strip() for item in (value or []) if str(item).strip())


def _effective(monitor: Monitor) -> dict[str, Any]:
    """Read-only: the file over the registry's defaults, without touching the disk."""
    over = _file(monitor)
    return {
        "enabled": bool(over.get("enabled", True)),
        # 计划任务是独立的一档：项目开着、但不想按点自动跑时，它只影响时钟，不影响手动运行。
        "schedule": bool(over.get("schedule", True)),
        "interval_hours": int(over.get("interval_hours", monitor.interval_hours)),
        "start_hour": int(over.get("start_hour", monitor.start_hour)),
        "start_minute": int(over.get("start_minute", monitor.start_minute)),
        "window_hours": int(over.get("window_hours", monitor.window_hours)),
        "slice_hours": int(over.get("slice_hours", monitor.slice_hours)),
        "min_score": int(over.get("min_score", monitor.min_score)),
        "queries": _rows(over.get("queries")) or monitor.queries,
    }


def values(monitor: Monitor) -> dict[str, Any]:
    """The effective settings; the file is materialised with every key on first read."""
    if not path(monitor).exists():
        write(monitor, {})
    return _effective(monitor)


def value(monitor: Monitor, key: str, default: Any = None) -> Any:
    return values(monitor).get(key, default)


def write(monitor: Monitor, patch: dict[str, Any]) -> dict[str, Any]:
    """Save what the settings page sent, and write every key back to the file."""
    current = _effective(monitor)
    for key, raw in (patch or {}).items():
        if key not in KEYS:
            continue
        if key in ("enabled", "schedule"):
            current[key] = bool(raw)
        elif key == "queries":
            current[key] = _rows(raw) or monitor.queries
        else:
            current[key] = int(raw)
    body = ("# %s 的查询、开关、节奏与采集参数；默认值在 monitors/registry.py。\n"
            % monitor.name)
    body += yaml.safe_dump(
        {key: (list(current[key]) if key == "queries" else current[key]) for key in KEYS},
        allow_unicode=True, sort_keys=False)
    target = path(monitor)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return current
