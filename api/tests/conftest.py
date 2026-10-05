"""Fixtures: an isolated database per test, and an API client on top of it."""

from __future__ import annotations

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from app import config
from app.feed import jobs as feed_jobs
from app.feed import rules as feed_rules
from app.feed.sources import pipeline as feed_pipeline
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

    def record(chain_id: str, run_id: str, config_id: str | None = None) -> dict:
        queued.append((chain_id, run_id))
        return {"id": run_id, "chain_id": chain_id, "config_id": config_id,
                "status": "pending"}

    monkeypatch.setattr(schedule.pipeline_run, "run_chain", record)
    monkeypatch.setattr(schedule, "executor", lambda: InlineExecutor())
    return queued


@pytest.fixture(autouse=True)
def no_live_feed(monkeypatch):
    """采集时钟在测试里不抓真东西。

    它一挂上就想立刻抓一轮 —— 那时四十五个源都是「从没抓过」，于是每个建 app 的测试都会真的
    去敲四十家站点。时钟本身还是起来 (起没起是 test_feed 里可断言的一件事), 只是那一轮换成什么
    都不做。
    """
    monkeypatch.setattr(feed_jobs, "run_due", lambda moment=None: {"ran": {}, "failed": {}})
    # 手动跑一轮 `collect.poll` 的测试也走这条路: 落库之后还有一站「补正文」, 它会拿着夹具里
    # 那些编出来的 url 去读原文 —— 不把它调成"这一轮不补", 每个这样的测试都会真的去敲几十个
    # 域名 (慢, 而且不该)。要测补正文的那几条自己会把它换成真的预算 (test_feed.py 末尾)。
    monkeypatch.setattr(feed_pipeline, "detail",
                        lambda path=None: {"maxFetches": 0, "minBodyChars": 200,
                                           "timeoutSeconds": 5, "retryHours": 24})
    # 写作站同理, 而且更要紧: 它是整条流水线上唯一会花钱的一站, 而测试里那些编出来的记录
    # 不值得问模型. `maxItems: 0` 就是「这一站关掉」——要测写作站的那几条自己换成真预算.
    monkeypatch.setattr(feed_pipeline, "llm",
                        lambda path=None: {"preset": "full", "maxItems": 0, "concurrency": 1,
                                           "timeoutSeconds": 5, "retryHours": 24,
                                           "maxFailures": 1})


@pytest.fixture(autouse=True)
def no_release_gate(monkeypatch):
    """发布门在测试里默认关掉。

    `rules.RELEASE_DELAY_SECONDS` 是"一条从被读到到公开"的三分钟缓冲 —— 生产里它是真的,
    所以夹具写完一条立刻去读, 读到的会是空页. 门本身由 `test_feed_publication.py` 专门测
    (那里把它调回 180), 其余每一条测试关心的都不是"到点了没有".
    """
    monkeypatch.setattr(feed_rules, "RELEASE_DELAY_SECONDS", 0)


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
