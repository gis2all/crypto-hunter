"""The rail's status strip: what counts as a dead model or a dead session.

The readings are pure: the model side is a list of past calls, the X side is a list of pool
rows. Nothing here may reach a gateway or X, so every test hands the function its own list.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import service_health
from app.store import db

LIVE = {"base_url": "https://gateway/v1", "model": "m", "api_key": "sk-live"}


@pytest.fixture(autouse=True)
def _own_ledger(isolated_db):
    """Every reading below reads runtime_state and llm_calls; none may touch the real ones."""
    return isolated_db


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _calls(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(service_health.llm, "configured", lambda: True)
    monkeypatch.setattr(service_health.llm, "settings", lambda: dict(LIVE))
    monkeypatch.setattr(service_health.db, "llm_calls_recent",
                        lambda limit=20: rows)


def _call(ok: bool, error: str = "", at: str | None = None) -> dict:
    return {"purpose": "monitor_extract", "model": "m", "ok": ok,
            "error": error, "created_at": at or _now()}


def _test(ok: bool, *, at: str | None = None, **over) -> None:
    value = {"ok": ok, "base_url": LIVE["base_url"], "model": LIVE["model"],
             "key_hash": service_health._key_hash(LIVE["api_key"]),
             "note": "", "at": at or _now()}
    value.update(over)
    db.runtime_set("llm.test", value)


def test_a_quota_error_is_a_dead_model(monkeypatch):
    _calls(monkeypatch, [_call(False, "HTTPStatusError('upstream 429: GoUsageLimitError "
                                       "usage limit exceeded, limitName monthly')")])
    state = service_health.llm_health()
    assert state["level"] == "down"
    assert "额度用尽" in state["label"]


def test_a_bad_key_is_a_dead_model(monkeypatch):
    _calls(monkeypatch, [_call(False, "HTTPStatusError('upstream 401: unauthorized')")])
    state = service_health.llm_health()
    assert state["level"] == "down"
    assert "密钥无效" in state["label"]


def test_a_missing_model_is_a_dead_model(monkeypatch):
    _calls(monkeypatch, [_call(False, "HTTPStatusError('upstream 404: model not found')")])
    state = service_health.llm_health()
    assert state["level"] == "down"
    assert "模型不存在" in state["label"]


def test_a_plain_rate_limit_is_only_a_warning(monkeypatch):
    _calls(monkeypatch, [_call(False, "HTTPStatusError('upstream 429: Too Many Requests')")])
    state = service_health.llm_health()
    assert state["level"] == "warn"
    assert "限流" in state["label"]


def test_the_newest_success_clears_the_reading(monkeypatch):
    _calls(monkeypatch, [_call(True), _call(False, "429 GoUsageLimitError")])
    assert service_health.llm_health()["level"] == "ok"


def test_an_old_failure_is_history_not_state(monkeypatch):
    _calls(monkeypatch, [_call(False, "401 unauthorized", at="2020-01-01T00:00:00+08:00")])
    assert service_health.llm_health()["level"] == "ok"


def test_a_passing_test_clears_an_old_quota_error(monkeypatch):
    """The button is the freshest evidence: a new gateway that answers must clear the old 429."""
    _calls(monkeypatch, [_call(False, "429 GoUsageLimitError")])
    _test(True)
    assert service_health.llm_health()["level"] == "ok"


def test_a_test_of_an_unsaved_draft_does_not_clear_the_error(monkeypatch):
    _calls(monkeypatch, [_call(False, "429 GoUsageLimitError")])
    _test(True, base_url="http://127.0.0.1:3425/v1")
    assert service_health.llm_health()["level"] == "down"


def test_a_failed_test_is_the_reading(monkeypatch):
    _calls(monkeypatch, [_call(True)])
    _test(False, note="HTTPStatusError('upstream 401: unauthorized')")
    state = service_health.llm_health()
    assert state["level"] == "down"
    assert "密钥无效" in state["label"]


def test_a_new_endpoint_retires_the_old_failure(monkeypatch):
    _calls(monkeypatch, [_call(False, "429 GoUsageLimitError",
                               at="2020-01-01T00:00:00+08:00")])
    db.runtime_set("llm.saved", {"at": _now()})
    assert service_health.llm_health()["level"] == "ok"


def test_no_session_at_all_is_a_dead_x(monkeypatch):
    monkeypatch.setattr(service_health.system_settings, "x_sessions", lambda: [])
    state = service_health.x_health()
    assert state["level"] == "down"
    assert "未配置" in state["label"]


def test_every_session_disabled_is_a_dead_x(monkeypatch):
    monkeypatch.setattr(service_health.system_settings, "x_sessions", lambda: [
        {"label": "a", "enabled": True, "state": "disabled"},
        {"label": "b", "enabled": False, "state": ""},
    ])
    state = service_health.x_health()
    assert state["level"] == "down"
    assert "全部失效" in state["label"]


def test_a_cooling_session_is_counted_as_unavailable(monkeypatch):
    monkeypatch.setattr(service_health.system_settings, "x_sessions", lambda: [
        {"label": "a", "enabled": True, "state": ""},
        {"label": "b", "enabled": True, "state": "cooling"},
        {"label": "c", "enabled": True, "state": ""},
    ])
    state = service_health.x_health()
    assert state["level"] == "warn"
    assert state["label"] == "X 会话 2/3 可用"


def test_a_failed_probe_survives_a_pool_restart(monkeypatch):
    """The pool forgets its 401 state; the persisted probe must still make it unavailable."""
    monkeypatch.setattr(service_health.system_settings, "x_sessions", lambda: [
        {"label": "main", "enabled": True, "state": "",
         "last_test": {"ok": False, "note": "401 unauthorized", "at": _now()}},
    ])
    state = service_health.x_health()
    assert state["level"] == "down"
    assert "全部失效" in state["label"]


def test_a_passing_probe_does_not_hide_a_failed_session(monkeypatch):
    monkeypatch.setattr(service_health.system_settings, "x_sessions", lambda: [
        {"label": "a", "enabled": True, "state": "",
         "last_test": {"ok": True, "note": "", "at": _now()}},
        {"label": "b", "enabled": True, "state": "",
         "last_test": {"ok": False, "note": "timed out", "at": _now()}},
    ])
    state = service_health.x_health()
    assert state["level"] == "warn"
    assert state["label"] == "X 会话 1/2 可用"


def test_runtime_state_round_trips(isolated_db):
    db.runtime_set("x.test.main", {"ok": False, "note": "401", "at": _now()})
    assert db.runtime_get("x.test.main")["ok"] is False
    assert set(db.runtime_prefix("x.test.")) == {"x.test.main"}
    assert db.runtime_get("x.test.missing") is None
