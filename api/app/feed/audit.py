"""审计日志（AIHOT 的 `audit.ts`）。

一个东西被改了，得能回答“谁改的、什么时候、从什么改成什么”。这里只记事实，不做权限 ——
权限在调用方那边（管理台的登录），而一个没有记账的写入比一个没有登录的写入更难追查。

`subject` 是被改的东西的稳定名字（`feed:source:rss:theblock`、`feed:item:rss:x:123`），
而不是一句描述 —— 一句描述没办法当键查。
"""

from __future__ import annotations

from ..store import db

#: 动作名。写在这里而不是各处字符串里，因为页面会把它翻成中文 ——
#: 一个拼错的动作名会在页面上变成一个没有名字的事。
ACTIONS = (
    "source.update", "source.withdraw", "source.restore",
    "item.withdraw", "item.restore", "item.reindex", "item.index_exclude",
    "story.merge", "story.link", "story.override_set", "story.override_clear",
    "model.set", "model.reset",
    "settings.save", "report.regenerate", "round.cancel",
)

#: 动作的中文说法。页面不猜这些名字。
ACTION_LABELS = {
    "source.update": "修改信源",
    "source.withdraw": "下架信源",
    "source.restore": "恢复信源",
    "item.withdraw": "下架条目",
    "item.restore": "恢复条目",
    "item.reindex": "收录条目",
    "item.index_exclude": "排除收录",
    "story.merge": "合并事件",
    "story.link": "关联事件",
    "story.override_set": "人工归属",
    "story.override_clear": "取消人工归属",
    "model.set": "改能力位模型",
    "model.reset": "恢复能力位模型",
    "settings.save": "保存设置",
    "report.regenerate": "重写报刊",
    "round.cancel": "取消这一轮",
}


def record(action: str, *, actor: str = "", subject: str = "", detail: dict | None = None,
           at: str | None = None) -> int:
    """记一笔。动作名不在 `ACTIONS` 里就报错 —— 一个写错的动作名会在页面上
    变成一行没有名字的事，而它发生在写入的那一刻，不是读的时候。
    """
    if action not in ACTIONS:
        raise ValueError("不认识的审计动作: %s" % action)
    return db.feed_audit_add({"action": action, "actor": actor or "",
                              "subject": subject or "", "detail": detail or {}, "at": at})


def recent(limit: int = 50, *, subject: str = "", action: str = "") -> list[dict]:
    """最近的几笔。页面上是一张表，所以它只需要一层读口。"""
    return db.feed_audit_recent(limit, subject=subject, action=action)
