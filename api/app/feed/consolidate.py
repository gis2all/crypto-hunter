"""合并与相关 (AIHOT 的 `events/consolidate.ts` + `merge.ts`)。

判定站只回答"这条报道和每个候选是什么关系"; 一条报道同时**牢牢绑住**两件事时, 那两件事可能本来
就是一件。这一站拿那批边去比两个**根**: 先问一次, 再反着复核一次, 两次都说是同一件事才合。

合并只搬**事实归属**: 一条报道属于哪个事实不变, 变的是那个事实现在算在哪件事下。被并的那件事
留一行 `merged_into`, 已经贴出去的 `s:<id>` 还能落到新事上 (`db.feed_story_leader`)。

比出来"合不了"、却被至少两条报道绑住的两件事, 互相记成「相关事件」(`links`)。一条边太容易是
模型在一串候选里顺口说了一句, 两条才算数 —— 而挂错的代价是读者以为它们是一件。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..llm import client as llm
from ..store import db
from . import grouping, writing
from .sources import pipeline

log = logging.getLogger("feed.consolidate")

#: 多久以内的边值得再比一遍 (AIHOT 的 `RECALL_DAYS` 窗口)。
TIE_DAYS = grouping.RECALL_DAYS

#: 互为「相关」要至少几条报道绑在一起 (AIHOT 的 `RELATED_MIN_REPORTS`)。
RELATED_MIN_REPORTS = 2


def merge(from_id: int, into_id: int) -> dict | None:
    """把 `from_id` 并进 `into_id`, 并把保留那件的结论作废。

    结论 (标题/综述/最新进展/输入指纹) 是**按成员算出来**的, 成员变了旧的就不算数: 清掉, 下一轮
    综述站按新输入重写。保留那件的标题此时退回代表报道的标题 —— 留着上一版的综述配新的成员,
    是页面上说了一件没有人再算过的事。
    """
    result = db.feed_story_merge(from_id, into_id)
    if result is None:
        return None
    root = db.feed_story_root_report(into_id) or {}
    db.feed_story_invalidate(into_id, root.get("title_zh") or root.get("title") or "")
    return result


def _view(row: dict) -> grouping.Candidate:
    """根事实那一行 -> 判定用的候选 (AIHOT 的 `CandidateView`: 报道 + 它那一格骨架 + 篇数)。"""
    return grouping.Candidate(
        key="f:%d" % row["fact_id"],
        story="s:%d" % row["story_id"],
        title=(row.get("title_zh") or row.get("title") or row.get("fact_title") or "").strip(),
        summary=(row.get("summary_llm") or "").strip(),
        source=(row.get("source") or "").strip(),
        at=(row.get("published_at") or row.get("fact_at") or "").strip(),
        first_party=bool(row.get("first_party")),
        subject=str(row.get("subject") or ""),
        action=str(row.get("action") or ""),
        object=str(row.get("object") or ""),
        occurred_at=str(row.get("occurred_at") or ""),
        members=int(row.get("members") or 0),
        fact_title=str(row.get("fact_title") or ""),
    )


def _groups(rows: list[dict]) -> dict[tuple[str, str], set[int]]:
    """把边按"哪一条报道把它们绑在一起"分组。

    AIHOT 一次 `consolidate(storyIds)` 就是**一条报道**绑住的那些事; 我们落库时按边存, 这里再按
    报道收回去 —— 一条报道绑了 A、B、C, 比的是 A 与 B、A 与 C, 而不是三条互不相干的边。
    """
    out: dict[tuple[str, str], set[int]] = {}
    for row in rows:
        key = (row["source_id"], row["external_id"])
        out.setdefault(key, set()).update((int(row["x"]), int(row["y"])))
    return out


def run(moment: datetime, *, budget: dict) -> dict:
    """比一比被同一条报道绑住的两件事 (AIHOT 判定站末尾的 `consolidate`)。

    `maxItems` 数的是**比几对**, 不是条目: 一对要一次判定, 过了线还要一次复核 —— 与综述站
    "数模型调用"的口径一致。
    """
    limit = max(0, int(budget.get("maxItems") or 0))
    report = {"asked": 0, "merged": 0, "kept": 0, "failed": 0}
    if limit <= 0 or not writing.enabled() or not pipeline.stages(budget)["group"]:
        return report
    timeout = float(budget.get("timeoutSeconds") or 60)
    since = (moment - timedelta(days=TIE_DAYS)).isoformat(timespec="seconds")
    for ids in _groups(db.feed_story_tie_pairs(since, limit=limit * 4)).values():
        if report["asked"] >= limit:
            break
        live = sorted({db.feed_story_leader(story) for story in ids})
        if len(live) < 2:
            continue
        # 盘点起头的事永不合并 (AIHOT 的 `roundup`): 它本来就是"好几件事放在一条里"。
        roots = [row for row in (db.feed_story_root_report(story) for story in live)
                 if row and not row["roundup"]]
        if len(roots) < 2:
            continue
        # 早的当锚点, 晚的并进去 —— 并进最早那件事, 因为那里才是这件事的开始。
        roots.sort(key=lambda row: (row.get("published_at") or "", row["story_id"]))
        anchor, others = roots[0], roots[1:]
        for other in others:
            if report["asked"] >= limit:
                break
            try:
                first = grouping.review(_view(anchor), _view(other), timeout=timeout,
                                        purpose=grouping.PURPOSE_GROUP)
                report["asked"] += 1
                if not grouping.firmly_tied(first):
                    report["kept"] += 1
                    continue
                # 反着再读一遍: 同一对关系两次都成立才合 (AIHOT 的复核档)。
                second = grouping.review(_view(other), _view(anchor), timeout=timeout)
            except llm.LLMUnavailable as exc:
                # 模型不可用就收手: 半截地合下去, 这一批的边就只处理了一半。
                report["failed"] += 1
                log.warning("feed: consolidation stopped: %s", str(exc)[:200])
                return report
            if grouping.firmly_tied(second, min_confidence=grouping.STORY_REVIEW_MIN_CONFIDENCE) \
                    and merge(other["story_id"], anchor["story_id"]):
                report["merged"] += 1
            else:
                report["kept"] += 1
    return report


def links(moment: datetime) -> dict:
    """至少两条报道绑住、又没合起来的两件事, 互相记成「相关事件」。

    AIHOT 的 `linkRelatedStories`: 只加不减 —— 合并掉的那一头在读取时自然落下去
    (`db.feed_story_links` 两头都还在才给)。
    """
    since = (moment - timedelta(days=TIE_DAYS)).isoformat(timespec="seconds")
    pairs: list[tuple[int, int]] = []
    for row in db.feed_story_link_pairs(since, min_reports=RELATED_MIN_REPORTS):
        if _started_by_roundup(row["x"]) or _started_by_roundup(row["y"]):
            continue
        pairs.append((int(row["x"]), int(row["y"])))
    return {"added": db.feed_story_links_add(pairs)}


def _started_by_roundup(story_id: int) -> bool:
    root = db.feed_story_root_report(int(story_id))
    return bool(root and root["roundup"])
