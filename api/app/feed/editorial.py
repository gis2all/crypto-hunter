"""The reading pass: what happened, and whether it is a fact or a discussion.

Today it reads without a model, and every answer says so in `provider`. The shape it fills is
the shape a model will fill - a one-line summary, one line on why an observer might care, and
the label - so swapping a real reader in changes this file and nothing above it.

The label is the one thing a reader is not allowed to drop. An event two independent parties
reported is a fact; an event one party reported is that party talking. An observer who cannot
tell a hack from a rumour of one has been handed nothing, and a summary without the label is
exactly how that happens.
"""

from __future__ import annotations

from . import materials
from .models import Event

#: How the two answers read on the page. Here rather than in the frontend for the same reason
#: the kind labels are in `materials`: the page prints what it is handed, and a label that has
#: to be kept in step with a rule is kept next to the rule.
CERTAINTY_LABELS = {"fact": "事实", "discussion": "讨论"}

#: Source kinds that speak for themselves: a venue announcing its own listing and a regulator
#: publishing its own action are the primary record, whatever anybody else says about them.
PRIMARY_KINDS = frozenset({"exchange", "regulator"})

#: One line per kind on why an observer might care. Written for the rule reader; a model reads
#: the event itself, and this table is one of the things it replaces.
WHY = {
    "hack": "资金安全出了问题；同类协议通常会被一起重新定价。",
    "regulator": "监管动作会改一个赛道的合规成本，影响面通常大于当事方。",
    "listing": "上币改的是能买到的资金池，短期流动性和定价都会跟着动。",
    "funding": "谁在给这个方向掏钱，通常比金额本身更能说明风向。",
    "launch": "新主网或新代币把一批资金和开发者带进场，值得先看一眼再判断。",
    "partnership": "合作把两个原本无关的生态接起来，先看它是不是只是一份公告。",
    "": "还归不到具体类型的信号，先当作一条待观察的动态。",
}


def certify(event: Event) -> str:
    """`fact` or `discussion`, from who is saying it rather than from how it reads.

    Two independent parties make a fact, because collusion is more expensive than accuracy. One
    party is a discussion - not because it is wrong, but because nothing here can tell a report
    from a rumour, and a reader deserves to know which of the two they are holding.
    """
    if event.sources >= 2:
        return "fact"
    if any(item.source_kind in PRIMARY_KINDS for item in event.discoveries):
        return "fact"
    return "discussion"


def read_one(event: Event) -> dict:
    """Read one event. The answer carries its provider, so a page can say how it was read."""
    lead = materials.representative(event.discoveries)
    return {
        "provider": "rules",
        "summary": lead.title.strip(),
        "why": WHY.get(event.kind, WHY[""]),
        "certainty": certify(event),
        "certainty_label": CERTAINTY_LABELS[certify(event)],
    }


def read(events: list[Event]) -> list[Event]:
    """Read every event in place and hand the same list back.

    In place because the order is already decided by the time this runs, and a reader that also
    sorted would be a second opinion about importance - held by the one station whose job is to
    read, not to rank.
    """
    for event in events:
        event.judgement = read_one(event)
    return events
