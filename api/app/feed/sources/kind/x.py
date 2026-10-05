"""X 账号抓取器：`search/engines/xsearch.py` 的一层薄壳。

壳薄是有意的。那个引擎管的是「怎么问、怎么被限流、怎么重试」，这里只管三件事：把
`kind` 里的 handle 递给它、把它的 `TweetRow` 翻译成 `Discovery`、把读到哪儿了记进 cursor。

一条推文的 `external_id` 是它自己的 id，不是链接：同一条推文被转发、被引用，id 不变，而链接
会带上一串跟踪参数。归组和去重都靠这个 id。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .... import system_settings
from ....sources import http
from ....search.engines.xsearch import TWEET_LIMIT, XEngine
from ... import htmlbody
from ...models import Discovery

log = logging.getLogger(__name__)

#: twscrape 给回来的时间戳有两种写法，见过这两种。
_FORMATS = ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z")


def _stamp(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return ""


def engine() -> XEngine:
    """这一台机器上的 X 会话。凭证与「系统设置」页测的是同一份。"""
    values = system_settings.effective({})
    return XEngine(token=values.get("x.auth_token"), ct0=values.get("x.ct0"),
                   cli=(values.get("x.cli") or "").strip() or None)


def fetch(source: dict, first_import: bool) -> tuple[list[Discovery], dict]:
    """读一个账号最近的推文。回 (记录, 新的 cursor)。"""
    config = source.get("config") or {}
    handle = (config.get("handle") or "").strip()
    if not handle:
        raise http.SourceError("x_search source without handle: %s" % source.get("id"))
    rows = engine().timeline(handle, limit=TWEET_LIMIT)
    out: list[Discovery] = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        first_line = text.splitlines()[0].strip()
        quoted: dict = {}
        if (row.quoted_text or "").strip():
            quoted = {"text": row.quoted_text.strip(), "handle": row.quoted_handle,
                      "url": ("https://x.com/%s/status/%s" % (row.quoted_handle, row.quoted_id)
                              if row.quoted_handle and row.quoted_id else "")}
        out.append(Discovery(
            source_kind="x",
            source_id=source["id"],
            external_id=str(row.id),
            title=first_line[:180] or str(row.id),
            url="https://x.com/%s/status/%s" % (row.handle or handle, row.id),
            published_at=_stamp(row.created_at),
            body=htmlbody.from_plain(text),
            author=row.handle or handle,
            quoted=quoted,
        ))
    cursor = dict(source.get("cursor") or {})
    if rows:
        newest = max(str(row.id) for row in rows)
        cursor["x"] = {"lastTweetId": newest}
    return out, cursor
