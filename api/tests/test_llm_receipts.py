"""付费收据：同一份材料只付一次钱、预算数的是真发出去的请求、不确定的结果不自动重发。

这些是 AIHOT `providers/receipts.ts` 的三条纪律，每一条都能用库里的行回答，所以每条都在
这里用行断言，而不是只看函数返回了什么。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.llm import receipts
from app.store import db


@pytest.fixture()
def paid_env(isolated_db, monkeypatch):
    monkeypatch.delenv("CH_LLM_BUDGETS", raising=False)
    return isolated_db


def _rows(sql: str, *args):
    conn = db.connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _outcome(text: str = "answer") -> receipts.Outcome:
    return receipts.Outcome(text=text, prompt_tokens=10, completion_tokens=5)


def _insert(*, key: str, status: str, ago_seconds: int = 0, attempt_status: str | None = None,
            attempt_error: str | None = None) -> int:
    """Hand-write a receipt the way a crash would leave it behind."""
    stamp = (datetime.now().astimezone() - timedelta(seconds=ago_seconds)).isoformat(
        timespec="seconds")
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO llm_receipts (logical_key, service, model, purpose, status, attempts,"
            " created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (key, "llm", "m", "p", status, stamp, stamp))
        receipt_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO llm_receipt_attempts (receipt_id, attempt, service, model, origin,"
            " status, error, started_at) VALUES (?,1,?,?,'live',?,?,?)",
            (receipt_id, "llm", "m", attempt_status or status, attempt_error, stamp))
        return receipt_id
    finally:
        conn.close()


def test_one_logical_request_is_paid_once(paid_env):
    calls: list[int] = []

    def call():
        calls.append(1)
        return _outcome()

    first = receipts.paid(purpose="score", identity={"m": "x"}, call=call, model="m")
    second = receipts.paid(purpose="score", identity={"m": "x"}, call=call, model="m")

    assert first.reused is False
    assert first.text == "answer"
    assert second.reused is True and second.text == "answer"
    assert second.receipt_id == first.receipt_id
    assert len(calls) == 1, "同一个逻辑请求不该付第二次钱"
    assert len(_rows("SELECT id FROM llm_receipt_attempts")) == 1


def test_a_different_attempt_tag_is_a_different_request(paid_env):
    """两次独立评分问的是同一份材料: 不给它们各自的标签, 第二次会被重放成第一个数。"""
    calls: list[int] = []
    for tag in ("score-1", "score-2"):
        receipts.paid(purpose="score", identity={"m": "x"},
                      call=lambda: (calls.append(1), _outcome())[1], model="m",
                      attempt_tag=tag)
    assert len(calls) == 2
    assert len(_rows("SELECT id FROM llm_receipts")) == 2


def test_the_key_binds_everything_that_decides_the_answer(paid_env):
    calls: list[int] = []
    for identity in ({"m": "x"}, {"m": "y"}):
        receipts.paid(purpose="score", identity=identity,
                      call=lambda: (calls.append(1), _outcome())[1], model="m")
    assert len(calls) == 2, "材料变了就是另一个请求"


def test_a_budget_counts_attempts_and_stops_the_service(paid_env, monkeypatch):
    monkeypatch.setenv("CH_LLM_BUDGETS",
                       json.dumps({"llm": {"per_minute": 2, "per_hour": 100, "per_day": 100}}))
    call = lambda: _outcome()  # noqa: E731
    receipts.paid(purpose="p", identity={"i": 1}, call=call, model="m")
    receipts.paid(purpose="p", identity={"i": 2}, call=call, model="m")

    with pytest.raises(receipts.BudgetExceeded) as caught:
        receipts.paid(purpose="p", identity={"i": 3}, call=call, model="m")
    assert caught.value.window == "minute"
    assert caught.value.retry_after == 60
    assert len(_rows("SELECT id FROM llm_receipts")) == 2, "被拦下的那次不落收据"


def test_a_zero_budget_stops_the_service(paid_env, monkeypatch):
    monkeypatch.setenv("CH_LLM_BUDGETS",
                       json.dumps({"llm": {"per_minute": 0, "per_hour": 0, "per_day": 0}}))
    with pytest.raises(receipts.BudgetExceeded) as caught:
        receipts.paid(purpose="p", identity={"i": 1}, call=lambda: _outcome(), model="m")
    assert caught.value.window == "stopped"


def test_no_budget_means_unlimited(paid_env):
    for index in range(3):
        receipts.paid(purpose="p", identity={"i": index}, call=lambda: _outcome(), model="m")
    assert len(_rows("SELECT id FROM llm_receipts")) == 3


def test_a_rejected_request_may_be_paid_again(paid_env):
    calls: list[int] = []

    def call():
        calls.append(1)
        raise RuntimeError("HTTP 503")

    def classify(exc):
        return "failed"

    with pytest.raises(RuntimeError):
        receipts.paid(purpose="p", identity={"i": 1}, call=call, model="m", classify=classify)
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "failed"

    receipts.paid(purpose="p", identity={"i": 1}, call=lambda: _outcome(), model="m")
    assert len(calls) == 1
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "received"
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] != "unknown"


def test_an_unknown_request_is_never_resent(paid_env):
    calls: list[int] = []

    def call():
        calls.append(1)
        raise TimeoutError("no idea what happened")

    with pytest.raises(TimeoutError):
        receipts.paid(purpose="p", identity={"i": 1}, call=call, model="m")
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "unknown"

    with pytest.raises(receipts.ReceiptUnknown):
        receipts.paid(purpose="p", identity={"i": 1}, call=call, model="m")
    assert len(calls) == 1, "结果不确定就不该自动重发"


def test_a_crash_leaves_a_pending_row_that_ages_into_unknown(paid_env):
    receipt_id = _insert(key="llm:p:m:abc:0", status="pending", ago_seconds=11 * 60)
    assert receipts.mark_stale_pending() == 1
    assert _rows("SELECT status FROM llm_receipts WHERE id=?", receipt_id)[0]["status"] == "unknown"
    assert _rows("SELECT status FROM llm_receipt_attempts WHERE receipt_id=?",
                 receipt_id)[0]["status"] == "unknown"


def test_a_fresh_pending_row_is_not_touched(paid_env):
    receipt_id = _insert(key="llm:p:m:abc:0", status="pending", ago_seconds=30)
    assert receipts.mark_stale_pending() == 0
    assert _rows("SELECT status FROM llm_receipts WHERE id=?", receipt_id)[0]["status"] == "pending"


def test_an_unknown_receipt_is_released_once_after_half_an_hour(paid_env):
    old = _insert(key="llm:p:m:old:0", status="unknown", ago_seconds=31 * 60,
                  attempt_status="unknown", attempt_error="boom")
    fresh = _insert(key="llm:p:m:fresh:0", status="unknown", ago_seconds=60,
                    attempt_status="unknown", attempt_error="boom")

    assert receipts.auto_release_unknown(limit=100) == 1
    assert _rows("SELECT status FROM llm_receipts WHERE id=?", old)[0]["status"] == "failed"
    assert _rows("SELECT status FROM llm_receipts WHERE id=?", fresh)[0]["status"] == "unknown"
    assert _rows("SELECT error FROM llm_receipt_attempts WHERE receipt_id=?",
                 old)[0]["error"] == receipts.AUTO_RELEASE_NOTE

    # 第二次未知: 那张收据上有「已经自动放行过」的记号, 所以不再自动放行。
    conn = db.connect()
    try:
        conn.execute("UPDATE llm_receipts SET status='unknown', updated_at=? WHERE id=?",
                     ((datetime.now().astimezone() - timedelta(hours=2)).isoformat(
                         timespec="seconds"), old))
    finally:
        conn.close()
    assert receipts.auto_release_unknown(limit=100) == 0


def test_an_unusable_answer_is_released_for_a_later_attempt(paid_env):
    receipts.paid(purpose="p", identity={"i": 1}, call=lambda: _outcome("bad"), model="m")
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "received"

    receipts.reject_received(receipts.paid(
        purpose="p", identity={"i": 1}, call=lambda: _outcome("bad"), model="m").receipt_id,
        "draft was not JSON")
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "failed"

    calls: list[int] = []
    receipts.paid(purpose="p", identity={"i": 1},
                  call=lambda: (calls.append(1), _outcome("good"))[1], model="m")
    assert len(calls) == 1, "用不了的答案不该被重放, 而是允许再付一次"
    assert _rows("SELECT response FROM llm_receipts")[0]["response"] == "good"


def test_completing_a_receipt_keeps_the_answer_reusable(paid_env):
    result = receipts.paid(purpose="p", identity={"i": 1}, call=lambda: _outcome(), model="m")
    receipts.complete(result.receipt_id)
    assert _rows("SELECT status FROM llm_receipts")[0]["status"] == "completed"

    calls: list[int] = []
    again = receipts.paid(purpose="p", identity={"i": 1},
                          call=lambda: (calls.append(1), _outcome())[1], model="m")
    assert calls == []
    assert again.reused is True


def test_recover_settles_both_kinds(paid_env):
    _insert(key="llm:p:m:stale:0", status="pending", ago_seconds=11 * 60)
    _insert(key="llm:p:m:lost:0", status="unknown", ago_seconds=31 * 60,
            attempt_status="unknown", attempt_error="boom")
    report = receipts.recover()
    assert report == {"stale": 1, "released": 1}
