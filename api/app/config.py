"""Configuration: chain settings from config/chains.yaml, secrets from the environment."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("CH_CONFIG", str(ROOT / "config" / "chains.yaml")))
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("CH_DB", str(DATA_DIR / "crypto-hunter.db")))
WEB_DIST = ROOT / "web" / "dist"

HOST = os.environ.get("CH_HOST", "127.0.0.1")
PORT = int(os.environ.get("CH_PORT", "8787"))

#: The writing stage's endpoint, as a file next to the chain config. A deployment detail
#: (which gateway, which key) does not belong in a shell profile, and this app runs as one
#: process, so a file plus a restart is the whole install step.
LLM_ENV_PATH = Path(os.environ.get("CH_LLM_ENV", str(ROOT / "config" / "llm.env")))

#: What the file put in the environment, and what it put there. A value that is still
#: exactly this is still the file's to withdraw; anything else means somebody else owns
#: the variable now and the file must keep its hands off.
_FROM_FILE: dict[str, str] = {}


def load_env_file(path: Path | None = None) -> int:
    """Apply `KEY=VALUE` lines from an env file and return how many were applied.

    Called on every LLM call rather than once at import. That is what makes the file the
    install step: edit it and the next round picks the change up, delete it and the
    settings are gone - no restart, and no stale endpoint left behind in `os.environ`.

    A real environment variable always wins, including one set after the file was read:
    that key is simply never touched again.
    """
    path = path or LLM_ENV_PATH
    for key, value in list(_FROM_FILE.items()):
        if os.environ.get(key) == value:
            del os.environ[key]
        _FROM_FILE.pop(key, None)
    if not path.is_file():
        return 0
    applied = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        _FROM_FILE[key] = value
        applied += 1
    return applied


load_env_file()


@functools.lru_cache(maxsize=1)
def load_chains() -> dict[str, dict]:
    """All chains declared in config/chains.yaml, keyed by id."""
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return {c["id"]: c for c in raw.get("chains", [])}


def chain(chain_id: str) -> dict:
    chains = load_chains()
    if chain_id not in chains:
        raise KeyError(chain_id)
    return chains[chain_id]


def enabled_chains() -> list[dict]:
    return [c for c in load_chains().values() if c.get("enabled")]


def reload() -> None:
    load_chains.cache_clear()


#: The five sections that make up "a chain's settings". Everything a round does is
#: decided inside these, which is why a settings version can be replayed later.
SETTINGS_SECTIONS = ("schedule", "pipeline", "report", "alerts", "sources")


def settings_payload(chain_id: str, version_id: int | None = None) -> dict:
    """The settings a round ran under.

    A round is archived under the口径 of its own time, so this takes the version the run
    pinned rather than whatever is in force when somebody reads the page. The stored
    snapshot is overlaid on the YAML instead of replacing it: a knob added to the config
    after a version was saved still exists, at its YAML default.
    """
    import json

    chain_cfg = chain(chain_id)
    payload = {key: chain_cfg.get(key) for key in SETTINGS_SECTIONS}
    if version_id is None:
        return payload
    from .store import db

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM settings_versions WHERE id = ?", (version_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return payload
    return merge_patch(payload, json.loads(row["payload_json"]))


TRUE_WORDS = {"true", "1", "on", "yes", "开启"}
FALSE_WORDS = {"false", "0", "off", "no", ""}


def _infer(value: Any) -> Any:
    """Guess the type of a value for a knob that has no default yet.

    A blank default (holders_max: null) means no bound, which is a real answer a blank
    box has to be able to give back. But the first number typed into that box still has
    to land as a number: an int compared against a string raises instead of filtering,
    and the round dies on a knob the page let you set. So the text is read once, here,
    rather than trusted to be a string forever.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def coerce_like(base: Any, value: Any) -> Any:
    """Force `value` into the shape of `base`.

    The settings page sends whatever an <input> held - always strings, sometimes
    "20" where the config says 20, sometimes "true" where the config says true. The
    frontend is deliberately not trusted to know the types: the current config value
    is the authority, so a knob can never silently become a string where a number was.
    A knob whose current value is None has no type to be forced into, so the text
    speaks for itself (_infer), and a None coming the other way is kept as the blank it
    is rather than coerced into a number.
    """
    if base is None:
        return _infer(value)
    if value is None:
        # An explicit null survives: it is how a knob that shipped blank is put back to
        # blank, and the API layer only lets one through for a knob whose config default
        # is already blank (`api/v1/settings._resolve_blanks`).
        return None
    if isinstance(base, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        raise ValueError("expected a boolean, got %r" % (value,))
    if isinstance(base, int) and not isinstance(base, bool):
        return int(round(float(value)))
    if isinstance(base, float):
        return float(value)
    if isinstance(base, list):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value)
    if isinstance(base, str):
        return str(value)
    return value


def merge_patch(base: dict, patch: dict) -> dict:
    """Deep-merge a partial patch into `base`, coercing each leaf to base's type.

    Keys may be nested (`{"a": {"b": 1}}`) or dotted (`{"a.b": 1}`). The settings form
    labels every control with its dotted path, so accepting that same spelling back
    means the page never has to reassemble the tree and can never disagree with it.
    """
    out = dict(base)
    for key, value in (patch or {}).items():
        head, dot, rest = str(key).partition(".")
        if dot:
            child = out.get(head)
            out[head] = merge_patch(child if isinstance(child, dict) else {}, {rest: value})
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_patch(out[key], value)
        elif key in out:
            out[key] = coerce_like(out[key], value)
        else:
            out[key] = value
    return out


def sync_schedule(payload: dict) -> dict:
    """Keep `schedule.cron` in step with `schedule.interval_hours`.

    The interval is what a human edits; the cron expression is what the ticker reads.
    Deriving one from the other here means the two can never disagree.
    """
    schedule = dict(payload.get("schedule") or {})
    hours = schedule.get("interval_hours")
    if hours:
        from .api.v1.forms import interval_slots

        slots = interval_slots(int(hours))
        schedule["cron"] = "0 %s * * *" % ",".join("%d" % s for s in slots)
    payload = dict(payload)
    payload["schedule"] = schedule
    return payload
