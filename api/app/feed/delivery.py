"""飞书推送 (5.8, AIHOT 的 `notify` 那一半)。

一期刊出来得有人知道。出口是飞书群机器人：一个 webhook 地址、一条卡片消息。地址是**机器本地
的配置**，不是清单里的一行 —— 它是一把 key，进 git 就等于公开，所以它进 `feed_settings`
而不进 `config/`。

投递记在 `feed_deliveries` 上，不记在这一层的内存里：发失败要退避重试，而重试是「下一趟定时活儿」
的事 —— 进程重启一次，内存里的待发清单就没了，表上的还在。退避写在行上 (`next_try_at`)，
不是靠重跑整轮。写入端也不必记得「这一期发过没发过」——幂等由 `subject`（期号）与
`feed_delivery_seen` 保证

发出去的地址是**发的那一刻现读**，不是排队那一刻存的：表上记的是 webhook 的指纹
而不是明文 (运维读口会把这张表翻出来)。换了地址，排队中的几条跟着去新地址 ——
那正是人换地址时想要的行为。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from ..store import db
from . import site
from .lib import http_policy
from .sources import pipeline

log = logging.getLogger(__name__)

#: 地址的两个出处: 页面写进 `feed_settings` 的那一个, 与环境里的那一个。
#: 页面优先 —— 它是后改的, 而环境变量往往是部署时写的。
WEBHOOK_KEY = "feishu_webhook"
WEBHOOK_ENV = "CH_FEISHU_WEBHOOK"

#: 一趟最多发几条。飞书对同一个机器人有频率限制, 而这里一天也就出几期刊。
BATCH = 5


def webhook() -> str:
    """现在该往哪发。空串 = 没配, 那就不发也不排队。"""
    return (db.feed_setting_get(WEBHOOK_KEY) or os.environ.get(WEBHOOK_ENV) or "").strip()


def fingerprint(url: str) -> str:
    """webhook 写进台账的样子。明文进表就是把一把 key 写在一个会被翻出来的地方。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def card(row: dict) -> dict:
    """一期的卡片: 导语 + 入选几件 + 报头那几个数。直接从库里那一行长出来，
    不重算 —— 一期是出刊那一刻的定稿 (DESIGN.md 7)。
    """
    payload = json.loads(row.get("payload_json") or "{}")
    entries = payload.get("entries") or []
    lead = (payload.get("lead") or "").strip()
    top = [str(entry.get("title") or "").strip() for entry in entries[:3]]
    top = [title for title in top if title]
    lines = ["**这一期 %d 件。**" % len(entries)]
    if top:
        lines.extend("· " + title for title in top)
    elements: list[dict] = []
    if lead:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": lead}})
    elements.append({"tag": "div", "text": {"tag": "lark_md",
                                            "content": "\n".join(lines)}})
    link = site.absolute("/")
    if link:
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": link}]})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text",
                                 "content": (row.get("headline") or row.get("key") or "")[:80]}},
            "elements": elements,
        },
    }


def queue_issue(row: dict) -> int | None:
    """一期排一条推送。没配地址、已经排过、或者这一期是空的，都回 None。

    空的一期不发：没有人需要被告知「这一天没事」。幂等在表上 (`feed_delivery_seen`)，
    所以同一期跑第二遍不会发两次。
    """
    url = webhook()
    if not url or not row:
        return None
    if not int(row.get("entries") or 0):
        return None
    target = fingerprint(url)
    subject = str(row.get("id") or "")
    if not subject or db.feed_delivery_seen("feishu", target, subject):
        return None
    return db.feed_delivery_add("feishu", target, subject,
                                json.dumps(card(row), ensure_ascii=False))


def send(url: str, body: dict, timeout: float) -> None:
    """发一条。飞书把失败写在 HTTP 200 里的 `code` 上，所以只看状态码是不够的。"""
    http_policy.check(url)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, json=body)
    if response.status_code >= 400:
        raise RuntimeError("飞书回了 %d: %s"
                           % (response.status_code, response.text[:160]))
    try:
        answer = response.json()
    except ValueError:
        return
    code = answer.get("code") if isinstance(answer, dict) else None
    if code not in (None, 0):
        raise RuntimeError("飞书拒了: %s" % str(answer)[:200])


def run(now: datetime | None = None, limit: int = BATCH) -> dict:
    """把到点的几条发出去。没配地址就一条都不认领 —— 认领了就得给它一个结局,
    而这里没有结局可给；地址配上之后它们还在 pending 上，下一趟自然会发。
    """
    moment = now or datetime.now(timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    report = {"claimed": 0, "sent": 0, "failed": 0}
    url = webhook()
    if not url:
        return report
    rows = db.feed_delivery_claim(limit, stamp)
    report["claimed"] = len(rows)
    timeout = float(pipeline.detail().get("timeoutSeconds") or 20)
    for row in rows:
        try:
            send(url, json.loads(row.get("body_json") or "{}"), timeout)
        except Exception as exc:
            # 退避指数上升，封顶一小时: 一个发不出去的广告位不该每五分钟敲一次。
            attempts = int(row.get("attempts") or 0) + 1
            wait = min(2 ** attempts, 60)
            db.feed_delivery_mark(
                row["id"], "failed", str(exc)[:400],
                next_try_at=(moment + timedelta(minutes=wait)).isoformat(timespec="seconds"))
            report["failed"] += 1
            log.warning("feed: feishu delivery %s failed: %s", row["id"], str(exc)[:200])
            continue
        db.feed_delivery_mark(row["id"], "sent")
        report["sent"] += 1
    return report
