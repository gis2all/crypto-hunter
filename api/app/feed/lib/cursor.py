"""不透明游标（AIHOT 的 `lib/cursor.ts`）。

分页不能用 `OFFSET`：这一秒里新来一条，后面的每一页都会错位一次。游标把“上一页最后
那一条”带下去，读的是它**之后**的。对外只露一串不透明字符：页面不该猜内部
排序键，也不该把它当契约用（改了排序就得能换掉旧游标）。

游标里带一个 `v`：旧版本的游标不能被新代码当成合法输入。读不懂就报错（而不是退回第一页）——
退回第一页会让“翻到底”变成一个死循环。
"""

from __future__ import annotations

import json

from .ids import b64url_decode, b64url_encode

#: 游标格式的版本。改了字段含义就加一。
CURSOR_VERSION = 1


class InvalidCursor(ValueError):
    """这串游标不是我们写的。调用方把它翻成 400。"""


def encode(values: dict) -> str:
    body = {"v": CURSOR_VERSION, **values}
    return b64url_encode(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def decode(raw: str) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(b64url_decode(raw))
    except Exception as exc:  # 任何一种坏法都是同一件事: 这不是游标
        raise InvalidCursor("cursor is not readable") from exc
    if not isinstance(body, dict) or body.get("v") != CURSOR_VERSION:
        raise InvalidCursor("cursor is from another version")
    return {k: v for k, v in body.items() if k != "v"}
