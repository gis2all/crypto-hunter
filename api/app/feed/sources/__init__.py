"""信息流的源：一份清单、四种抓法、一层快照。

读的一侧是 `snapshot` —— 四个实时视图与三种刊读的都是它，而不是源本身。
写的一侧是 `collect.poll` —— 把到点的源抓一轮，落进 `feed_items`；谁去叫它由 `tick` 说
（进程内一个 asyncio 任务，每分钟一班）。
清单是 `config/feed/sources.yaml`，由 `store.import_roster()` 导进 `feed_sources` 表。

`registry` 是「谁在说」：名字、分级、一手标记，也从那张表读。
`mock` 还在，但它是开发与测试的夹具（`mock.py` ＋ `mock/records.json`），不是清单里的一员 ——
一份夹具要能写出「今天的真形状」（同一件事多家在说、有首发有转载、时间有差），而一个真源的
形状由那个源自己决定。
"""

from __future__ import annotations

from . import mock, registry, tick
from .collect import poll
from .snapshot import read_between, read_window
from .store import import_roster

__all__ = ["mock", "registry", "poll", "read_between", "read_window", "import_roster", "tick"]
