"""Turning a window of discoveries into the things they are about.

One event per identity, and the count that matters is how many independent parties said it, not
how many times it was said. A wire service reprinted by nine sites is one party speaking, and a
page that shows nine is how a story looks hot when it is not.

This is a merge, not a vote. Two sources disagreeing about a number are still one event, and
which number is right is not a question this module can answer - so it does not pretend to: both
discoveries stay on the event, and the reading pass is what has to say which it believes.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from . import materials
from .models import Discovery, Event, Item
from .sources import registry


def group(items: Iterable[Discovery], names: Sequence[str] = ()) -> list[Event]:
    """The window's events, in the order their first discovery was read.

    Order is arrival order and nothing else. Heat is what sorts an issue, and a grouping step
    that also sorted would be a second opinion about importance - taken before anybody read
    anything, which is the exact order this design is trying to avoid.
    """
    buckets: dict[str, list[Discovery]] = {}
    for item in items:
        buckets.setdefault(materials.identity_key(item, names), []).append(item)
    return [_event(key, bucket) for key, bucket in buckets.items()]


def build(key: str, items: Sequence[Item]) -> Event:
    """Finalise one bucket from items that are already built.

    Split out from `_event` because there are two ways into a bucket now: the rule grouper
    (raw discoveries in), and the reading side putting them back where the model decided they
    belong (`feed/stories.rebucket`). Both must land on the same Event - a second constructor
    would be a second opinion about what a story's title, time span and source count are.
    """
    lead = materials.representative([item.discovery for item in items])
    kind = materials.kind_of("%s %s" % (lead.title, lead.body))
    moments = [materials.moment(item.discovery.published_at) for item in items]
    return Event(
        identity_key=key,
        kind=kind,
        kind_label=materials.KIND_LABELS.get(kind, materials.KIND_LABELS[""]),
        title=lead.title.strip(),
        url=lead.url,
        first_seen_at=min(moments).isoformat(timespec="seconds"),
        last_seen_at=max(moments).isoformat(timespec="seconds"),
        items=list(items),
        sources=len({item.discovery.source_id for item in items}),
    )


def _event(key: str, items: Sequence[Discovery]) -> Event:
    """One rule bucket: raw discoveries in, items built here.

    一手是**来源的属性**, 从名录读, 不从文字读 —— "我们很高兴地宣布"不是任何事情的证据
    (`Item.first_party`)。所以它在建 Item 的地方就填上, 而不是等阅读 pass: 出刊那条路
    不跑 `analysis`, 只在这里填, 报头那个「件一手发布」就永远是 0, 而 0 是不印的 ——
    一个永远不出现的数比没有这个数更难发现.
    """
    return build(key, [Item(discovery=item, first_party=registry.of(item.source_id).first_party)
                       for item in items])
