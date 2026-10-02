"""Macro-track settings from config/macro.yaml.

One file on purpose: the source list and the thresholds are the whole
configuration, and the page for it comes later. Read fresh every time - the
file is tiny and an hourly slot must see a hand edit without a restart.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .. import config as root_config
from ..store import db

MACRO_PATH = Path(os.environ.get("CH_MACRO_PATH", str(root_config.ROOT / "config" / "macro.yaml")))

DEFAULTS: dict = {
    "cadence": {"collect_minutes": 60, "digest_hour": 8, "timezone": "Asia/Shanghai"},
    "selection": {"score_threshold": 3.5, "min_single_score": 2.5,
                  "keep_per_day": 50, "prescreen_max_age_days": 14},
    "heat": {"window_hours": 48, "half_life_hours": 24},
    "topics": {"sectors": [], "morphs": []},
}


def load_macro(path: str | Path | None = None) -> dict:
    """The macro settings, merged over defaults. Missing file reads as defaults."""
    target = Path(path or MACRO_PATH)
    raw: dict = {}
    if target.is_file():
        with open(target, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = {key: {**DEFAULTS[key], **(raw.get(key) or {})} for key in DEFAULTS}
    sources = []
    for entry in raw.get("sources") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        sources.append({
            "id": str(entry["id"]),
            "name": str(entry.get("name") or entry["id"]),
            "type": str(entry.get("type") or "rss").lower(),
            "url": str(entry.get("url") or ""),
            "tier": str(entry.get("tier") or "media").lower(),
            "enabled": bool(entry.get("enabled", True)),
            "items_path": str(entry.get("items_path") or "items"),
            "fields": dict(entry.get("fields") or {}),
        })
    cfg["sources"] = [s for s in sources if s["url"]]
    return cfg


def sync_sources(cfg: dict | None = None) -> int:
    """Mirror the enabled sources into macro_sources. Returns the enabled count."""
    cfg = cfg or load_macro()
    conn = db.connect()
    try:
        for src in cfg["sources"]:
            conn.execute(
                "INSERT INTO macro_sources (id, name, type, url, tier, enabled, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,"
                " url=excluded.url, tier=excluded.tier, enabled=excluded.enabled,"
                " updated_at=excluded.updated_at",
                (src["id"], src["name"], src["type"], src["url"], src["tier"],
                 1 if src["enabled"] else 0, db.now_iso()),
            )
    finally:
        conn.close()
    return sum(1 for src in cfg["sources"] if src["enabled"])