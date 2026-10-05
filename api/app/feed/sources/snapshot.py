"""窗口里的原始记录：从 `feed_items` 读回来。

四个实时视图与三种刊读的都是这一层，不是源。这是「抓一次、大家读同一份」的落点：页面为画
一屏去敲四十个源的网络，是接真实源那天最先会炸的地方，而把读取与抓取分开之后，页面延迟就
是一个 SQLite 查询。

读出来的是 `Discovery`，与抓取器交出去的是同一个形状 —— 于是下游（去重、归组、打标签、评分、
热度）一行都不用改，看不见这一层存在。唯一一件不做的事是把 `identity_key` 一起读回来：
那一列是入库那一刻按当时的词表算的账，现在的词表可能已经学到了新名字，所以它由下游重算。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ...store import db
from ..models import Discovery
from .. import htmlbody, scope
from ..materials import moment


def row_to_discovery(row: dict) -> Discovery:
    try:
        media = json.loads(row.get("media_json") or "[]")
    except ValueError:
        media = []
    try:
        quoted = json.loads(row.get("quoted_json") or "{}")
    except ValueError:
        quoted = {}
    return Discovery(
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        title=row["title"],
        url=row["url"],
        published_at=row["published_at"],
        body=row.get("body") or "",
        author=row.get("author") or "",
        media=htmlbody.media_urls(media if isinstance(media, list) else []),
        quoted=quoted if isinstance(quoted, dict) else {},
        seen_at=(row.get("seen_at") or "").strip(),
    )


def read_between(start: datetime, end: datetime) -> list[Discovery]:
    """这段时间里的记录，最早的在先。

    下界交给 SQL（有索引），上界在 Python 里判 —— 一条发表时间在未来（源自己写错）的记录
    不该进这一期，而 `published_at` 是文本，SQL 上的比较取决于写法里带没带时区。
    """
    rows = db.feed_items_since(start.isoformat(timespec="seconds"))
    out: list[Discovery] = []
    for row in rows:
        discovery = row_to_discovery(row)
        try:
            when = moment(discovery.published_at)
        except ValueError:
            continue
        if start <= when < end and scope.listed(row, end):
            # 发布门与下架在这里判: 这一层是“页面读到什么”的唯一入口,
            # 而补正文/分组/热度那几条路读的是原始表, 不该被这道门挡住
            # (一条刚读到的东西还要先去补正文)。
            out.append(discovery)
    return out


def read_window(now: datetime, hours: float) -> list[Discovery]:
    """最近这些小时。四个实时视图读的是它。"""
    return read_between(now - timedelta(hours=hours), now)


def read_written_between(start: datetime, end: datetime) -> dict[tuple[str, str], dict]:
    """这段时间里**模型写过的字**, 按 (来源, 该来源自己的 id) 取。

    与 `read_between` 同一个窗口、同一个理由: 窗口是"这一期是什么"的定义, 而两份读法各切一刀
    的话, 页面上就会出现一条有中文标题、而它的兄弟报道没有的窗口。行里只留 `llm_stage` 不是
    空的那种 —— 一个字都没写过的行, 页面上印的仍然是规则版。
    """
    out: dict[tuple[str, str], dict] = {}
    for row in db.feed_items_since(start.isoformat(timespec="seconds")):
        if not row.get("llm_stage"):
            continue
        try:
            when = moment(row["published_at"])
        except ValueError:
            continue
        if start <= when < end:
            out[(row["source_id"], row["external_id"])] = row
    return out


def read_written_window(now: datetime, hours: float) -> dict[tuple[str, str], dict]:
    """最近这些小时里模型写过的字。三个实时视图读的是它。"""
    return read_written_between(now - timedelta(hours=hours), now)
