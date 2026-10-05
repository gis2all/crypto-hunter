"""One issue: the window's events, ranked, cut to size, and read.

The cut is by heat and it is a hard cut. An issue that grew to fit whatever the day held would be
a list, and a list is not an issue: the value of a short publication is that something was left
out of it, and a reader who is shown everything has been shown nothing.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from ..llm import client as llm
from . import capability, editorial, writing
from .models import Edition, Event

log = logging.getLogger(__name__)


def headline(events: list[Event]) -> str:
    """一期一个标题: the thing the issue is about, which is the thing it ranked first.

    Not a second judgement about importance - the ranking already made one, and a title that
    disagreed with the front page would be the paper arguing with itself. The model reader
    replaces this with a sentence about the whole issue (DESIGN.md 7).
    """
    return events[0].title if events else ""


#: 一分钟读多少字, 用来估版面长度. 估的是长度, 不是难度 —— 一条只有标题的记录和一整篇分析
#: 印在纸上是几行之差, 不是几倍之差.
CHARS_PER_MINUTE = 300


def metrics(entries: list[Event], seen: int) -> list[dict]:
    """The figures the 报头 prints, in the order it prints them.

    Labels travel with the numbers, and this is not decoration: `sources` means "independent
    parties after dedupe by source_id", which is a fact about a rule in `heat.py`. A page that
    wrote its own word for it would be restating that rule, and the previous version of this
    page did exactly that - it printed a made-up "读了 N 条" and could not print "个来源" at
    all. One station computes, one station prints (CLAUDE.md 2.3.3).

    A zero is left out rather than printed: "0 件一手发布" is true of most quiet windows and
    says nothing a reader can act on, while the issue being empty is already a whole screen.
    """
    parties = {item.discovery.source_id for entry in entries for item in entry.items}
    first_party = sum(1 for entry in entries if any(item.first_party for item in entry.items))
    chars = sum(len(entry.title)
                + len(entry.judgement.get("summary", "") if entry.judgement else "")
                + len(entry.judgement.get("why", "") if entry.judgement else "")
                for entry in entries)
    counted = [
        ("events", "件大事", len(entries)),
        ("sources", "个来源", len(parties)),
        ("first_party", "件一手发布", first_party),
        ("seen", "条动态", seen),
        ("minutes", "分钟读完", max(1, round(chars / CHARS_PER_MINUTE)) if entries else 0),
    ]
    return [{"key": key, "label": label, "value": value}
            for key, label, value in counted if value > 0]


#: 一期一次调用, 写两样: 整期的标题 (报头那一行) 与一段导语 (版面前面那段话).
#: AIHOT 把这一档叫 `report`, 能力位也就叫这个 (feed/capability.py) —— 过去版面是
#: 规则拼的: 标题取第一条, 每一条各自带一句「为什么」. 拼出来的东西像清单, 不像一期刊物.
EDITION_PROMPT = "edition.md"
PURPOSE_EDITION = "feed.edition"


class EditionDraft(BaseModel):
    """`prompts/edition.md` 那个 JSON 对象的形状."""

    headline: str = ""
    lead: str = ""


def edition_material(events: list[Event]) -> str:
    """这一期的材料: 一条一件, 带类型、判词、来源数与读到的那两句.

    给的是**排过序的**名次, 因为导语讲的正是「这一期为什么是这几件」; 名次另算一遍就是第二个
    关于重要性的意见, 而排名已经给过一个了 (headline 同一条理由)。
    """
    lines = ["这一期的条目（按名次）：", ""]
    for index, event in enumerate(events, 1):
        judgement = event.judgement or {}
        lines.append("%d. [%s] %s" % (index, event.kind_label or "动态", event.title))
        lines.append("   判词：%s｜%d 家独立来源"
                     % (judgement.get("certainty_label") or "讨论", event.sources))
        summary = (judgement.get("summary") or "").strip()
        if summary and summary != (event.title or "").strip():
            lines.append("   摘要：%s" % summary)
        lines.append("")
    return "\n".join(lines).strip()


def _ask(events: list[Event], timeout: float) -> EditionDraft:
    messages = [{"role": "system", "content": writing.read_prompt(EDITION_PROMPT)},
                {"role": "user", "content": edition_material(events)}]
    raw = llm.complete_json(messages, purpose=PURPOSE_EDITION,
                            prompt_version=writing.version(EDITION_PROMPT),
                            timeout=timeout, temperature=0.3,
                            model=capability.model_for("report"))
    try:
        return EditionDraft.model_validate(raw)
    except ValidationError as exc:
        raise llm.LLMUnavailable("%s: draft did not fit EditionDraft: %s"
                                 % (EDITION_PROMPT, str(exc)[:200])) from exc


def write_issue(events: list[Event], budget: dict) -> EditionDraft | None:
    """让模型写这一期, 写不出来就回 None —— 调用方接着用规则版.

    没配模型 (`writing.enabled`) 或者模型这一趟不可用, 都不该让一期出不来: 一期没有导语只是
    少一段话, 而一期出不来是这一天的快照整个缺掉 (DESIGN.md 9.7 的底线)。
    """
    if not events or not writing.enabled():
        return None
    try:
        return _ask(events, float(budget.get("timeoutSeconds") or 60))
    except llm.LLMUnavailable as exc:
        log.warning("feed: edition writing stopped, falling back to the rules version: %s",
                    str(exc)[:200])
        return None


#: 报尾那一行快讯放几条. 它是补充, 不是第二份版面.
FLASH_LIMIT = 12


def highlights(entries: list[Event]) -> list[dict]:
    """这一期的要点 (AIHOT 的 `highlights`): 判词有摘要、而且够得上「事实」的那些.

    判据就是 `editorial.certify` 那一条 (两家独立来源, 或者有首发源), 这里不另立一个门槛 ——
    于是"要点"和判词上那枚徽章说的永远是同一件事. 摘要是空的那几条不是没话可说就是纯转载,
    印成要点会撑不起那一行.
    """
    return [
        {
            "identity_key": entry.identity_key,
            "title": entry.title,
            "summary": (entry.judgement or {}).get("summary", ""),
            "sources": entry.sources,
            "kind_label": entry.kind_label,
        }
        for entry in entries
        if (entry.judgement or {}).get("summary") and editorial.certify(entry) == "fact"
    ]


def flashes(entries: list[Event]) -> list[dict]:
    """这一期只够一行的那些 (AIHOT 的 `flashes`): 判词里既没有摘要也没有「为什么」.

    一条都不给, 读者会把这一期读成"这一段只发生了这几件事"; 全部展开, 纸就变成流水账.
    所以它们印在纸尾, 一行一条, 点开才是详情.
    """
    rest = [entry for entry in entries
            if not (entry.judgement or {}).get("summary")
            and not (entry.judgement or {}).get("why")]
    return [
        {"identity_key": entry.identity_key, "title": entry.title,
         "last_seen_at": entry.last_seen_at}
        for entry in rest[:FLASH_LIMIT]
    ]


def compose(*, kind: str, key: str, window_from: str, window_to: str, events: list[Event],
            seen: int, per_edition: int, budget: dict | None = None) -> Edition:
    """Rank the window, take the top, read only those, and put the issue out.

    The events arrive already scored, because ranking needs no reading and reading is the one
    thing here that can cost money. A `per_edition` of zero means no cut, which is what a test
    wants and what a quiet window does not need.
    """
    ranked = sorted(events, key=lambda event: (-event.heat, event.first_seen_at))
    picked = ranked[:per_edition] if per_edition > 0 else ranked
    read = editorial.read(picked)
    title = headline(read)
    lead = ""
    # 导语与报头那一行一起交给模型, 但**顺序不变**: 排名与判词已经定了,
    # 模型只写这两样文字, 写不出来就用规则版 (上面那一行).
    if read:
        written = write_issue(read, budget or {})
        if written is not None:
            title = (written.headline or "").strip()[:60] or title
            lead = (written.lead or "").strip()[:900]
    return Edition(
        kind=kind,
        key=key,
        window_from=window_from,
        window_to=window_to,
        entries=read,
        seen=seen,
        headline=title,
        lead=lead,
        metrics=metrics(read, seen),
    )
