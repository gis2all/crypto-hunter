"""出厂清单与健康账: 源「是什么」从文件来, 源「怎么样了」从这一层的算术来。

这一层不抓取 (那是 `kind/`)、不落原始记录 (那是 `collect.py`)。它管两件事:

1. 把 `config/feed/sources.yaml` 导进 `feed_sources`。清单是人手编辑的, 表是程序写的;
   导入只写前一组列, 后一组 (cursor / fail_count / health / next_fetch_at) 一列不碰。
2. 下一次什么时候看它 —— 成功按自己的间隔, 失败按退避。这两个数算在这里, 是因为它们
   只有一处该算, 而"哪一处"不该是调用方。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from ... import config
from ...store import db
from . import registry

log = logging.getLogger(__name__)

#: 出厂清单。`config/feed/` 这个目录以后还要放门槛与提示词, 它们是同一批东西的载体。
ROSTER_PATH = config.ROOT / "config" / "feed" / "sources.yaml"

#: 抓之前先把下一次推后多久。够一次抓取跑完, 又不至于让一次崩溃把它锁死很久。
LEASE_MINUTES = 10
#: 退避的上限。一个坏了的源每六小时试一次就够了, 再慢就等于放弃它。
BACKOFF_CEILING_MINUTES = 360


def _stamp(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def import_roster(path: Path | None = None) -> int:
    """把出厂清单导进表。回导入的行数。

    每次进程起来都跑一遍是安全的, 也是必要的: 这一处是清单与表之间唯一的门, 而"表里少了
    一行"与"这一行被删了"从表本身看不出来。
    """
    target = Path(path or ROSTER_PATH)
    if not target.exists():
        log.warning("feed: no roster at %s; the feed will have nothing to collect", target)
        return 0
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    rows = document.get("sources") or []
    written = db.feed_sources_put(rows)
    registry.forget()
    log.info("feed: roster has %d sources", written)
    return written


def next_after_ok(source: dict, now: datetime) -> str:
    """成功之后, 下一次按它自己的间隔。"""
    return _stamp(now + timedelta(minutes=int(source.get("interval_minutes") or 60)))


def next_after_fail(source: dict, now: datetime, fail_count: int) -> str:
    """失败之后退避: interval x (fail_count + 2), 封顶六小时。

    乘的是新计数, 所以第一次失败退到 3 个间隔、第二次 4 个 —— 一个偶发的 500 只让它慢一拍,
    而一个真的坏了的源会很快退到上限, 不再每分钟去敲一次。
    """
    interval = int(source.get("interval_minutes") or 60)
    minutes = min(interval * (fail_count + 2), BACKOFF_CEILING_MINUTES)
    return _stamp(now + timedelta(minutes=minutes))


def lease_until(now: datetime) -> str:
    """抓之前先占住的那一格。"""
    return _stamp(now + timedelta(minutes=LEASE_MINUTES))
