"""The rail's status strip: is the gateway answering, are the X sessions usable.

Neither reading costs an upstream call. The model reading is the ledger this process already
wrote (`llm_calls`); the X reading is the session pool's own state plus the last probe the
operator ran. A session nobody has used since a restart reads as usable - only a real call or
the test button can tell (2026-10-31).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from . import llm, system_settings
from .store import db


def _at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _reason(error: str) -> tuple[str, str]:
    """One upstream error, in the words the strip can afford. Fatal means "do not wait"."""
    low = (error or "").lower()
    if "401" in low or "403" in low or "unauthorized" in low or "invalid api key" in low:
        return "down", "密钥无效"
    if "402" in low or "insufficient" in low or "balance" in low:
        return "down", "余额不足"
    if "404" in low or "model not found" in low or "invalid model" in low:
        return "down", "模型不存在"
    if "429" in low and ("usage limit" in low or "usagelimit" in low
                       or "quota" in low or "limitname" in low):
        return "down", "额度用尽"
    if "429" in low:
        return "warn", "触发限流"
    if "timeout" in low or "timed out" in low or "connect" in low:
        return "warn", "连不上网关"
    return "warn", "调用失败"


def _detail(row: dict) -> str:
    text = re.sub(r"\s+", " ", str(row.get("error") or "")).strip()
    model = str(row.get("model") or "")
    return ((model + " · " + text) if model else text)[:220]


def _key_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def _saved_cutoff() -> datetime | None:
    saved = db.runtime_get("llm.saved")
    return _at(str((saved or {}).get("at") or "")) if saved else None


def _test_reading() -> dict | None:
    """The button's last answer, but only when it described the wiring that is saved now.

    A test reads the draft on screen. An unsaved success says nothing about the endpoint a
    round would actually call, so its base_url / model / key must match the live settings
    before the strip may trust it (2026-10-31).
    """
    test = db.runtime_get("llm.test")
    if not test:
        return None
    current = llm.settings()
    if str(test.get("base_url") or "") != str(current.get("base_url") or ""):
        return None
    if str(test.get("model") or "") != str(current.get("model") or ""):
        return None
    if str(test.get("key_hash") or "") != _key_hash(str(current.get("api_key") or "")):
        return None
    at = _at(str(test.get("at") or ""))
    if at is None or datetime.now().astimezone() - at > timedelta(hours=24):
        return None
    return test


def llm_health() -> dict:
    """The newest call is the reading; a failure older than a day is history, not state."""
    if not llm.configured():
        return {"level": "down", "label": "模型未配置",
                "detail": "系统设置 · 模型", "at": None}
    now = datetime.now().astimezone()
    cutoff = _saved_cutoff()
    test = _test_reading()
    test_at = _at(str((test or {}).get("at") or ""))
    rows = db.llm_calls_recent(20)
    if cutoff is not None:
        rows = [row for row in rows
                if (at := _at(str(row.get("created_at") or ""))) is not None and at > cutoff]
    last = rows[0] if rows else None
    last_at = _at(str((last or {}).get("created_at") or ""))
    # A probe newer than the last real call is the freshest evidence there is.
    if test_at is not None and (last_at is None or test_at >= last_at):
        if test.get("ok"):
            return {"level": "ok", "label": "", "detail": "测试连接正常",
                    "at": test.get("at")}
        level, reason = _reason(str(test.get("note") or ""))
        label = ("模型不可用 · " + reason) if level == "down" else ("模型" + reason)
        return {"level": level, "label": label,
                "detail": str(test.get("note") or "")[:220], "at": test.get("at")}
    if not rows:
        return {"level": "ok", "label": "", "detail": "还没有调用记录", "at": None}
    stamp = _at(str(last.get("created_at") or ""))
    if not last["ok"] and (stamp is None or now - stamp <= timedelta(hours=24)):
        level, reason = _reason(str(last.get("error") or ""))
        label = ("模型不可用 · " + reason) if level == "down" else ("模型" + reason)
        return {"level": level, "label": label, "detail": _detail(last),
                "at": last.get("created_at")}
    recent = []
    for row in rows:
        at = _at(str(row.get("created_at") or ""))
        if at is not None and now - at <= timedelta(hours=24):
            recent.append(row)
    failed = sum(1 for row in recent if not row["ok"])
    if len(recent) >= 4 and failed * 2 >= len(recent):
        return {"level": "warn", "label": "模型不稳定",
                "detail": "近 24 小时 %d/%d 次调用失败" % (failed, len(recent)),
                "at": recent[0].get("created_at")}
    return {"level": "ok", "label": "", "detail": "", "at": last.get("created_at")}


def x_health() -> dict:
    """A cooling session is configured; a failed persisted probe is still a failure."""
    rows = system_settings.x_sessions()
    if not rows:
        return {"level": "down", "label": "X 会话未配置",
                "detail": "系统设置 · 搜索", "at": None}
    total = len(rows)
    def failed(row: dict) -> bool:
        test = row.get("last_test") or {}
        return row.get("state") == "" and test.get("ok") is False

    usable = [row for row in rows if row.get("enabled")
              and row.get("state") != "disabled" and not failed(row)]
    ready = [row for row in usable if row.get("state") != "cooling"]
    if not usable:
        return {"level": "down", "label": "X 会话全部失效",
                "detail": "、".join(str(row.get("label") or "") for row in rows),
                "at": next((str((row.get("last_test") or {}).get("at") or "")
                            for row in rows if failed(row)), None)}
    if len(ready) < total:
        ready_labels = {str(row.get("label") or "") for row in ready}
        dead = [str(row.get("label") or "") for row in rows
                if str(row.get("label") or "") not in ready_labels]
        detail = "失效或冷却：" + "、".join(dead)
        return {"level": "warn", "label": "X 会话 %d/%d 可用" % (len(ready), total),
                "detail": detail,
                "at": next((str((row.get("last_test") or {}).get("at") or "")
                            for row in rows if failed(row)), None)}
    return {"level": "ok", "label": "", "detail": "", "at": None}
