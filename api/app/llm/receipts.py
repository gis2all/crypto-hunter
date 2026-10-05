"""付费请求的收据：预算、幂等、重放、释放（AIHOT 的 `providers/receipts.ts`）。

一次付费请求有一个稳定的**逻辑键**：它绑住用途、模型与「决定了输出的那一切」（提示词、
材料、温度、上限）。键相同、上一次又已经拿到答案的请求不再付第二次钱 —— 重跑一整轮、
取消后重来、进程重启，靠的都是这一行把答案捡回来。

真的发出去的每一次请求记一行 attempt，**预算数的是 attempt**：重试躲不过预算，而捡回来的
那次根本没发、也不重复计费。

三种结局分得很清楚：

- `received`  拿到了答案（业务写不完也不影响重放；写完了再 `complete`）。
- `failed`    对面明确没收下这次请求（4xx / 内容审核 / 答案不可用），下次可以再付一次。
- `unknown`   不知道对面收没收到（超时、掉线、进程死了）。**不再自动重发** —— 自动重发就是
  拿钱去猜一个有答案的请求。它只被放行一次（`auto_release_unknown`），之后等人核对账单。

崩溃留下的 `pending` 行超过 `STALE_PENDING_SECONDS` 按 `unknown` 处理，所以一条丢失的答案
最多重复花一次钱。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import config
from ..store import db

log = logging.getLogger(__name__)

#: 崩溃留下的 pending 行多久算陈旧 (AIHOT 的 `PENDING_STALE_MS`)。10 分钟远大于一次调用最长
#: 180 秒的时长, 所以不会把一次还在飞的请求误判。
STALE_PENDING_SECONDS = 10 * 60

#: 结果未知的收据多久自动放行一次 (AIHOT 的 `AUTO_RELEASE_AFTER_MS`)。
AUTO_RELEASE_SECONDS = 30 * 60

#: 自动放行过的那一次带上这句。它是「已经自动放行过」的判据 (AIHOT 同款), 所以一个收据只自动
#: 放行一次: 第二次未知就等人工核对账单。
AUTO_RELEASE_NOTE = "自动放行：结果未知超过 30 分钟，未核对是否计费"

#: 这一档在预算表里的名字 (AIHOT 的 `budgets.service`)。向量与搜索走各自的客户端, 不进这里。
SERVICE = "llm"

_RECEIVED = ("received", "completed")


class ReceiptError(RuntimeError):
    """这一档现在不能问 —— 调用方按「拿不到答案」处理, 不是「这次请求有问题」。"""


class BudgetExceeded(ReceiptError):
    def __init__(self, service: str, window: str, retry_after: int):
        super().__init__("budget for %s exhausted (%s)" % (service, window))
        self.service = service
        self.window = window
        self.retry_after = retry_after


class ReceiptBusy(ReceiptError):
    """同一个逻辑请求正在飞。"""


class ReceiptUnknown(ReceiptError):
    """上一次的结果不知道, 还没被放行。"""

    def __init__(self, receipt_id: int, message: str):
        super().__init__(message)
        self.receipt_id = receipt_id


@dataclass
class Outcome:
    """一次调用拿到了什么。`text` 是答案本体, 也是重放时捡回来的那一份。"""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class Result:
    text: str
    reused: bool
    receipt_id: int


def stable_json(value: object) -> str:
    """AIHOT 的 `stableJson`: 键排序、无空格, 同一份材料永远算出同一个哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def logical_key(*, service: str, purpose: str, model: str | None, identity: object,
                attempt_tag: str | None = None) -> str:
    """AIHOT 的 `logicalKeyFor`: 服务 / 用途 / 模型 / 材料哈希 / 显式重跑标签。

    `attempt_tag` 留给「同一份材料有意问两次」的档 (AIHOT 的 `score-1` / `score-2`):
    不给它, 两次独立评分会被当成同一次请求而被重放成同一个数。
    """
    return ":".join([service, purpose, model or "-", digest(identity), attempt_tag or "0"])


def budget_for(service: str = SERVICE) -> tuple[int, int, int] | None:
    """这一档的预算 (每分钟 / 每小时 / 每天); 没配 = 不限。

    AIHOT 的默认行在迁移里 (llm 300/6000/40000), 运营删掉那行就是「不限」—— 这里用同一个
    语义: 没写就是不限, 写 0 就是停。

    先看环境变量 (文件里的值也被 `config` 放到那里, 见 `flatten`), 文件只是没被读进来时的
    退路 —— 真正生效的那一份永远是同一个。
    """
    raw = os.environ.get("CH_LLM_BUDGETS", "").strip()
    if not raw:
        try:
            document = config.read_document(config.SYSTEM_PATH) or {}
        except Exception:  # 配置坏了不该让一轮采集挂掉: 按不限处理
            return None
        value = config._dig(document, "llm.budgets") if isinstance(document, dict) else None
        if not isinstance(value, dict):
            return None
        raw = json.dumps(value, ensure_ascii=False)
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("CH_LLM_BUDGETS 不是 JSON; 按不限处理")
        return None
    row = parsed.get(service) if isinstance(parsed, dict) else None
    if not isinstance(row, dict):
        return None
    try:
        return (int(row["per_minute"]), int(row["per_hour"]), int(row["per_day"]))
    except (KeyError, TypeError, ValueError):
        log.warning("llm.budgets.%s 不是 {per_minute, per_hour, per_day}; 按不限处理", service)
        return None


def _stamp(moment: datetime | None = None) -> datetime:
    return moment or datetime.now().astimezone()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _older_than(stamp: str, seconds: float, now: datetime) -> bool:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() >= seconds
    except ValueError:  # 时间戳坏了: 当作很旧, 让它走放行那条路
        return True


def _check_budget(conn: sqlite3.Connection, service: str, now: datetime) -> None:
    limit = budget_for(service)
    if limit is None:
        return
    per_minute, per_hour, per_day = limit
    if per_minute <= 0 or per_hour <= 0 or per_day <= 0:
        raise BudgetExceeded(service, "stopped", 3600)
    row = conn.execute(
        "SELECT"
        " (SELECT count(*) FROM llm_receipt_attempts"
        "   WHERE service=? AND origin='live' AND started_at>?) AS minute,"
        " (SELECT count(*) FROM llm_receipt_attempts"
        "   WHERE service=? AND origin='live' AND started_at>?) AS hour,"
        " (SELECT count(*) FROM llm_receipt_attempts"
        "   WHERE service=? AND origin='live' AND started_at>?) AS day",
        (service, _iso(now - timedelta(minutes=1)),
         service, _iso(now - timedelta(hours=1)),
         service, _iso(now - timedelta(days=1))),
    ).fetchone()
    if row["minute"] >= per_minute:
        raise BudgetExceeded(service, "minute", 60)
    if row["hour"] >= per_hour:
        raise BudgetExceeded(service, "hour", 600)
    if row["day"] >= per_day:
        raise BudgetExceeded(service, "day", 3600)


def _mark_unknown(conn: sqlite3.Connection, receipt_id: int, reason: str, now: datetime) -> None:
    conn.execute("UPDATE llm_receipts SET status='unknown', error=?, updated_at=? WHERE id=?",
                 (reason, _iso(now), receipt_id))
    conn.execute(
        "UPDATE llm_receipt_attempts SET status='unknown', error=?, finished_at=?"
        " WHERE receipt_id=? AND status='pending'",
        (reason, _iso(now), receipt_id))


@dataclass
class _Claim:
    receipt_id: int
    attempt_id: int
    attempt: int
    reuse: str | None = None


def _claim(*, key: str, service: str, purpose: str, model: str | None, subject: str | None,
           request_json: str, now: datetime) -> _Claim:
    """先落一行再打电话 (AIHOT 的声明段)。整段在一把写锁里, 所以预算不会被并发绕过。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, status, response, attempts, updated_at FROM llm_receipts"
            " WHERE logical_key=?", (key,)).fetchone()
        if row is not None and row["status"] in _RECEIVED:
            conn.execute("COMMIT")
            return _Claim(row["id"], 0, int(row["attempts"]), reuse=row["response"])
        if row is not None:
            if row["status"] == "pending":
                if not _older_than(row["updated_at"], STALE_PENDING_SECONDS, now):
                    conn.execute("ROLLBACK")
                    raise ReceiptBusy("收据 %s 正在飞" % row["id"])
                _mark_unknown(conn, row["id"], "placeholder went stale without a recorded result", now)
                conn.execute("COMMIT")
                raise ReceiptUnknown(row["id"], "收据 %s 超时未落定, 等放行" % row["id"])
            if row["status"] == "unknown":
                conn.execute("ROLLBACK")
                raise ReceiptUnknown(row["id"], "收据 %s 结果未知, 等放行" % row["id"])
            # failed: 对面明确没收下, 这次可以再付一次
            _check_budget(conn, service, now)
            attempt = int(row["attempts"]) + 1
            conn.execute(
                "UPDATE llm_receipts SET status='pending', attempts=?, error=NULL, updated_at=?"
                " WHERE id=?", (attempt, _iso(now), row["id"]))
            receipt_id = row["id"]
        else:
            _check_budget(conn, service, now)
            cursor = conn.execute(
                "INSERT INTO llm_receipts (logical_key, service, model, purpose, subject,"
                " status, request_json, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,1,?,?)",
                (key, service, model, purpose, subject, "pending", request_json,
                 _iso(now), _iso(now)))
            receipt_id = int(cursor.lastrowid)
            attempt = 1
        cursor = conn.execute(
            "INSERT INTO llm_receipt_attempts (receipt_id, attempt, service, model, origin,"
            " status, started_at) VALUES (?,?,?,?,'live','pending',?)",
            (receipt_id, attempt, service, model, _iso(now)))
        attempt_id = int(cursor.lastrowid)
        conn.execute("COMMIT")
        return _Claim(receipt_id, attempt_id, attempt)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _settle(receipt_id: int, attempt_id: int, status: str, *, error: str | None = None,
            outcome: Outcome | None = None, latency_ms: int | None = None,
            now: datetime | None = None) -> None:
    moment = _stamp(now)
    conn = db.connect()
    try:
        if status == "received":
            conn.execute(
                "UPDATE llm_receipts SET status='received', response=?, error=NULL,"
                " received_at=?, updated_at=? WHERE id=?",
                (outcome.text if outcome else "",
                 _iso(moment), _iso(moment), receipt_id))
            conn.execute(
                "UPDATE llm_receipt_attempts SET status='received', prompt_tokens=?,"
                " completion_tokens=?, latency_ms=?, finished_at=? WHERE id=?",
                (outcome.prompt_tokens if outcome else None,
                 outcome.completion_tokens if outcome else None,
                 latency_ms, _iso(moment), attempt_id))
        else:
            conn.execute("UPDATE llm_receipts SET status=?, error=?, updated_at=? WHERE id=?",
                         (status, error, _iso(moment), receipt_id))
            conn.execute(
                "UPDATE llm_receipt_attempts SET status=?, error=?, latency_ms=?, finished_at=?"
                " WHERE id=?", (status, error, latency_ms, _iso(moment), attempt_id))
    finally:
        conn.close()


def paid(*, purpose: str, identity: object, call, model: str | None = None,
         subject: str | None = None, attempt_tag: str | None = None,
         request_summary: dict | None = None, service: str = SERVICE,
         classify=None) -> Result:
    """跑一次付费请求; 同一个逻辑键最多付一次钱。

    `call` 返回 `Outcome`。`classify` 说这次异常算 `failed` 还是 `unknown` —— 分界是
    「对面明确没收下」与「不知道收没收到」，见 AIHOT 的 `ProviderRejectedError`。
    """
    key = logical_key(service=service, purpose=purpose, model=model, identity=identity,
                      attempt_tag=attempt_tag)
    now = _stamp()
    claim = _claim(key=key, service=service, purpose=purpose, model=model, subject=subject,
                   request_json=stable_json(request_summary or {}), now=now)
    if claim.reuse is not None:
        return Result(claim.reuse, True, claim.receipt_id)

    started = now
    try:
        outcome = call()
    except BaseException as exc:  # 取消也要落定: 一行 pending 留在库里就是下一次的假在飞
        status = classify(exc) if classify is not None else "unknown"
        _settle(claim.receipt_id, claim.attempt_id, status, error=repr(exc)[:2000],
                latency_ms=int((_stamp() - started).total_seconds() * 1000))
        raise
    _settle(claim.receipt_id, claim.attempt_id, "received", outcome=outcome,
            latency_ms=int((_stamp() - started).total_seconds() * 1000))
    return Result(outcome.text, False, claim.receipt_id)


def complete(receipt_id: int) -> None:
    """业务写完了。写不完也留着 `received` —— 下次重放捡的是同一份答案。"""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE llm_receipts SET status='completed',"
            " completed_at=coalesce(completed_at, ?), updated_at=? WHERE id=?",
            (_iso(_stamp()), _iso(_stamp()), receipt_id))
    finally:
        conn.close()


def reject_received(receipt_id: int, reason: str) -> None:
    """答案收到了但用不了 (解析不了、空消息): 记成 `failed`, 下次可以再付一次钱。"""
    conn = db.connect()
    try:
        conn.execute("UPDATE llm_receipts SET status='failed', error=?, updated_at=?"
                     " WHERE id=? AND status IN ('received', 'completed')",
                     (reason[:2000], _iso(_stamp()), receipt_id))
    finally:
        conn.close()


def mark_stale_pending(now: datetime | None = None) -> int:
    """把崩溃留下的 pending 行标成 unknown (AIHOT 的 `markStalePendingReceipts`)。"""
    moment = _stamp(now)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM llm_receipts WHERE status='pending' AND updated_at<?",
            (_iso(moment - timedelta(seconds=STALE_PENDING_SECONDS)),)).fetchall()
        for row in rows:
            _mark_unknown(conn, row["id"], "placeholder went stale without a recorded result",
                          moment)
        conn.execute("COMMIT")
        return len(rows)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def auto_release_unknown(now: datetime | None = None, limit: int = 200) -> int:
    """结果未知超过半小时的收据放行一次 (AIHOT 的 `autoReleaseUnknownReceipts`)。

    放行 = 记成 `failed`, 于是下一次请求可以再付一次钱。`AUTO_RELEASE_NOTE` 写在 attempt 上
    当「已经自动放行过」的记号, 所以同一张收据不会被自动放行第二次。
    """
    moment = _stamp(now)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM llm_receipts r WHERE r.status='unknown' AND r.updated_at<?"
            " AND NOT EXISTS (SELECT 1 FROM llm_receipt_attempts a"
            "                 WHERE a.receipt_id=r.id AND a.error LIKE ?)"
            " ORDER BY r.id LIMIT ?",
            (_iso(moment - timedelta(seconds=AUTO_RELEASE_SECONDS)),
             AUTO_RELEASE_NOTE + "%", limit)).fetchall()
        for row in rows:
            conn.execute("UPDATE llm_receipts SET status='failed', error=?, updated_at=?"
                         " WHERE id=? AND status='unknown'",
                         (AUTO_RELEASE_NOTE, _iso(moment), row["id"]))
            conn.execute(
                "UPDATE llm_receipt_attempts SET status='failed', error=?, finished_at=?"
                " WHERE receipt_id=? AND status='unknown'",
                (AUTO_RELEASE_NOTE, _iso(moment), row["id"]))
        return len(rows)
    finally:
        conn.close()


def recover(now: datetime | None = None) -> dict:
    """一轮开始前收拾上一次留下的在途请求 (AIHOT 的 `recoverStaleWork`)。"""
    moment = _stamp(now)
    stale = mark_stale_pending(moment)
    released = auto_release_unknown(moment)
    if stale or released:
        log.info("llm: 收据清理 stale=%d released=%d", stale, released)
    return {"stale": stale, "released": released}
