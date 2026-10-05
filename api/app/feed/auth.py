"""管理台的口令 (5.12, AIHOT 的 `auth.ts`)。

这台机器只有一个用户, 所以「登录」不是一套账户体系，是一把口令加一枚签过名的 cookie:
口令在 `feed_settings['admin_password']` (或环境 `CH_ADMIN_PASSWORD`), 签名密钥在
`feed_settings['admin_secret']`, 第一次用到时自动生成。

没配口令 = 不设防。本地单机上的默认状态就是「谁在这台机器上谁就是管理员」;
配了口令之后，每一个改数据的入口都要先答对它。这不是一个可以静默降级的
东西：页面会明说现在是不是设防 ——「设了口令」与「以为设了口令」之间的差别，就是这个
模块存在的全部理由。

cookie 里只有「到什么时候为止」与一枚 HMAC，没有口令本身 —— 一枚签过名的
到期时间就是一张通行证，而把口令放进 cookie 等于每发一次请求就把口令在网线上
多走一遍。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from ..store import db

COOKIE = "ch_admin"
#: 一天半的通行证: 够一个上午的值守, 又不至于让一枚泄露的 cookie
#: 永远有用。到期了重登一次就是一个密码框。
TTL_HOURS = 12.0

PASSWORD_KEY = "admin_password"
SECRET_KEY = "admin_secret"
PASSWORD_ENV = "CH_ADMIN_PASSWORD"


def password() -> str:
    """现在的口令。空串 = 没设防 —— 页面把它当一个状态读, 而不是当一个错误。"""
    return (db.feed_setting_get(PASSWORD_KEY) or os.environ.get(PASSWORD_ENV) or "").strip()


def guarded() -> bool:
    return bool(password())


def secret() -> str:
    """签名密钥。第一次用到时生成一枚写回去 —— 它是程序自己的状态,
    不是人填的配置，所以不该让部署的人先想一个字符串。
    """
    stored = db.feed_setting_get(SECRET_KEY)
    if not stored:
        stored = secrets.token_urlsafe(32)
        db.feed_setting_put(SECRET_KEY, stored)
    return stored


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _mac(body: str) -> bytes:
    return hmac.new(secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()


def issue(moment: datetime | None = None) -> str:
    """签一张通行证。里面只有一个到期时刻 —— 多写一个字段就是多一个会与真相不一致的地方。"""
    now = moment or datetime.now(timezone.utc)
    payload = json.dumps({"exp": int((now + timedelta(hours=TTL_HOURS)).timestamp())},
                         separators=(",", ":")).encode("utf-8")
    body = _b64(payload)
    return "%s.%s" % (body, _b64(_mac(body)))


def valid(token: str, moment: datetime | None = None) -> bool:
    """验一张通行证: 签名对不对, 到没到期。两样都要, 而且先验签名再读到期 ——
    一个没签名的到期时刻是读者自己写的。
    """
    if not token or "." not in token:
        return False
    body, _, given = token.rpartition(".")
    try:
        expected = _mac(body)
        actual = _unb64(given)
        payload = json.loads(_unb64(body))
    except Exception:
        return False
    if not hmac.compare_digest(expected, actual):
        return False
    now = moment or datetime.now(timezone.utc)
    return float(payload.get("exp") or 0) > now.timestamp()


def signed_in(request: Request) -> bool:
    """没设口令就是已登录 —— 本地单机的默认状态, 而不是一个绕过去的漏洞。"""
    if not guarded():
        return True
    return valid(request.cookies.get(COOKIE) or "")


def matches(given: str) -> bool:
    return bool(password()) and hmac.compare_digest((given or "").strip(), password())


def require_admin(request: Request) -> None:
    """改数据的入口都挂它。回 401 而不是 403: 它要的不是权限,
    而是一次登录 —— 而 401 是页面唯一会去弹登录框的那一个码。
    """
    if not signed_in(request):
        raise HTTPException(status_code=401, detail="需要先登录管理台")
