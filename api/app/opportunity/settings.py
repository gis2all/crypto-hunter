"""新机会的设定：一份 yaml，读在调用处，写在设定页（2026-10-19, user）。

The module's knobs were Python constants - every one of them something this session changed by
editing code (`MAX_SOURCES`, `MAX_CALLS`, `MAX_CLASSIFY`, `MAX_EXTRACT`, `MAX_TRACES`, `X_INTERVAL`,
the window and slice). They are settings, not facts, so they live in `config/`:

- `config/opportunity.example.yaml` is the tracked template, with the defaults written out;
- `config/opportunity.yaml` is the machine's own copy and appears **when the reader saves one** -
  not seeded at startup, because a file that exists only after a save keeps the defaults in code
  (where the tests and the constants already are) and the overrides in one place;
- `value(key)` answers the effective setting: the file's value when it has one, else the default.

Same split as `config/chains/*.example.yaml` and its live copy, and the same rule as the rest of the
repo: settings are not stored in the database.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .. import config

log = logging.getLogger("crypto-hunter.opportunity")

EXAMPLE_PATH = Path(str(config.ROOT / "config" / "opportunity.example.yaml"))
LIVE_PATH = Path(str(config.ROOT / "config" / "opportunity.yaml"))

#: Every knob, with the words the 设定 page prints and the range it accepts. **The defaults here are
#: the truth**; the yaml only overrides them, which is what keeps a missing file harmless.
FIELDS = (
    {"key": "sources_per_scan", "label": "读源数", "default": 40, "min": 1, "max": 200,
     "hint": "一次扫描读 watchlist 的前 N 个（按最久没读过的先读）。只算查询行；"
             "17 条查询全读一轮约 22 分钟。"},
    {"key": "calls_per_scan", "label": "X 调用上限", "default": 40, "min": 1, "max": 300,
     "hint": "查询行一条会切成好几片，所以预算按调用次数算，不按行数。40 次约 6 分钟。"},
    {"key": "x_interval", "label": "调用间隔（秒）", "default": 5.0, "min": 1.0, "max": 60.0,
     "hint": "实测 2.5 秒时第 13 个连续请求开始 429，而 CLI 自己的退避把每个失败变成四十秒。"},
    {"key": "classify_per_scan", "label": "分组帖子上限", "default": 1000, "min": 1, "max": 2000,
     "hint": "一次分组最多 N 条；完整正文不截断，超过上限直接报错，不拆批、不留下轮。"},
    {"key": "extract_per_scan", "label": "抽取项目上限", "default": 40, "min": 1, "max": 200,
     "hint": "一个项目组＝一次抽取；40 个约 4 分钟，超出的留在未抽取。"},
    {"key": "traces_per_scan", "label": "溯源项目上限", "default": 200, "min": 0, "max": 500,
     "hint": "按项目去重后最多溯源 N 个；并发 4，先解析官方号，再读官网与文档。0 表示不溯源。"},
    {"key": "window_hours", "label": "查询窗口（小时）", "default": 24, "min": 1, "max": 720,
     "hint": "一条查询往回看多久。窗口 ÷ 片长 = 这条查询的调用次数。"},
    {"key": "slice_hours", "label": "切片长度（小时）", "default": 4, "min": 1, "max": 168,
     "hint": "每次请求最多 40 条，切片是解法：片太长热词会被截断，太短则吃光预算。"},
    {"key": "pass_score", "label": "过闸下限分", "default": 30, "min": 0, "max": 200,
     "hint": "资格规则命中之后，总分仍要达到它。总分只决定排序，不决定过闸。"},
)

_BY_KEY = {field["key"]: field for field in FIELDS}

#: The page's blocks, in the order it prints them. Each block is one card in the ARC-style
#: settings page; the field objects themselves stay flat in FIELDS so `value()` and the yaml
#: file keep their one-key-per-setting shape.
BLOCKS = (
    {"key": "collect", "title": "采集", "columns": 1, "groups": (
        {"key": "budget", "title": "扫描预算",
         "keys": ("sources_per_scan", "calls_per_scan", "x_interval")},
    )},
    {"key": "analyze", "title": "分析", "columns": 1, "groups": (
        {"key": "models", "title": "模型预算",
         "keys": ("classify_per_scan", "extract_per_scan", "traces_per_scan")},
    )},
    {"key": "query", "title": "查询与闸", "columns": 1, "groups": (
        {"key": "timing", "title": "时间与闸门",
         "keys": ("window_hours", "slice_hours", "pass_score")},
    )},
)

_cache: dict[str, Any] = {}


def _coerce(field: dict, raw: Any) -> Any:
    """One value, as the field's own type, clamped to its range. A bad value keeps the default."""
    default = field["default"]
    try:
        value = float(raw) if isinstance(default, float) else int(raw)
    except (TypeError, ValueError):
        return default
    if isinstance(default, float):
        value = round(value, 2)
    return max(field["min"], min(field["max"], value))


def _file() -> dict[str, Any]:
    """Only what the machine's own file carries, coerced. An absent key is absent here."""
    document: dict[str, Any] = {}
    try:
        if LIVE_PATH.exists():
            document = yaml.safe_load(LIVE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        log.exception("opportunity: the settings file could not be read, using the defaults")
    if not isinstance(document, dict):
        document = {}
    return {key: _coerce(_BY_KEY[key], raw) for key, raw in document.items() if key in _BY_KEY}


def read() -> dict[str, Any]:
    """The effective settings: the file's values over the defaults in `FIELDS`."""
    over = _file()
    return {key: over.get(key, field["default"]) for key, field in _BY_KEY.items()}


def value(key: str, default: Any = None) -> Any:
    """One setting, read at call time so a saved change reaches the next scan, not the next restart.

    **The caller's default is the default**: the file overrides it when it names the key, and the
    module constants stay the truth otherwise (which is also what keeps them monkeypatchable in a
    test, and what makes a missing file harmless).
    """
    return _file().get(key, default)


def write(patch: dict[str, Any]) -> dict[str, Any]:
    """Save what the 设定 page sent, and answer the effective settings.

    Only known keys are written, and only values that differ from the defaults: a key nothing
    reads would be the silent switch this app does not ship, and a file full of defaults is a
    second copy of FIELDS that can drift. Saving everything back to defaults removes the file.
    """
    current = read()
    for key, raw in (patch or {}).items():
        if key in _BY_KEY:
            current[key] = _coerce(_BY_KEY[key], raw)
    overrides = {key: value for key, value in current.items()
                 if value != _BY_KEY[key]["default"]}
    body = ("# 新机会的设定。默认值写在 api/app/opportunity/settings.py 的 FIELDS 里，\n"
            "# 这个文件只保存被改过的那些；删掉它即回到默认。\n")
    if overrides:
        LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_PATH.write_text(body + yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
    elif LIVE_PATH.exists():
        LIVE_PATH.unlink()
    log.info("opportunity: settings saved (%d overrides)", len(overrides))
    return current


def form() -> dict:
    """The fields and the page's block layout: key, label, current value, range and hint."""
    current = read()
    fields = [{"key": field["key"], "label": field["label"], "value": current[field["key"]],
               "min": field["min"], "max": field["max"], "hint": field["hint"],
               "kind": "float" if isinstance(field["default"], float) else "int"}
              for field in FIELDS]
    by_key = {field["key"]: field for field in fields}
    blocks = [
        {"key": block["key"], "title": block["title"],
         "columns": int(block.get("columns") or 1),
         "groups": [
             {"key": group["key"], "title": group["title"],
              "fields": [by_key[key] for key in group["keys"]]}
             for group in block["groups"]
         ]}
        for block in BLOCKS
    ]
    return {"fields": fields, "blocks": blocks,
            "dirty_hint": "保存并生效后，下一轮扫描使用新值。"}
