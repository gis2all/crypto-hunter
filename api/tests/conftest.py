"""Fixtures: an isolated database per test, and an API client on top of it."""

from __future__ import annotations

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from app import config
from app.pipeline import schedule
from app.store import db

#: Rounds the API queued during a test, in the order they were queued. Handed to a test
#: that wants to assert what a request started.
queued: list[tuple[str, str]] = []


class InlineExecutor:
    """The round queue, run on the calling thread.

    A test that starts a round must not wait on a worker to notice it, and must not reach
    five real sources. The queue is kept; the thread is not.
    """

    def submit(self, fn, *args, **kwargs) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # a failing round is one of the cases worth testing
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True) -> None:
        return None


@pytest.fixture(autouse=True)
def no_live_rounds(monkeypatch):
    """A round started by a request is recorded and not run. The queue it goes through is
    the real one, minus the thread: what a request starts is worth asserting on."""
    queued.clear()

    def record(chain_id: str, run_id: str) -> dict:
        queued.append((chain_id, run_id))
        return {"id": run_id, "chain_id": chain_id, "status": "pending"}

    monkeypatch.setattr(schedule.pipeline_run, "run_chain", record)
    monkeypatch.setattr(schedule, "executor", lambda: InlineExecutor())
    return queued


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return config.DB_PATH


@pytest.fixture()
def client(isolated_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
