"""Configuration: chain settings from config/chains/*.yaml, secrets from the environment.

One file per chain, and one chain carries one or more sets of settings - see `chain()`,
which reads a chain through the one it is asked for.
"""

from __future__ import annotations

import copy
import functools
import io
import itertools
import json
import os
import secrets
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter

ROOT = Path(__file__).resolve().parents[2]
#: One file per chain under `config/chains/`. Adding a chain is adding a file there: there
#: is no shared list to edit and no way for two chains to disagree about the file's shape.
CHAINS_DIR = Path(os.environ.get("CH_CHAINS_DIR", str(ROOT / "config" / "chains")))

#: What marks the tracked template in that directory: `robinhood.example.yaml` is the factory
#: default and the only one in git, `robinhood.yaml` is this install's copy of it. The app
#: writes the second and never the first, which is what keeps switching the live set - or adding
#: one - out of every diff. Same directory rather than a subdirectory so the two can be diffed
#: against each other by eye.
EXAMPLE_MARK = ".example."
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("CH_DB", str(DATA_DIR / "crypto-hunter.db")))
WEB_DIST = ROOT / "web" / "dist"

#: The hour a schedule starts from when a config file names none. The shipped files all
#: belong to the 02/05/08... grid, so the fallback is the hour they already use: an anchor
#: that changed by itself would move a schedule nobody asked to move.
DEFAULT_START_HOUR = 2

HOST = os.environ.get("CH_HOST", "127.0.0.1")
PORT = int(os.environ.get("CH_PORT", "8787"))

#: The deployment's own wiring - which gateway answers the writing stage, which X session
#: carries a search, how loud the log is - in one file next to the chain config. A deployment
#: detail does not belong in a shell profile, and this app runs as one process, so the file is
#: the whole install step; the 系统设置 page is that same file with a form in front of it.
SYSTEM_PATH = Path(os.environ.get("CH_SYSTEM_ENV", str(ROOT / "config" / "system.yaml")))

#: The only list of what that file may set: the key's path inside it, the environment variable
#: the rest of the code already reads, and the shape a value has to be. One table, because two
#: readers walk it - the loader below and the page - and a second copy would let the page offer
#: a knob nothing reads. A secret is never handed back to a browser, and a 'choice:' tail is the
#: whole set of answers a save will take.
SYSTEM_KEYS: tuple[tuple[str, str, str], ...] = (
    ("llm.base_url", "CH_LLM_BASE_URL", "text"),
    ("llm.api_key", "CH_LLM_API_KEY", "secret"),
    ("llm.api_key_fallback", "CH_LLM_API_KEY_FALLBACK", "secret"),
    ("llm.model", "CH_LLM_MODEL", "text"),
    ("llm.reasoning", "CH_LLM_REASONING", "choice:none|minimal|low|medium|high"),
    ("llm.max_tokens", "CH_LLM_MAX_TOKENS", "int"),
    ("llm.timeout", "CH_LLM_TIMEOUT", "num"),
    ("llm.tries", "CH_LLM_TRIES", "int"),
    ("llm.key_cooldown", "CH_LLM_KEY_COOLDOWN", "num"),
    ("llm.extra_headers", "CH_LLM_EXTRA_HEADERS", "json"),
    ("research_workers", "CH_RESEARCH_WORKERS", "int"),
    ("x.auth_token", "CH_X_AUTH_TOKEN", "secret"),
    ("x.ct0", "CH_X_CT0", "secret"),
    ("x.cli", "CH_X_CLI", "text"),
    ("log_level", "CH_LOG_LEVEL", "choice:DEBUG|INFO|WARNING|ERROR"),
)

#: Keys the file accepts and the page deliberately does not show: the page would invite the
#: operator to write by hand what the client already sends for the gateway (6.4.8), while the
#: file keeps taking them, because an install may already have written one and needing the
#: exception should not wait for a release. The test that pairs rows with keys reads this - a
#: key is either a row or file-only, never both.
FILE_ONLY_KEYS: frozenset[str] = frozenset({"llm.extra_headers"})

#: What the file put in the environment, and what it put there. A value that is still exactly
#: this is still the file's to withdraw; anything else means somebody else owns the variable
#: now and the file must keep its hands off.
#:
#: Keyed by path because two files used to be read here, and re-reading one must not withdraw
#: what the other set: the LLM file is re-read on every model call, and while the record was
#: one flat dict that reload deleted the search file's credentials on the first call of every
#: process - every X search after it failed "needs CH_X_AUTH_TOKEN", in a round that looked
#: like it had simply found nothing. Both files are one file now and the rule is unchanged:
#: re-reading one path never reaches for a variable another path owns.
_FROM_FILE: dict[Path, dict[str, str]] = {}


def _dig(document: Mapping, path: str) -> Any:
    """`document`'s value at `path`, or None where the file has nothing to say."""
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def flatten(document: Mapping) -> dict[str, str]:
    """The variables `document` sets - and only the ones it really sets.

    `null`, a missing key and an empty value all mean the same thing: this file does not
    configure that variable, so nothing goes into the environment and the code's own default
    decides. That is what a half-filled copy of the example relies on, and it is why a value is
    never written as an empty string: `float(os.environ.get("CH_LLM_TIMEOUT", "180"))` would
    raise on one where the default is what was meant.
    """
    out: dict[str, str] = {}
    for key, name, kind in SYSTEM_KEYS:
        value = _dig(document, key)
        if value is None:
            continue
        if kind == "json" and isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value).strip()
        if text:
            out[name] = text
    return out


def file_owner(path: Path | None = None) -> dict[str, str]:
    """What the file at `path` currently owns in the environment, variable by variable.

    The page reads this to tell a value it can write from one the operating system is holding:
    a variable the file did not set is never touched by the file, so a row for it is read-only.
    """
    return dict(_FROM_FILE.get(path or SYSTEM_PATH, {}))


def load_env_file(path: Path | None = None) -> int:
    """Apply `config/system.yaml` and return how many variables were set.

    Called on every LLM call rather than once at import. That is what makes the file the
    install step: edit it and the next round picks the change up, delete it and the settings
    are gone - no restart, and no stale endpoint left behind in `os.environ`.

    A real environment variable always wins, including one set after the file was read: that
    key is simply never touched again.
    """
    path = path or SYSTEM_PATH
    for key, value in list(_FROM_FILE.pop(path, {}).items()):
        if os.environ.get(key) == value:
            del os.environ[key]
    if not path.is_file():
        return 0
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        # A file caught mid-write parses as nothing, and the round it lands in is not the place
        # to learn that. Everything it owned was withdrawn above, so the code's own defaults
        # answer - the same state as a file that is not there at all.
        return 0
    applied = 0
    owned: dict[str, str] = {}
    for name, value in flatten(document).items():
        if name in os.environ:
            continue
        os.environ[name] = value
        owned[name] = value
        applied += 1
    _FROM_FILE[path] = owned
    return applied


load_env_file()


@functools.lru_cache(maxsize=1)
def load_chains() -> dict[str, dict]:
    """Every chain on disk, keyed by its id.

    A chain's file carries the chain's own facts - browser, node, the sources that exist
    for it - and a `configs` list: one entry per set of settings. Each entry screens the
    same chain under its own口径 and keeps its own report library, so two entries on one
    chain never blend into each other.
    """
    seed_chain_files()
    chains: dict[str, dict] = {}
    for path in _chain_files():
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        chain_id = _chain_id_of(raw, path)
        raw["id"] = chain_id
        try:
            # What the settings page prints as where the defaults came from. A config
            # directory outside the checkout - what CH_CHAINS_DIR is for - has no relative
            # name, and losing the page over a path is not worth the shorter string.
            raw["path"] = path.relative_to(ROOT).as_posix()
        except ValueError:
            raw["path"] = str(path)
        raw["configs"] = [_config_entry(c) for c in raw.get("configs") or []]
        if not raw["configs"]:
            raise KeyError(f"{chain_id}: a chain file with no configs screens nothing")
        raw["warning"] = _keep_one_live(raw["configs"])
        chains[chain_id] = raw
    return chains


def _all_files() -> list[Path]:
    """Every yaml in the chain directory, templates included, in the order they are read in."""
    return sorted(CHAINS_DIR.glob("*.yaml")) + sorted(CHAINS_DIR.glob("*.yml"))


def _chain_files() -> list[Path]:
    """The files an install runs on, in the order they are read in - the templates are not chains.

    Reading a template here would give one chain id two entries, and the same set of settings
    twice on the rail.
    """
    return [path for path in _all_files() if EXAMPLE_MARK not in path.name]


def seed_chain_files() -> int:
    """Give every chain the live file this install has not got yet, from the tracked template.

    Only the template is in git; the live file is this install's, and it is what the page writes
    - switching the live set, adding one, renaming it - so keeping the two apart is what keeps
    that out of every diff. The same shape `system.yaml` already has. It copies rather than
    aliases, so an install that deletes its file is handed the factory one again on the next
    read, and it runs on the read path because every way in - the API, the ticker, a test - has
    to find a chain this machine has never started.
    """
    made = 0
    for example in (path for path in _all_files() if EXAMPLE_MARK in path.name):
        live = example.with_name(example.name.replace(EXAMPLE_MARK, ".", 1))
        if not live.exists():
            live.write_bytes(example.read_bytes())
            made += 1
    return made


def _chain_id_of(raw: dict, path: Path) -> str:
    """The id a chain file claims. The file name is only the fallback."""
    return str(raw.get("id") or path.stem)


def _keep_one_live(configs: list[dict]) -> str | None:
    """Read a file that switched two sets of settings on as one, and say so.

    The write path cannot produce this - `set_active_config` writes one `enabled: true` and
    the rest false in a single pass - but a file edited by hand can, and the ticker would
    paper over it: it starts the first set that is due and leaves the second waiting for a
    slot that then goes by. Reading it as "the first one wins" is the half that has to
    happen for a round to be predictable; the page printing the warning is the half that
    gets it fixed.
    """
    live = [cfg for cfg in configs if cfg.get("enabled", True)]
    if len(live) <= 1:
        return None
    for cfg in live[1:]:
        cfg["enabled"] = False
    return (f"文件里有 {len(live)} 套配置都开着；只按第一套「{live[0]['label']}」跑，"
            f"其余按关闭读。存一次设定就会写回成一套。")


def _config_entry(cfg: dict | None) -> dict:
    """One set of settings, with the fields the rest of the code assumes are there.

    `default` is never deletable: it is the set of settings a chain has before anyone
    makes a second one, and the one every round falls back to when nothing else is named.
    """
    entry = dict(cfg or {})
    entry["id"] = str(entry.get("id") or "default")
    entry["label"] = str(entry.get("label") or entry["id"])
    entry.setdefault("enabled", True)
    entry["locked"] = bool(entry.get("locked")) or entry["id"] == "default"
    # Filled in rather than demanded: the page refuses a patch aimed at a knob the config
    # does not have, and 0 hours apart is the value that means "never" - so a file that
    # names neither is a chain the ticker leaves alone, which is what the page then shows.
    schedule = entry.get("schedule")
    if isinstance(schedule, dict):
        schedule.setdefault("interval_hours", 0)
        schedule.setdefault("start_hour", DEFAULT_START_HOUR)
    return entry


def chain(chain_id: str, config_id: str | None = None) -> dict:
    """One chain read through one of its sets of settings.

    The five settings sections come from the named config; everything else is the chain's
    own. `id` stays the chain's id even under a second set of settings - both sets screen
    the same tokens on the same node, and that id is what says so.
    """
    entry = chain_entry(chain_id)
    cfg = config_entry(chain_id, config_id)
    merged = {key: value for key, value in entry.items() if key != "configs"}
    merged.update({key: cfg.get(key) for key in SETTINGS_SECTIONS})
    merged["config_id"] = cfg["id"]
    merged["config_label"] = cfg["label"]
    return merged


def chain_name(chain: Mapping[str, Any] | None) -> str:
    """What the chain is called *in prose*: `Arc`, never `Arc 雷达`.

    Two names, because they answer two questions. `label` is the radar's own name - what the
    rail and the settings drawer print - and a sentence built from it reads as the radar
    talking about itself: 「只回答和 Arc 雷达 什么关系」 is not what the card means. This is the
    chain's own name, and it is what goes into the writing prompt, the search query and the
    口径 line on the settings page.

    A file that never set one falls back to the label, so a new chain still reads as
    something rather than as a blank.
    """
    chain = chain or {}
    for key in ("name", "label", "id"):
        text = str(chain.get(key) or "").strip()
        if text:
            return text
    return ""


def chain_entry(chain_id: str) -> dict:
    """The chain's file as written: its own facts plus every set of settings."""
    chains = load_chains()
    if chain_id not in chains:
        raise KeyError(chain_id)
    return chains[chain_id]


def config_entry(chain_id: str, config_id: str | None = None) -> dict:
    """The settings entry `config_id` names, or the chain's first one when it is blank.

    Blank is the safe reading of "no opinion": a caller that never learned about more than
    one set of settings gets the chain's default one rather than an error about a name it
    had no way to know.
    """
    configs = chain_entry(chain_id).get("configs") or []
    if not configs:
        raise KeyError(chain_id)
    if not config_id:
        return configs[0]
    for cfg in configs:
        if cfg["id"] == config_id:
            return cfg
    raise KeyError(f"{chain_id}:{config_id}")


def configs_of(chain_id: str) -> list[dict]:
    """Every set of settings on this chain, in file order. The first one is the default."""
    return chain_entry(chain_id).get("configs") or []


def enabled_chains() -> list[dict]:
    return [c for c in load_chains().values() if c.get("enabled")]


def enabled_configs() -> list[dict]:
    """Every (chain, settings) pair whose rounds the ticker may start.

    One per chain by construction, which is the rule about the sources: a file that switched
    two on by hand is read as the first one and says so in its warning (`_keep_one_live`),
    and `set_active_config` writes the flags back to one on the next save. The busy gate in
    `schedule.tick` sits under this for the window where a file was hand-edited and nothing
    has saved over it yet.
    """
    rounds: list[dict] = []
    for entry in enabled_chains():
        for cfg in entry.get("configs") or []:
            if cfg.get("enabled", True):
                live = chain(entry["id"], cfg["id"])
                live["schedule"] = live_schedule(entry["id"], cfg["id"])
                rounds.append(live)
    return rounds


def reload() -> None:
    load_chains.cache_clear()

#: One writer at a time. Four requests edit the very files the ticker reads a round out
#: of, and the read half of a write that has not finished is a chain that is briefly not
#: the chain on disk. Nothing under this lock waits on a network, so holding it costs
#: nothing worth measuring.
_write_lock = threading.RLock()

#: What a generated `config_id` is drawn from. The id ends up in run keys, in URLs and in
#: five primary keys, so it stays lowercase and free of anything a shell, a file name or a
#: regular expression would want quoted.
_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

#: How many characters of it. Six is 2.2 billion names, and the id is only ever compared
#: for equality - nobody reads it, so nobody needs it to mean anything.
_ID_LENGTH = 6

#: A label is the whole of what a human reads in the menu, so it is bounded by what the
#: menu can show rather than by anything in the filesystem.
LABEL_MAX = 20

#: What a set of settings added by hand is called. The two naming rules the page offers are
#: `配置 N` for a new one and `<源> 副本` for a copy - see `_new_label` / `_copy_label`.
NEW_LABEL = "配置"
COPY_SUFFIX = "副本"


class ConfigRefused(ValueError):
    """A refusal about the state of the chain rather than about the text handed in.

    Its own type so the HTTP layer can tell the two apart without matching on the sentence:
    an empty or already-taken name is a 400, while "this one is live, switch first" is a
    409. The page prints either sentence as it is, which is why both are written for the
    person who has to act on them.
    """


def chain_path(chain_id: str) -> Path:
    """The file `chain_id` lives in. Raises KeyError when no file claims that id."""
    for path in _chain_files():
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if _chain_id_of(raw, path) == chain_id:
            return path
    raise KeyError(chain_id)


#: `holders_max: null` is how these files spell "no bound". Left to itself the round-trip
#: writer drops the word and prints an empty value - the same None read back, but not the
#: file as written, and the difference shows up as a changed line in every file it touches.
#: Registered on the class rather than on one reader because that is where ruamel keeps it.
def _represent_none(representer: Any, _data: Any) -> Any:
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


RoundTripRepresenter.add_representer(type(None), _represent_none)


def _round_trip() -> YAML:
    """The parser that keeps a file's comments. The read path cannot write, and this cannot.

    Every knob in these files carries the reason it is the number it is, written by hand
    over several passes. A writer that reformats a file it was asked to add one line to
    turns the next commit into the whole file, so the round-trip parser is the one that
    writes. What it takes for a file nobody edited to come back out byte for byte is the
    three settings below, and that property is the whole point: a one-line edit has to be
    a one-line diff. `indent` is the file's own shape (`  - id:` with the keys under it at
    four), and `width` is here because the comments are prose and rewrapping them at 80
    columns is a diff nobody asked for either.
    """
    writer = YAML(typ="rt")
    writer.preserve_quotes = True
    writer.indent(mapping=2, sequence=4, offset=2)
    writer.width = 1 << 16
    return writer


def read_document(path: Path) -> Any:
    """A config file as the round-trip parser models it - comments and key order and all.

    Public because two writers share it and neither may lose a file's comments: the chain
    files edit one set of settings at a time, and `system.yaml` edits one key at a time.
    """
    with open(path, "rb") as fh:
        return _round_trip().load(fh.read().decode("utf-8"))


def write_document(path: Path, document: Any) -> None:
    """Replace a config file with `document`, keeping its newline and its comments.

    Through a temp file in the same directory and a rename: a reader that loses the race -
    the ticker, or the next request - gets the whole of the old file or the whole of the
    new one, never half of the one being written. The newline is read off the original
    because these files are CRLF on disk and a YAML dumper emits LF, which would make every
    line of a touched file show up as changed.
    """
    with open(path, "rb") as fh:
        crlf = b"\r\n" in fh.read()
    buffer = io.StringIO()
    _round_trip().dump(document, buffer)
    text = buffer.getvalue().replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(temp, path)


def _edit_configs(chain_id: str, edit: Callable[[Any], None]) -> None:
    """Rewrite one chain file's `configs` list, then reload what the process reads.

    `edit` is handed the list itself as the round-trip parser loaded it, so anything
    appended to it keeps the file's own indentation and flow style. Read, edit, write and
    reload all happen under one lock: a request that reads this chain while another is
    adding a set of settings to it sees one file or the other, never a menu missing the set
    it just made.
    """
    with _write_lock:
        path = chain_path(chain_id)
        document = read_document(path)
        configs = (document or {}).get("configs")
        if not isinstance(configs, list) or not configs:
            raise KeyError(f"{chain_id}: a chain file with no configs screens nothing")
        edit(configs)
        write_document(path, document)
        reload()


def _entry_by_id(configs: Any, config_id: str) -> Any:
    """The one entry in a loaded `configs` list whose id is `config_id`."""
    for cfg in configs:
        if str(cfg.get("id") or "") == config_id:
            return cfg
    raise KeyError(config_id)


def _taken_labels(chain_id: str) -> set[str]:
    return {cfg["label"] for cfg in configs_of(chain_id)}


def _new_config_id(taken: set[str]) -> str:
    """A name for a set of settings that no human has to read or remember.

    Random rather than a counter: a counter has to survive deletion without handing the
    same name to two different sets of settings, which means storing the count somewhere
    and keeping it in step with a file people edit by hand.
    """
    while True:
        candidate = "c" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
        if candidate not in taken:
            return candidate


def _new_label(chain_id: str) -> str:
    """`配置 N` for the smallest free N. The shipped one is `默认配置`, not `配置 1`."""
    taken = _taken_labels(chain_id)
    for n in itertools.count(2):
        if f"{NEW_LABEL} {n}" not in taken:
            return f"{NEW_LABEL} {n}"


def _copy_label(chain_id: str, source: str) -> str:
    """`<源> 副本`, numbered when the chain already has a copy of that one."""
    taken = _taken_labels(chain_id)
    first = f"{source} {COPY_SUFFIX}"
    if first not in taken:
        return first
    for n in itertools.count(2):
        if f"{first} {n}" not in taken:
            return f"{first} {n}"


def check_label(chain_id: str, label: Any, keep: str | None = None) -> str:
    """The label to write, or a ValueError saying what is wrong with the one handed in.

    Uniqueness is per chain because the menu is per chain: two rows reading `配置 2` is a
    dropdown nobody can choose from. `keep` is the entry being renamed, which may keep its
    own name - it is the only one that is not read as a clash.
    """
    text = " ".join(str(label or "").split())
    if not text:
        raise ValueError("配置名不能为空")
    if len(text) > LABEL_MAX:
        raise ValueError(f"配置名最多 {LABEL_MAX} 个字，当前 {len(text)} 个")
    for cfg in configs_of(chain_id):
        if cfg["id"] != keep and cfg["label"] == text:
            raise ValueError(f"这条链上已经有一套叫「{text}」的设定了")
    return text


def create_config(chain_id: str, mode: str = "new", from_config_id: str | None = None,
                  label: Any = None) -> dict:
    """Add a set of settings: a fresh one seeded from `default`, or a copy of a named one.

    The new entry gets the base's five sections and nothing else. A report library is what
    a set of settings has earned, so a new one starts empty - handing it somebody else's
    rounds would point "上一轮" at a round it never ran. It also starts switched off: only
    one set per chain runs (`set_active_config`), and a set nobody has saved yet is not the
    one to make live.
    """
    if mode not in ("new", "copy"):
        raise ValueError("模式只能是新建或复制")
    base_id = "default" if mode == "new" else config_entry(chain_id, from_config_id)["id"]
    made: dict = {}

    def edit(configs: Any) -> None:
        # The values come from the plain view rather than from the loaded document: a set
        # of settings made here is generated, not written, and copying the base's comments
        # into it would repeat ninety lines of prose per entry in a file whose whole job is
        # to be read by a human.
        base = config_entry(chain_id, base_id)
        _entry_by_id(configs, base_id)
        made["id"] = _new_config_id({str(cfg.get("id") or "") for cfg in configs})
        if label is not None:
            made["label"] = check_label(chain_id, label)
        elif mode == "new":
            made["label"] = _new_label(chain_id)
        else:
            made["label"] = _copy_label(chain_id, str(base.get("label") or ""))
        entry = CommentedMap()
        entry["id"] = made["id"]
        entry["label"] = made["label"]
        # Written out even though it is the one value that is not inherited: a missing
        # `enabled` reads as on, and a new set of settings is the one thing that must not
        # quietly become the live one.
        entry["enabled"] = False
        for section in SETTINGS_SECTIONS:
            if base.get(section) is not None:
                entry[section] = _as_generated(base[section])
        configs.append(entry)

    _edit_configs(chain_id, edit)
    return config_entry(chain_id, made["id"])


def _as_generated(value: Any) -> Any:
    """Plain data, in the shape the round-trip writer should print it in.

    Mappings come out as the block mappings the file already uses. Lists come out inline,
    because that is how the shipped entry spells `sources` and `push_content`: a second
    entry that spells them differently reads like a mistake rather than like a choice.
    """
    if isinstance(value, dict):
        node = CommentedMap()
        for key, item in value.items():
            node[str(key)] = _as_generated(item)
        return node
    if isinstance(value, list):
        node = CommentedSeq()
        for item in value:
            node.append(_as_generated(item))
        node.fa.set_flow_style()
        return node
    return value


def rename_config(chain_id: str, config_id: str, label: Any) -> dict:
    """Rename a set of settings. The id is not a name and does not move with it.

    `id` is in five primary keys and in every run key this chain has written, so a rename
    that moved it would be a migration of the whole report library to change what one row
    reads. The label is the part a human reads and edits; the id is the part they never
    see. `default` is renameable like any other - what is locked is the entry, not the
    words on it.
    """
    target = config_entry(chain_id, config_id)["id"]

    def edit(configs: Any) -> None:
        _entry_by_id(configs, target)["label"] = check_label(chain_id, label, keep=target)

    _edit_configs(chain_id, edit)
    return config_entry(chain_id, target)


def set_active_config(chain_id: str, config_id: str) -> None:
    """Make one set of settings the chain's live one and switch every other off.

    One at a time is a rule about the sources rather than about taste: every set on a chain
    reads the same台子 at the same rate limits off the same node, and the ticker starts one
    round per chain per slot - so a second live set is a round waiting for a slot that has
    already gone by. Writing every flag in one pass is also what repairs a file that was
    hand-edited into two `enabled`: the next save writes it back to one.
    """
    def edit(configs: Any) -> None:
        target = _entry_by_id(configs, config_id)
        for cfg in configs:
            cfg["enabled"] = cfg is target

    _edit_configs(chain_id, edit)


def delete_config(chain_id: str, config_id: str) -> dict:
    """Drop one set of settings from its chain file, and hand back what it was.

    Refused twice over: `locked` is the set every chain keeps, and the live one is refused
    because dropping it would leave the chain with nothing due - switching first is one
    save, and the failure it avoids is a chain that silently stops running.

    Its report library is not this function's business, but the order is: the file goes
    first and the caller drops the rows second, which is why the entry comes back. If the
    second half fails the leftovers are rows nobody can reach; the other order leaves a
    chain still offering a set of settings whose history has already been destroyed.
    """
    entry = config_entry(chain_id, config_id)
    if entry.get("locked"):
        raise ConfigRefused(f"「{entry['label']}」是这条链的默认配置，删不掉")
    if entry.get("enabled"):
        raise ConfigRefused(f"「{entry['label']}」正在生效，先切到别的配置再删")
    target = entry["id"]

    def edit(configs: Any) -> None:
        for position, cfg in enumerate(configs):
            if str(cfg.get("id") or "") == target:
                del configs[position]
                return
        raise KeyError(target)

    _edit_configs(chain_id, edit)
    return entry


def forget_library(chain_id: str, config_id: str) -> dict[str, int]:
    """Drop a set of settings' rows and its rounds' raw responses, and say what went.

    Runs after `delete_config` has taken the entry off the chain file, and the order is the
    whole of the safety here: a set of settings that is off the menu but still has rows is a
    cleanup that did not finish, while the rows gone from a set of settings still on the
    menu is a report library destroyed with nothing to show for it.

    The raw responses are dropped by run id - `data/raw/<run_id>/`, which is how the round
    wrote them - and a failure to remove one is ignored rather than raised: by the time this
    runs the rounds are gone from the database, and a directory nobody can reach is not worth
    failing a delete that has already happened.
    """
    from .store import db

    run_ids = db.library_run_ids(chain_id, config_id)
    sizes = db.forget_config(chain_id, config_id)
    for run_id in run_ids:
        shutil.rmtree(RAW_DIR / run_id, ignore_errors=True)
    return sizes

#: The five sections that make up "a chain's settings". Everything a round does is
#: decided inside these, which is why a settings version can be replayed later.
SETTINGS_SECTIONS = ("schedule", "pipeline", "report", "alerts", "sources")


def settings_payload(chain_id: str, version_id: int | None = None,
                     config_id: str | None = None) -> dict:
    """The settings a round ran under.

    A round is archived under the口径 of its own time, so this takes the version the run
    pinned rather than whatever is in force when somebody reads the page. The stored
    snapshot is overlaid on the YAML instead of replacing it: a knob added to the config
    after a version was saved still exists, at its YAML default.
    """
    import json

    config_cfg = config_entry(chain_id, config_id)
    payload = {key: config_cfg.get(key) for key in SETTINGS_SECTIONS}
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
    # Read against the YAML's shape, not the snapshot's: a knob the config dropped lives
    # on in every version saved while it existed, and `prune_unknown` is what stops it
    # reaching a round (and the archive) as if it were still a setting.
    return merge_patch(payload, prune_unknown(payload, json.loads(row["payload_json"])))



def live_schedule(chain_id: str, config_id: str | None = None) -> dict:
    """The schedule the ticker obeys: the saved one, not the file's.

    The page's two knobs are saved as a version, while the file keeps the defaults it
    shipped with. Reading the file here instead would mean the hours an operator picks never
    fire - the ticker would keep starting rounds at the ones they replaced - so this is what
    the ticker reads, and everything that describes the file keeps reading the file.
    """
    payload = settings_payload(chain_id, latest_settings_version(chain_id, config_id),
                              config_id)
    return dict(payload.get("schedule") or {})


def latest_settings_version(chain_id: str, config_id: str | None = None) -> int | None:
    '''The version a round created now should pin.

    Saving a version is only half the promise - the row has to be *used*, and a run is
    where that happens. A round pins the version in force when it is created, never at
    the moment it happens to start: a save landing while a round waits in the queue must
    not move a knob under a round already written down and about to read it.

    `None` when this set of settings has never been saved. The YAML is then the whole
    truth, and a
    run that carries no version says so rather than pinning a snapshot nobody made.
    '''
    from .store import db

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM settings_versions WHERE chain_id = ? AND config_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (chain_id, config_entry(chain_id, config_id)["id"]),
        ).fetchone()
    finally:
        conn.close()
    return int(row["id"]) if row is not None else None


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


def prune_unknown(base: dict, payload: dict) -> dict:
    """Keep only the leaves whose path `base` also has.

    A saved version is the YAML merged with every version saved before it, and a merge
    only ever adds. So a knob the config dropped - a renamed one, or a rule that is now
    fixed in code - stays in the snapshot for ever and is copied into every version after
    it. Nothing reads it, which is exactly why it went unnoticed; what it did do is make a
    stored version describe a round that never ran that way.

    `base` is a shape and nothing else. A key that is a group on one side and a value on
    the other is left as the payload has it: refusing that mismatch is the save path's
    job (`_reject_unknown_paths`), not a reader's business.
    """
    out: dict = {}
    for key, value in (payload or {}).items():
        here = (base or {}).get(key)
        if isinstance(here, dict) and isinstance(value, dict):
            out[key] = prune_unknown(here, value)
        elif key in (base or {}):
            out[key] = value
    return out


def interval_slots(hours: int, start_hour: int = DEFAULT_START_HOUR) -> list[int]:
    """The trigger hours an interval produces, walked forward from its anchor.

    The interval says how many, the anchor says which: three hours from 02:00 is
    02/05/08/11/14/17/20/23, and the same interval from 05:00 is a different eight hours.
    Zero hours apart is how a chain says "never" - it is the value the page offers as
    不排期 - and an interval that does not divide the day walks the shipped three-hour grid
    from the anchor rather than inventing a short last gap; the page only offers divisors.
    """
    hours = int(hours)
    if hours <= 0:
        return []
    start = int(start_hour) % 24
    step = hours if not 24 % hours else 3
    return [(start + h) % 24 for h in range(0, 24, step)]
