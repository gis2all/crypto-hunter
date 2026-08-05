"""The two gates, and the reason a row is not on the list.

An activity is promoted when it has **steps** and **a way in**; a project when it has **a
sentence** and a way in. The two gates differ because the two kinds do: a protocol that just
appeared has nothing to participate in yet, and judging it by the activity gate would drop the
whole class - which is the mistake this module was redesigned around (see the module README).

Not being promoted is not being rejected. The row stays, with the reason on it, and the page
prints the pool: "why is this not on the list" is a question a reader asks, and a pipeline that
only ever reports its successes cannot answer it.
"""

from __future__ import annotations

#: The reasons, in the words the page prints. One per way a gate can fail, plus the one the
#: lifecycle sweep writes: an entry that left the list because its own material said it ended.
REASONS = {
    "no_steps": "没有抽到参与步骤",
    "no_summary": "没有抽到一句「是什么」",
    "no_official_link": "没有官方入口（官网或官方账号）",
    "not_classified": "材料没被判成活动或项目",
    "unread": "材料没读到",
    "vague": "材料没说清（这是什么 / 怎么做）",
    "no_official_source": "未找到官方来源（溯源 C 级）",
    "deadline_passed": "材料写的截止时间已过",
}

PROMOTED = "promoted"
CANDIDATE = "candidate"
RETIRED = "retired"


def way_in(entry) -> str:
    """The official way in: a URL, or the account a reader can go and look at."""
    return str(getattr(entry, "official_url", "") or "").strip() or \
        str(getattr(entry, "organizer", "") or "").strip()


#: What a summary has to reach before it counts as "what this is". Six characters, because the
#: threshold has to fit Chinese: 「比特币上的 AMM」 is seven and says exactly what the thing is,
#: while 「s」 and an empty string say nothing.
MIN_SUMMARY = 6


def vague(entry) -> bool:
    """Does the material actually say what this is?

    Only projects are judged here, and only on one thing: a project card whose summary is empty,
    a label, or the title read back is the card the user called 不明所以. Activities keep the
    gate they had (`no_steps`) - telling a concrete step from 「前往置顶帖互动」 is a reading a
    model does better than a length rule, and that ask belongs in the prompt.
    """
    if entry.kind != "project":
        return False
    summary = str(entry.summary or "").strip()
    return len(summary) < MIN_SUMMARY or summary == str(entry.title or "").strip()


def verdict(entry) -> tuple[str, str | None]:
    """(status, reason) for one entry. `reason` is None exactly when it is promoted."""
    if entry.kind == "activity":
        if not entry.steps:
            return CANDIDATE, "no_steps"
        if not way_in(entry):
            return CANDIDATE, "no_official_link"
        if vague(entry):
            return CANDIDATE, "vague"
        return PROMOTED, None
    if not str(entry.summary or "").strip():
        return CANDIDATE, "no_summary"
    if not way_in(entry):
        return CANDIDATE, "no_official_link"
    if vague(entry):
        return CANDIDATE, "vague"
    return PROMOTED, None


def reason_label(reason: str | None) -> str:
    """The reader's words for one reason; an unknown reason prints as itself."""
    if not reason:
        return ""
    return REASONS.get(reason, reason)
