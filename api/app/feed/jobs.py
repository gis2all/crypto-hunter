"""定时活儿: 一个名字、一个周期, 以及"上一次跑在哪个槽"。

这里不是队列, 也不打算长成队列 —— 一台机器、一个进程, 要回答的只有一个问题: **这一槽该不该跑**。
判据是 `feed_jobs.last_slot` 与"现在落在哪个槽":

  * 槽按周期对齐、向下取整。用它而不是"上次跑完的时刻"当依据: 一次跑久了不会让后面每一槽
    都跟着往后漂。
  * 进程停了三个槽再起来, `once` 的活儿补跑**一次**, 不是三次 —— 一天一次的活儿积了三天的账,
    补三次没有意义; 而它总得跑过一次, 否则"没跑"和"还没到"从表上分不出来 (这就是错槽补跑)。
  * `skip` 的活儿错过就不补: 把错过的那几槽折成当前这一槽, 下一槽到点再说。

活儿都在线程里跑 (`asyncio.to_thread`) —— 它们不是网络就是数据库, 直接在事件循环里跑会把整台
API 卡住几十秒。一个活儿坏掉只记一条账, 不让这一轮停下来: 那是它自己的事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..store import db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    """一个定时活儿。`run` 收到"现在", 回一份可以落账的细节 (没有就回空)。"""

    name: str
    cadence_minutes: int
    run: Callable[[datetime], dict | None]
    #: 错过了几个槽之后: once = 补一次, skip = 不补, 等下一槽。
    missed: str = "once"


#: 流水的保留期。够查一次"昨天那趟为什么没跑", 又不至于让它长成一个只进不出的表。
RUN_KEEP_DAYS = 30
#: 审计的保留期长一些: 它是"谁改了什么"的账, 半年之后仍然可能有人要查。
AUDIT_KEEP_DAYS = 180


def slot_of(moment: datetime, cadence_minutes: int) -> str:
    """这一时刻落在哪个槽: 从纪元起按周期对齐, 向下取整。

    对齐的是 UTC 的绝对分钟数, 所以"每小时"的活儿永远在整点算到点, 与这台机器在哪个时区
    无关 —— 时区是给人看的 (lib/calendar.py), 不是给调度用的。
    """
    step = max(1, int(cadence_minutes))
    minutes = int(moment.timestamp()) // 60
    floored = (minutes // step) * step
    return datetime.fromtimestamp(floored * 60, tz=timezone.utc).isoformat(timespec="seconds")


def _retention(moment: datetime) -> dict:
    """把流水清到保留期以内。三张表各按自己的口径 —— 它们记的不是一类东西。"""
    return {
        "fetch_runs": db.feed_fetch_runs_prune(
            (moment - timedelta(days=RUN_KEEP_DAYS)).isoformat(timespec="seconds")),
        "job_runs": db.feed_job_runs_prune(
            (moment - timedelta(days=RUN_KEEP_DAYS)).isoformat(timespec="seconds")),
        "audit": db.feed_audit_prune(
            (moment - timedelta(days=AUDIT_KEEP_DAYS)).isoformat(timespec="seconds")),
        "selected_ledger": db.feed_selected_ledger_prune(
            (moment - timedelta(days=AUDIT_KEEP_DAYS)).isoformat(timespec="seconds")),
        # 队列只清做完的那一档: 失败的还要重试, 不能跟着旧行一起消掉。
        "fetch_queue": db.feed_queue_prune(
            (moment - timedelta(days=RUN_KEEP_DAYS)).isoformat(timespec="seconds")),
        "deliveries": db.feed_deliveries_prune(
            (moment - timedelta(days=AUDIT_KEEP_DAYS)).isoformat(timespec="seconds")),
    }


def _clock(text: object) -> tuple[int, int] | None:
    """`08:00` -> (8, 0)。认不出回 None —— 一个手写的时刻不该让整台定时器停下来。"""
    try:
        hour, _, minute = str(text).partition(":")
        found = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    if not (0 <= found[0] <= 23 and 0 <= found[1] <= 59):
        return None
    return found


def _publish_due(moment: datetime) -> dict | None:
    """到点的刊各出一期。出的永远是**刚过去的那一期** (理由在 `pipeline.DEFAULTS` 的 schedule)。

    十分钟一趟, 所以"到点"的粒度是十分钟 —— 真正的时刻是 `schedule.<kind>`, 按北京时间。
    判两件事: 本期到点没有, 以及那一期在不在 `feed_editions` 里。**出过就不再动**: 一期是出刊
    那一刻的定稿 (4.10.8), 重出会把它换成"现在这一秒的行情", 纸和详情之间那个刻意的差就没了。
    """
    from . import service
    from .sources import pipeline

    out: dict = {}
    for kind in service.KINDS:
        given = str(pipeline.schedule().get(kind) or "").strip()
        if not given:
            continue                        # 留空 = 这一期不出
        clock = _clock(given)
        if clock is None:
            log.warning("feed: schedule.%s 认不出 (%r), 这一期不出了", kind, given)
            continue
        # 本期从哪里开始 —— 本期开始的那一天/一周/一月, 就是这个时刻该出刊的日子。
        _now_key, start, _end = service.period(kind, moment)
        due = start.astimezone(service.ZONE).replace(
            hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if moment.astimezone(service.ZONE) < due:
            continue
        # 上一期 = 本期开始的前一秒。不用"当前时刻减一天": 进程停了一整天再起来, 减一天还落在
        # 本期里, 会印出一张刚开头半天的纸; 减一秒永远落在刚过去那一期里。
        previous = start - timedelta(seconds=1)
        key, _s, _e = service.period(kind, previous)
        if db.feed_edition(service.edition_id(kind, key)) is not None:
            continue
        service.run_once(now=previous, kind=kind)
        out[kind] = key
    return out or None


def registry() -> list[Job]:
    """这一台机器上所有的活儿。

    在函数里 import 是为了断掉环: `sources/collect.py` 往下游走, 而下游 (写作、分组)
    迟早要问"现在几点"或读一次库, 顶层 import 会把它们串成一个圈。
    """
    from . import delivery, ledger, snapshot_heat, writing
    from .sources import collect, icons

    return [
        # 抓一轮。周期是 1 分钟, 而哪一家到点是源自己的事 (`feed_sources.next_fetch_at`) ——
        # 这一层只负责"每分钟问一次", 不替谁做排班。
        Job("sources.poll", 1, lambda now: collect.poll(now)),
        # 找图标: 一天一次。放过期比放过勤重要 —— 一个没有图标的站点很少长出一枚来。
        Job("sources.icons", 24 * 60, lambda now: icons.refresh(now)),
        # 补正文的队列 (1.2)。`sources.poll` 已经会在每一轮里推它一把, 这一趟负责的是**回收**那一半:
        # 上一次崩在半路、租约已经过期的那几条, 在这里被认领回来。没有过期租约时它什么都不做。
        Job("sources.details", 1, lambda now: collect.drain(now)),
        # 整点给热度存一格。曲线是它攒出来的, 少一格就是曲线上一个断口。
        Job("hot.snapshot", 60, lambda now: snapshot_heat.snapshot(now)),
        # 精选台账: 和热度一样整点一趟, 对一次"进出了哪几条"。
        Job("selected-ledger", 60, lambda now: ledger.sync(now)),
        # 清流水。半夜跑一次就够。
        # 发推送 (5.8)。五分钟一趟: 一期刊是一天几次的事, 而退避是记在行上的。
        Job("notify.deliver", 5, lambda now: delivery.run(now)),
        # 引用帖的译文 (3.6)。十分钟一趟: 一条被引的帖子一辈子只译一次, 而它随时会被新的
        # 引用带进来 —— 慢一点只是"这一句还没中文", 快一点是白花一次调用。
        Job("quotes.translate", 10, lambda now: writing.translate_quotes(now)),
        # 出刊 (3.4)。十分钟一趟: 到点是 `schedule.<kind>`, 这一趟只回答"到了没有、
        # 出过没有" —— 真正的编排在 `service.run_once`。
        Job("editions.publish", 10, _publish_due),
        Job("ops.retention", 24 * 60, _retention),
    ]


def register() -> int:
    """把活儿的登记写进表。每次进程起来跑一遍 (同源清单)。"""
    return db.feed_jobs_put([
        {"name": job.name, "cadence_minutes": job.cadence_minutes} for job in registry()])


def due(moment: datetime) -> list[Job]:
    """这一槽该跑的活儿。跳过的那些顺手把它的槽推掉 (否则下一秒还会问它一遍)。"""
    held = {row["name"]: row for row in db.feed_jobs_all()}
    out: list[Job] = []
    for job in registry():
        row = held.get(job.name)
        if row is not None and not row.get("enabled"):
            continue
        slot = slot_of(moment, job.cadence_minutes)
        last = (row or {}).get("last_slot") or ""
        if not last:
            out.append(job)          # 一次都没跑过: 第一次必须发生
            continue
        if last >= slot:
            continue                 # 这一槽已经有着落了
        if job.missed == "skip":
            previous = slot_of(moment - timedelta(minutes=job.cadence_minutes),
                               job.cadence_minutes)
            if last != previous:
                # 错过了一槽以上: 不补。把**这一槽**记成跳过 —— 错过的那几槽折成它, 下一槽到点
                # 再跑。记成上一槽不行: 下一次问的时候还是"晚了一槽以上", 于是它会一直跳过,
                # 永远等不到自己那一槽。
                db.feed_job_mark(job.name, slot, "skipped")
                continue
        out.append(job)
    return out


def run_due(moment: datetime | None = None) -> dict:
    """跑到点的活儿。回一份谁跑了、谁坏了的账。"""
    now = moment or datetime.now(timezone.utc)
    ran: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for job in due(now):
        slot = slot_of(now, job.cadence_minutes)
        run_id = db.feed_job_run_start(job.name, slot)
        try:
            detail = job.run(now)
        except Exception as exc:  # 一个活儿坏掉是它自己的事
            message = repr(exc)[:400]
            db.feed_job_run_end(run_id, "failed", {}, message)
            db.feed_job_mark(job.name, slot, "failed", message)
            log.exception("feed job %s failed", job.name)
            failed[job.name] = message
            continue
        payload = detail if isinstance(detail, dict) else {}
        db.feed_job_run_end(run_id, "ok", payload)
        db.feed_job_mark(job.name, slot, "ok")
        ran[job.name] = payload
    return {"ran": ran, "failed": failed}