"""收摊的一个名单（AIHOT 的 `lib/shutdown.ts`）。

进程退出时要停的那几件事（采集时钟、后台任务）在这里登记。不用信号的原因：
一个正在写库的线程被死掉，比多等一秒更坏。所以这里只记录“有哪些”，
由 `api/app` 的生命周期在退出前按顺序 await 它们。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

_hooks: list[tuple[str, Callable[[], Awaitable[None]]]] = []


def register(name: str, hook: Callable[[], Awaitable[None]]) -> None:
    """登记一个收摊动作。同名的只留最后一个（幂等）。"""
    for index, (known, _) in enumerate(_hooks):
        if known == name:
            _hooks[index] = (name, hook)
            return
    _hooks.append((name, hook))


def registered() -> list[str]:
    return [name for name, _ in _hooks]


async def run_all() -> None:
    """按登记顺序依次收摊；一个坏了不拖住后面的。"""
    while _hooks:
        name, hook = _hooks.pop(0)
        try:
            await hook()
        except Exception:  # 收摊不能因为一个环节失败而停下
            log.exception("feed shutdown hook failed: %s", name)
