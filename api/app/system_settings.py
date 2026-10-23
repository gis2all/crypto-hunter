"""`config/system.yaml`, as the 系统设置 page reads it and writes it back.

One file, two faces: `config.load_env_file` turns it into the environment variables the
rest of the app already reads (on every model call), and this module turns it into a form and
back. Both walk `config.SYSTEM_KEYS`, so the page can never offer a key nothing reads.

Two rules come with the loader, and the page has to keep them:

- A variable the operating system already owns beats the file, now and later. That row is drawn
  read-only: a save that wrote the file while the process went on reading the other value would
  be a save whose effect the operator cannot see.
- Nothing is stored as an empty string. A blank box means the key is not configured, which is
  what the code's own defaults answer. The one exception is a secret, where a blank box means
  "the value already stored": clearing and keeping are the same keystroke there, and keeping is
  the one that cannot cost an operator a working key.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
from collections.abc import Mapping
from typing import Any

from ruamel.yaml.comments import CommentedMap

from . import config, logging_setup

log = logging.getLogger("crypto-hunter.system")

#: Serialises a save against another save. `config`'s own lock guards the chain files, which
#: are other files entirely, so nothing here shares it.
_lock = threading.Lock()

#: The answer to "leave this one alone": a blank secret box. Distinct from None, which is the
#: value that clears a row.
_KEEP = object()

#: The one JSON object shape this file takes, written once for the refusals below.
_HEADERS_EXAMPLE = '{"x-opencode-session": "crypto-hunter"}'


def masked(value: str) -> str:
    """What the page may show of a stored credential: its two ends, never its middle.

    Head plus tail (`sk-liv…6789`) answers the one question this row has to answer - "is the
    panel using the key I think it is?" - without handing a whole credential to a browser, a
    screenshot or a bug report. The log keeps its sha256 head; this row is for eyes.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) < 12:                      # too short to split; the tail still names it
        return "…" + text[-4:] if len(text) > 4 else "…"
    return text[:6] + "…" + text[-4:]


def state() -> dict[str, dict]:
    """One entry per key in `config.SYSTEM_KEYS`: what a row shows, and where it came from.

    `value` is what the row draws and what a save sends back, `source` is which of the
    three places decided it (this file / the operating system / the code's own default), and
    `mask` is what may be shown of a secret. A row whose source is `env` is the one the
    page draws read-only.
    """
    # The page reads what the next call would read, so the file is applied first: an edit made
    # in a text editor since the last model call shows up here too.
    config.load_env_file()
    document = _load()
    owned = config.file_owner()
    out: dict[str, dict] = {}
    for path, name, kind in config.SYSTEM_KEYS:
        source = _source(name, owned)
        raw = _dig(document, path)
        if kind == "secret":
            stored = os.environ.get(name, "") if source == "env" else _text(raw)
            out[path] = {"value": "", "set": bool(stored.strip()), "mask": masked(stored),
                         "source": source, "kind": kind}
            continue
        if source == "file":
            value: Any = _shown(raw, kind)
        elif source == "env":
            value = os.environ.get(name, "")
        else:
            value = None
        out[path] = {"value": value, "set": None, "mask": "", "source": source, "kind": kind}
    return out


def save(patch: Mapping[str, Any]) -> None:
    """Write `patch` into the file, then make the process read what was written.

    A key is a row's own path (`llm.base_url`), so a patch names the lines of the file it means
    and nothing else. The re-read is part of the write rather than a step after it: a value that
    landed in the file but not in the process is a save nobody can see the effect of.
    """
    with _lock:
        if not patch:
            config.load_env_file()
            return
        _seed()
        document = _load()
        for path, raw in patch.items():
            entry = _entry(str(path))
            if entry is None:
                raise ValueError("系统设置里没有「%s」这一项" % path)
            value = _coerce(str(path), raw, entry[2])
            if value is _KEEP:
                continue
            _put(document, str(path), value)
        _write(document)
        config.load_env_file()
        # CH_LOG_LEVEL is read when the handler is attached and `setup` is idempotent, so a
        # level this save changed is the level the next line is logged at.
        logging_setup.setup()


def _entry(path: str) -> tuple[str, str, str] | None:
    """The table row for `path`, or None where there is no such key."""
    for key, name, kind in config.SYSTEM_KEYS:
        if key == path:
            return key, name, kind
    return None


def _session_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The file's X sessions as rows, with the raw values (never handed to a browser)."""
    block = document.get("x") if isinstance(document.get("x"), Mapping) else {}
    rows: list[dict[str, Any]] = []
    listed = block.get("cookies") if isinstance(block.get("cookies"), list) else []
    for index, item in enumerate(listed):
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("auth_token") or "").strip()
        ct0 = str(item.get("ct0") or "").strip()
        if not token or not ct0:
            continue
        rows.append({"label": str(item.get("label") or "cookie%d" % (index + 1)).strip(),
                     "auth_token": token, "ct0": ct0,
                     "enabled": bool(item.get("enabled", True))})
    if not rows:
        token = str(block.get("auth_token") or "").strip()
        ct0 = str(block.get("ct0") or "").strip()
        if token and ct0:
            rows.append({"label": "main", "auth_token": token, "ct0": ct0, "enabled": True})
    return rows


def x_sessions() -> list[dict[str, Any]]:
    """What the 系统设置 page shows: labels, masks, and the health the pool knows - never the
    cookies themselves.

    `state` is the pool's reading, not the file's: a session the file calls enabled can still be
    resting from a 429 (`cooling`) or retired by a 401 (`disabled`), and a page that showed only
    the file would look healthy while every call waited out a cooldown. The file stays the source
    of `enabled` - that is what a save writes.
    """
    try:
        from .search.x_sessions import pool as session_pool
        live = {row["label"]: row for row in session_pool().snapshot()}
    except Exception:  # the page must draw even if the pool cannot read the file
        live = {}
    out: list[dict[str, Any]] = []
    for row in _session_rows(_load()):
        reading = live.get(row["label"])
        state = ""
        if reading is not None and not reading["enabled"]:
            state = "disabled"
        elif reading is not None and reading["cooling"]:
            state = "cooling"
        out.append({"label": row["label"], "enabled": row["enabled"],
                    "mask_auth": masked(row["auth_token"]), "mask_ct0": masked(row["ct0"]),
                    "state": state})
    return out


def first_session() -> dict[str, Any] | None:
    """The session the probe button spends: the first enabled one, else the first."""
    rows = _session_rows(_load())
    for row in rows:
        if row["enabled"]:
            return row
    return rows[0] if rows else None


def probe_sessions(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The sessions a probe should ask about: the page's rows as they stand, with a blank secret
    standing for the stored one exactly as a save reads it. A row with neither is skipped -
    there is nothing to spend, and the page already says so.
    """
    stored = {row["label"]: row for row in _session_rows(_load())}
    out: list[dict[str, Any]] = []
    for raw in rows:
        label = str(raw.get("label") or "").strip()
        before = stored.get(str(raw.get("was_label") or "").strip()) or stored.get(label, {})
        token = str(raw.get("auth_token") or "").strip() or before.get("auth_token", "")
        ct0 = str(raw.get("ct0") or "").strip() or before.get("ct0", "")
        if token and ct0:
            out.append({"label": label or before.get("label", "cookie"),
                        "auth_token": token, "ct0": ct0})
    return out


def save_x_sessions(rows: list[Mapping[str, Any]]) -> None:
    """Replace the cookie list with the page's rows. A blank secret keeps the stored one."""
    with _lock:
        _seed()
        document = _load()
        existing = {row["label"]: row for row in _session_rows(document)}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows or []:
            label = str(raw.get("label") or "").strip()
            if not label:
                raise ValueError("每个会话都要一个标签")
            if label in seen:
                raise ValueError("会话标签重复：%s" % label)
            seen.add(label)
            before = (existing.get(str(raw.get("was_label") or "").strip())
                      or existing.get(label, {}))
            token = str(raw.get("auth_token") or "").strip() or before.get("auth_token", "")
            ct0 = str(raw.get("ct0") or "").strip() or before.get("ct0", "")
            if not token or not ct0:
                raise ValueError("会话 %s 需要 auth_token 和 ct0" % label)
            out.append({"label": label, "auth_token": token, "ct0": ct0,
                        "enabled": bool(raw.get("enabled", True))})
        if len(out) > 10:
            raise ValueError("最多 10 个会话")
        block = document.get("x")
        if not isinstance(block, Mapping):
            block = document["x"] = {}
        block["cookies"] = out
        for key in ("auth_token", "ct0"):
            if key in block:
                del block[key]
        _write(document)
        config.load_env_file()


def effective(patch: Mapping[str, Any]) -> dict[str, str]:
    """Every key as the next call would read it, with the page's unsaved draft laid over.

    The probe endpoints need this: a page that had to be saved before it could be tested would
    make the operator store an endpoint in order to find out whether it is the right one. Same
    coercion as a save - a typo a save refuses is refused here - but nothing is written.

    The values come back in the clear, because this never leaves the process: `state()` is what
    the browser gets, and it is the one that hides a credential behind its fingerprint.
    """
    config.load_env_file()
    out: dict[str, str] = {}
    for path, name, _kind in config.SYSTEM_KEYS:
        out[path] = os.environ.get(name, "")
    for path, raw in patch.items():
        entry = _entry(str(path))
        if entry is None:
            raise ValueError("系统设置里没有「%s」这一项" % path)
        value = _coerce(str(path), raw, entry[2])
        if value is _KEEP:
            continue
        out[str(path)] = _text(value)
    return out


def _source(name: str, owned: Mapping[str, str]) -> str:
    """Which of the three owns this variable right now: file | env | unset."""
    if name in owned:
        return "file"
    return "env" if name in os.environ else "unset"


def _coerce(path: str, raw: Any, kind: str) -> Any:
    """A row's answer as the file stores it - or a sentence saying why it cannot be stored.

    The browser sends strings, so every shape is read back here rather than trusted: a number
    that arrived as text is stored as a number, and a value outside a `choice:` set is refused
    instead of written into a file the next call would fail on.
    """
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False)
    text = "" if raw is None else str(raw).strip()
    if kind == "secret":
        return _KEEP if not text else text
    if kind == "json":
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ValueError("「%s」要写成 JSON，%s" % (path, _HEADERS_EXAMPLE))
        if not isinstance(parsed, dict):
            raise ValueError("「%s」要写成 JSON 对象，%s" % (path, _HEADERS_EXAMPLE))
        return parsed
    if not text:
        return None
    if kind.startswith("choice:"):
        options = kind.split(":", 1)[1].split("|")
        for option in options:
            if option.lower() == text.lower():
                return option
        raise ValueError("「%s」只能填 %s" % (path, " / ".join(options)))
    if kind in ("int", "num"):
        return _number(path, text, whole=kind == "int")
    return text


def _number(path: str, text: str, whole: bool) -> Any:
    """A number as the file stores it - an integer for `int`, and for `num` whichever one it
    is, so a seconds knob stays readable as 300 rather than 300.0.

    NaN and infinity are refused rather than stored: both survive the round trip as text and
    then blow up somewhere the operator cannot connect to this box.
    """
    try:
        number = float(text)
    except ValueError:
        raise ValueError("「%s」只能填%s" % (path, "整数" if whole else "数字"))
    if not math.isfinite(number):
        raise ValueError("「%s」只能填%s" % (path, "整数" if whole else "数字"))
    if whole:
        if number != int(number):
            raise ValueError("「%s」只能填整数" % path)
        return int(number)
    return int(number) if number.is_integer() else number


def _load() -> Any:
    """The file as the round-trip parser models it. A file that is not there is empty."""
    path = config.SYSTEM_PATH
    if not path.is_file():
        return CommentedMap()
    return config.read_document(path) or CommentedMap()


def _write(document: Any) -> None:
    """Keep the file as it stands, then replace it through a temp file and a rename (see
    `config.write_document`).

    The copy is what makes a bad save reversible: `system.yaml.bak` is the file the last save
    replaced, so copying it back undoes that save. A test once wrote this file by accident
    (2026-10-28) and the keys it held were gone with no copy to fall back on.
    """
    _backup(config.SYSTEM_PATH)
    config.write_document(config.SYSTEM_PATH, document)


def _backup(path: Any) -> None:
    """Write what the file holds now to `<name>.bak`, next to it."""
    if not path.is_file():
        return
    try:
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    except OSError as error:
        # A save that works is better than a save refused over its own safety copy; the log
        # line is what says the copy is missing.
        log.warning("system.yaml: could not write %s.bak: %s", path.name, error)


def _seed() -> None:
    """Give the file a body the first time, from the example, so the comments and the rows ship
    with it.

    Called by a save and not by a read: opening the page must not write a file on a machine
    that has not been configured yet.
    """
    path = config.SYSTEM_PATH
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    example = path.parent / (path.stem + ".yaml.example")
    if example.is_file():
        shutil.copyfile(example, path)
    else:
        path.write_text("", encoding="utf-8")


def _put(document: Any, path: str, value: Any) -> None:
    """Set one leaf, making the groups above it only where the file has none."""
    parts = path.split(".")
    node = document
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = CommentedMap()
            node[part] = child
        node = child
    node[parts[-1]] = value


def _dig(document: Mapping, path: str) -> Any:
    """`document`'s value at `path`, or None where the file has nothing to say."""
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _text(raw: Any) -> str:
    """A file value as the string a row shows, empty where the file has none."""
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False)
    return str(raw).strip()


def _shown(raw: Any, kind: str) -> Any:
    """What a row draws: JSON as the text a box can edit, everything else as it is."""
    return _text(raw) if kind == "json" else raw
