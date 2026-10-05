"""采集的时钟: 一个 asyncio 任务, 每分钟问一次「这一槽哪个活儿到点了」。

调度不是队列, 也不需要队列。活儿各自记着"上一次跑在哪个槽" (`feed_jobs.last_slot`), 而每个源
记着"下一次该看它的时刻" (`feed_sources.next_fetch_at`) —— 进程重启之后什么都不用重建, 表接得上。
这一层只做三件事: 起进程时把出厂清单与活儿登记导进表 (那是文件与表之间唯一的门), 然后每分钟
叫一次 `jobs.run_due`。

`poll` 是同步的, 而且里面是网络。直接在事件循环里跑会把整台 API 卡住几十秒, 所以抓取扔进
线程 (`asyncio.to_thread`), 事件循环只负责记时间。

一次 tick 坏掉只记日志, 不退出: 一个源坏了是那一行的事 (`poll` 自己记账), 而一个意外不该让
采集永远停在那里。
"""

from __future__ import annotations

import asyncio
import logging

from .. import jobs
from .store import import_roster

log = logging.getLogger(__name__)

#: 每分钟看一次。DESIGN.md 8.6 那张频率表里最短的一档是 15 分钟, 这个粒度够密。
TICK_SECONDS = 60

_task: asyncio.Task | None = None


async def _once() -> None:
    """一遍: 跑到点的活儿, 记一条日志。坏了只记日志 —— 下一分钟照来。"""
    try:
        outcome = await asyncio.to_thread(jobs.run_due)
    except asyncio.CancelledError:
        raise
    except Exception:  # 一次 tick 坏掉不该让采集停在那里
        log.exception("feed tick failed")
        return
    # 坏掉的活儿每一条都要看得见: 一个每天跑一次的活儿坏了, 不写下来就没人知道。
    for name, message in (outcome.get("failed") or {}).items():
        log.warning("feed job %s failed: %s", name, message)
    report = (outcome.get("ran") or {}).get("sources.poll") or {}
    writing = report.get("writing") or {}
    grouping = report.get("grouping") or {}
    if (report.get("ok") or report.get("failed") or report.get("filled")
            or writing.get("wrote") or grouping.get("story") or grouping.get("development")):
        log.info("feed: %d ok, %d failed, %d new, %d filled; writing asked %d, wrote %d "
                 "(%d full), failed %d; grouping asked %d, %d stories, %d developments, "
                 "failed %d", report.get("ok", 0), report.get("failed", 0), report.get("new", 0),
                 report.get("filled", 0), writing.get("asked", 0), writing.get("wrote", 0),
                 writing.get("full", 0), writing.get("failed", 0), grouping.get("asked", 0),
                 grouping.get("story", 0), grouping.get("development", 0),
                 grouping.get("failed", 0))


async def _loop() -> None:
    while True:
        await _once()
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """起进程时挂上它。幂等: 第二次调用不会留下两个时钟。

    判的是「这个事件循环里有没有一个还活着的时钟」。一个属于已经关掉的循环的任务永远不会再跑,
    而 `done()` 是 False —— 只看它, 重启之后的采集会以为自己已经在跑了。
    """
    global _task
    if (_task is not None and not _task.done()
            and _task.get_loop() is asyncio.get_running_loop()):
        return
    import_roster()
    jobs.register()
    _task = asyncio.create_task(_loop(), name="feed-tick")


async def stop() -> None:
    """收摊。等它真的停下, 免得进程退出时留下一个「还在跑」的任务。"""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
