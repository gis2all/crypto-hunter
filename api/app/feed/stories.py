"""分组站：把一条条报道归到「事实 / 事件」上，结果落库。

判定那一半在 `grouping.py`（纯函数，拿假模型就能整段测），这一半只管读写库与编排 —— 与 AIHOT
把 `events/relate.ts`（怎么判）和 `events/group.ts`（判完写哪儿）分开放是同一条理由。顺序照抄
AIHOT，换一种会改变结果：

    召回   过去 14 天的事实，取最像的十条              免费
    判定   逐条问模型这次是什么关系                    一次调用
    复核   召回分不到 0.85 却被判「同一次发生」的再问一遍
    定夺   落库：报进那个事实，或者在事件下起新事实，或者自己起一件事

**只在写作站真的配了模型时跑。** 分组要读模型写的 `title_zh` 与 `fact_json`，两样都是写作站的
产物；没有模型就没有骨架，读取侧退回规则分组（`events.group`）—— 那是这套设计里唯一保留的
「旧身份」，不是并行的第二套答案。

**分层不是多余的。** 热点榜数的是「几家在说**这一次发生**」，时间线要的是「这件事先后发生过
什么」，两个问题一张表存不下：把新进展也钉在同一个事实上，同一家会被数两次，热度就成了「发了
几条」而不是「几家在说」。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from ..llm import client as llm
from ..store import db
from . import events as feed_events
from . import embeddings, grouping, materials, writing
from .sources import pipeline
from .models import Event, Item

log = logging.getLogger(__name__)

#: 事实与事件在判定层里的键。带前缀是因为 `Decision.fact` 与 `Decision.story` 是两个 id 空间，
#: 而一张表里的自增 id 会撞车 —— 撞了之后「报进事实 3」会读成「报进事件 3」。
FACT_PREFIX = "f:"
STORY_PREFIX = "s:"

#: 讨论帖回头再挂一次的两个窗口 (AIHOT 的 `REMATCH_HOURS` / `WAIT_HOURS`)。
REMATCH_HOURS = 6
WAIT_HOURS = 48

#: 一趟回头捞最多看多少条帖子。窗口里堆着几千条时不能让它吃掉这一轮的预算 —— AIHOT 那边同样
#: 是「取一批」(它按向量粗筛, 我们按观察时刻倒序截断)。
REATTACH_LIMIT = 200


def _key(prefix: str, value: int) -> str:
    return "%s%d" % (prefix, value)


def _id(raw: str, prefix: str) -> int:
    text = str(raw or "").strip()
    if not text.startswith(prefix):
        return 0
    tail = text[len(prefix):]
    return int(tail) if tail.isdigit() else 0


def _frame(row: dict) -> dict:
    """模型抽的骨架 `{subject, action, object}`。读不动就当没有 —— 一条坏 JSON 不该让整轮倒下。"""
    try:
        frame = json.loads(row.get("fact_json") or "{}")
    except ValueError:
        return {}
    return frame if isinstance(frame, dict) else {}


def _quoted(row: dict) -> dict:
    try:
        quoted = json.loads(row.get("quoted_json") or "{}")
    except ValueError:
        return {}
    return quoted if isinstance(quoted, dict) else {}


def query_of(row: dict) -> grouping.Candidate:
    """一条待判的报道，在判定层里的样子 (AIHOT 的 `reportView`：报道 + 它自己那一格骨架)。"""
    frame = _frame(row)
    return grouping.Candidate(
        key=_key(FACT_PREFIX, 0),
        story="",
        title=(row.get("title_zh") or row.get("title") or "").strip(),
        summary=(row.get("summary_llm") or "").strip(),
        source=(row.get("source_name") or row.get("source_id") or "").strip(),
        at=row.get("published_at") or "",
        first_party=bool(row.get("source_first_party")),
        subject=str(frame.get("subject") or ""),
        action=str(frame.get("action") or ""),
        object=str(frame.get("object") or ""),
        occurred_at=str(frame.get("occurred_at") or ""),
    )


def pool(now: datetime, *, days: int = grouping.RECALL_DAYS,
         version: str = "") -> tuple[list[grouping.Candidate], dict[str, str]]:
    """召回池：过去这些天的事实，外加一张「报道网址 → 事实」的表（用于 boost）。

    代表报道取每个事实**最早**的那一条 —— 判定材料里印的是"这件事最早是怎么说的"，与 AIHOT 的
    `reportView` 一致。网址表是把"这条推回复/转述了那一条"变成材料里的既有事实，不让模型去猜
    (`grouping.recall` 的 `boost`)。

    `version` 是**现在这一版**分组规则: 给了它, 还在等重判的那些成员就不算证据, 也当不上代表
    (AIHOT 的 `recall.trusted`)。不给就是不筛 —— 老的调用方与测试按原样读全量。
    """
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    facts, members = db.feed_fact_pool(since, evidence_version=version)
    first: dict[int, dict] = {}
    urls: dict[str, str] = {}
    covered: dict[int, int] = {}
    for member in members:
        first.setdefault(member["fact_id"], member)
        covered[member["fact_id"]] = covered.get(member["fact_id"], 0) + 1
        url = (member.get("url") or "").strip()
        if url:
            urls.setdefault(url, _key(FACT_PREFIX, member["fact_id"]))
    out: list[grouping.Candidate] = []
    for fact in facts:
        rep = first.get(fact["id"]) or {}
        out.append(grouping.Candidate(
            key=_key(FACT_PREFIX, fact["id"]),
            story=_key(STORY_PREFIX, fact["story_id"]),
            title=(rep.get("title_zh") or rep.get("title") or fact.get("title") or "").strip(),
            summary=(rep.get("summary_llm") or "").strip(),
            source=(rep.get("source") or rep.get("source_id") or "").strip(),
            at=fact.get("at") or "",
            root=bool(fact.get("root")),
            first_party=bool(rep.get("first_party")),
            subject=str(fact.get("subject") or ""),
            action=str(fact.get("action") or ""),
            object=str(fact.get("object") or ""),
            occurred_at=str(fact.get("occurred_at") or ""),
            members=covered.get(fact["id"], 0),
            fact_title=str(fact.get("title") or ""),
        ))
    return out, urls


def boosts(row: dict, candidates: list[grouping.Candidate], urls: dict[str, str]) -> list[grouping.Candidate]:
    """这条报道自己指向的那几条：同一个网址，或者它引用/回复的那条推。"""
    keys = set()
    for url in ((row.get("url") or "").strip(), (_quoted(row).get("url") or "").strip()):
        key = urls.get(url) if url else None
        if key:
            keys.add(key)
    return [candidate for candidate in candidates if candidate.key in keys]


def _group(query: grouping.Candidate, candidates: list[grouping.Candidate],
           boost: list[grouping.Candidate], *,
           timeout: float) -> tuple[list[grouping.Decision], list[tuple[str, str, float]]]:
    """一条报道的「召回 → 判定 → 复核 → 定夺」整趟, 走哪条召回线在这儿定.

    配了 embedding 端点就整批走向量余弦 (召回线 0.6, AIHOT 的 `RECALL_MIN_COSINE`); 没配, 或者这
    一批有一条没拿到, 就整批退回字符二元组 (线 0.25)。**两条线不能混**: 尺度不同, 混出来的结果
    既不是向量也不是字面, 而且完全静默 —— 所以 `embeddings.warm` 整批回 None。

    第二样是"这条报道还被绑到了哪些事上", 交给 `consolidate`。
    """
    table = embeddings.warm([query.text] + [candidate.text for candidate in candidates])
    if table:
        return grouping.group_with_ties(query, candidates, boost=boost, timeout=timeout,
                                        similarity=embeddings.similarity(table),
                                        min_score=grouping.COSINE_MIN)
    return grouping.group_with_ties(query, candidates, boost=boost, timeout=timeout)


def _signal(row: dict, candidates: list[grouping.Candidate],
            pointed: list[grouping.Candidate], *, timeout: float, moment: datetime,
            version: str = "") -> str:
    """一条讨论帖的一趟 (AIHOT 的 `groupSignal`)。返回这一轮做了什么。

    三条路, 由便宜到贵:

        指向    它自己就带着答案 —— 转发/回复了库里那一条, 不必问模型
        直挂    召回分到 0.92 —— 几乎肯定是同一件事, 也不必问
        问一次  剩下的那些才花一次调用

    没配向量端点就只记一笔 (AIHOT 同一条): 这一档的判据是召回分, 而字面相似度在几十字的推文上
    量不出可信的值, 拿它当判据会把帖子挂到不相干的事上 —— 而挂错了比不挂坏得多, 它会把两件事
    连成一件, 读者看到的是"这两件事是同一件"。
    """
    target, kind = None, "signal"
    if pointed:
        target, kind = pointed[0], "signal-native"
    elif candidates:
        query = query_of(row)
        table = embeddings.warm([query.text] + [candidate.text for candidate in candidates])
        recalled = grouping.recall(
            query, candidates, top=grouping.SIGNAL_TOP, min_score=grouping.SIGNAL_MIN_COSINE,
            similarity=embeddings.similarity(table)) if table else []
        if recalled:
            if recalled[0].score >= grouping.SIGNAL_AUTO_COSINE:
                target = recalled[0]
            else:
                verdicts = grouping.judge_signal(query, recalled, timeout=timeout)
                target = grouping.signal_target(recalled, verdicts)
    db.feed_story_signal_set([{
        "source_id": row["source_id"],
        "external_id": row["external_id"],
        "story_id": _id(target.story, STORY_PREFIX) if target else None,
        "kind": kind if target else "signal-unmatched",
        "observed_at": row.get("published_at") or moment.isoformat(timespec="seconds"),
        "prompt_version": version,
    }])
    return "signal" if target else "unmatched"


def _quotes(url: str, external_id: str) -> bool:
    """那条引用的链接指的是不是这一条 (AIHOT `reclaimWaiting` 的 `/status/(\\d+)`)。

    按 id 对而不是按整个网址对: 同一个链接会被写成 `x.com/h/status/9`、`twitter.com/h/status/9`、
    带 `?s=20` 的转发串 —— 同一个 id 才是同一条推, 网址不是。
    """
    if not url or not external_id:
        return False
    return re.search(r"/status/%s(?:[^0-9]|$)" % re.escape(external_id), url) is not None


def _retry(row: dict, *, timeout: float, moment: datetime, version: str) -> str:
    """把一条没挂上的帖子重新走一遍挂靠。

    池子重读: 这一趟刚立起来的那件事必须能当候选, 否则重问一遍还是挂不上。指向那一路
    (`boosts`) 也重算 —— 原帖入库之后, 引用它的那条就从"没候选"变成"自带答案"。
    """
    candidates, urls = pool(moment, version=version)
    return _signal(row, candidates, boosts(row, candidates, urls),
                   timeout=timeout, moment=moment, version=version)


def _reattach(row: dict, *, timeout: float, moment: datetime, version: str) -> dict:
    """判完一条新报道之后回头看两条线 (AIHOT 的 `rematchSignals` + `reclaimWaiting`)。

    讨论帖先到、原帖后到就会永远掉在外面: 判定站只判一次, 而"没挂上"也记了一笔, 从此不再排队。
    这一趟是那个补口, **只在真的立了事件/起了新事实之后跑** —— AIHOT 只在 `new-story` 与
    `new-fact-in-story` 两个出口跑同样的两条线。

        回头捞  6 小时内没挂上、和新写的这条相似度过 0.72 的 (判据是向量, 没配端点就整条不做)
        等原帖  48 小时内引用了这一条的 (按引用链接里的 `/status/<id>` 对)

    两条线都只把帖子**重新问一遍**, 挂不挂得上还是 `_signal` 那一套判据说了算 —— 回头捞不是
    "强行挂靠", 多问一次不多花一分钱: 指向那一路 `boosts` 直接答, 够近的那一路 0.92 直接挂。
    一条帖子重问失败 (模型不可用) 只记一笔, 不让这一轮的判定跟着倒下 (AIHOT 的 `rematchError`)。
    """
    out = {"rematched": 0, "reclaimed": 0}
    query = query_of(row)

    window = db.feed_items_unattached_signals(_since(moment, REMATCH_HOURS), limit=REATTACH_LIMIT)
    if window:
        table = embeddings.warm([query.text] + [query_of(other).text for other in window])
        if table:  # 没配向量端点就没有这一条线 (AIHOT 的 `embeddingsAvailable`)
            sim = embeddings.similarity(table)
            for other in window:
                if sim(query.text, query_of(other).text) < grouping.SIGNAL_MIN_COSINE:
                    continue
                out["rematched"] += 1
                _retry_quietly(other, timeout=timeout, moment=moment, version=version)

    post_id = str(row.get("external_id") or "")
    for other in db.feed_items_unattached_signals(_since(moment, WAIT_HOURS), quoted_id=post_id,
                                                  limit=REATTACH_LIMIT):
        if not _quotes((_quoted(other).get("url") or ""), post_id):
            continue
        out["reclaimed"] += 1
        _retry_quietly(other, timeout=timeout, moment=moment, version=version)
    return out


def _retry_quietly(row: dict, *, timeout: float, moment: datetime, version: str) -> None:
    """重问一条帖子; 模型不可用只记一笔 (AIHOT 的 `rematchError`/`reclaimError` 不抛)。"""
    try:
        _retry(row, timeout=timeout, moment=moment, version=version)
    except llm.LLMUnavailable as exc:
        log.warning("feed: signal reattach skipped: %s", str(exc)[:300])


def _since(moment: datetime, hours: float) -> str:
    """窗口的起点。归一在库里做 (`_utc_stamp`), 这里只写白。"""
    return (moment - timedelta(hours=hours)).isoformat(timespec="seconds")


def apply(row: dict, decision: grouping.Decision, moment: datetime, *,
          version: str = "") -> str:
    """把一次定夺写进库。返回这次做了什么（报告用，也方便抽查）。

    `version` 是这一趟用的分组提示词版本, 跟着决定一起落库 —— 提示词换版之后, 靠它把按旧版定
    过的行挑出来重判 (AIHOT 把 `ANALYZE_PROMPT_VERSION` 记在每条判定上, 同一个目的)。
    """
    frame = _frame(row)
    title = (row.get("title_zh") or row.get("title") or "").strip()
    at = row.get("published_at") or moment.isoformat(timespec="seconds")
    story_id = _id(decision.story, STORY_PREFIX)
    fact_id = _id(decision.fact, FACT_PREFIX)
    item_key = {"source_id": row["source_id"], "external_id": row["external_id"],
                "prompt_version": version}

    if decision.role == "mention":
        # 复合分析 (4.9b): 不立事件、不建事实, 只记下它提到了哪几件事, 并给这一条打上
        # `composite`。它照常出现在全部动态里 (那是一篇真发表过的东西), 但它不是一件事的
        # 发生 —— 所以热点榜与综述都不会拿它当证据。
        rows = []
        for key in decision.mentions:
            mentioned = _id(key, FACT_PREFIX)
            if mentioned:
                rows.append(dict(item_key, fact_id=mentioned, role="mention",
                                 relation=decision.relation,
                                 confidence=decision.confidence))
        db.feed_item_mentions_clear(row["source_id"], row["external_id"])
        db.feed_item_mentions_set(rows)
        db.feed_items_set_scope(row["source_id"], row["external_id"], "composite")
        return "roundup"

    db.feed_item_mentions_clear(row["source_id"], row["external_id"])
    db.feed_items_set_scope(row["source_id"], row["external_id"], "primary")
    if story_id and fact_id:
        # 又一次报道同一个事实。热点榜按事实数「几家」，所以这里不新建任何东西。
        db.feed_item_facts_set([dict(item_key, fact_id=fact_id, role="report",
                                     relation=decision.relation,
                                     confidence=decision.confidence)])
        db.feed_story_touch(story_id, at)
        return "report"

    if story_id:
        # 明确是后续进展：在那件事下面起一个新事实，root = 0（后续只挂根，它自己不收后续）。
        new = db.feed_fact_new({"story_id": story_id, "title": title,
                                "subject": frame.get("subject") or "",
                                "action": frame.get("action") or "",
                                "object": frame.get("object") or "",
                                "at": at, "root": False})
        db.feed_item_facts_set([dict(item_key, fact_id=new, role="primary",
                                     relation=decision.relation,
                                     confidence=decision.confidence)])
        db.feed_story_touch(story_id, at)
        return "development"

    # 自己起一件事，这条报道就是它的根事实。
    new_story = db.feed_story_new({"title": title, "at": at})
    new_fact = db.feed_fact_new({"story_id": new_story, "title": title,
                                 "subject": frame.get("subject") or "",
                                 "action": frame.get("action") or "",
                                 "object": frame.get("object") or "",
                                 "at": at, "root": True})
    db.feed_item_facts_set([dict(item_key, fact_id=new_fact, role="primary",
                                 relation=decision.relation,
                                 confidence=decision.confidence)])
    return "story"


def run(moment: datetime, *, budget: dict) -> dict:
    """一轮分组：把「有骨架、还没归过组」的那些条目问一遍，再把按旧版规则定过的重判一遍。

    一条一次判定，而且每定夺一条就把池子重读一遍 —— 这一步是必须的：这一轮刚起的一件事，下一条
    报道就该能召回它，否则同一批里的两条会各自起一件事，页面上是两条，实际上是一件。
    """
    limit = max(0, int(budget.get("maxItems") or 0))
    report = {"asked": 0, "report": 0, "development": 0, "story": 0, "signal": 0,
              "unmatched": 0, "manual": 0, "failed": 0, "regrouped": 0,
              "rematched": 0, "reclaimed": 0}
    if limit <= 0 or not writing.enabled() or not pipeline.stages(budget)["group"]:
        return report
    rows = db.feed_items_for_grouping(limit)
    # 提示词换版重判 (AIHOT 的 `regroup_pending` + `scripts/regroup-events.ts`): 按旧版规则定过
    # 的那几条要重新定夺一次, 排在新来的后面 —— 新来的先定, 重判是补账。只取召回窗口里的那些,
    # 理由见 `feed_items_regroup`。
    version = grouping.station_version()
    stale = db.feed_items_regroup(version, (moment - timedelta(days=grouping.RECALL_DAYS)).isoformat(timespec="seconds"),
                                  max(0, limit - len(rows)))
    if stale:
        db.feed_grouping_reset([(row["source_id"], row["external_id"]) for row in stale])
        rows = list(rows) + stale
        report["regrouped"] = len(stale)
    if not rows:
        return report
    report["asked"] = len(rows)
    timeout = float(budget.get("timeoutSeconds") or 60)
    for row in rows:
        try:
            candidates, urls = pool(moment, version=version)
            pointed = boosts(row, candidates, urls)
            if (row.get("participation_mode") or "editorial") == "hot_signal":
                outcome = _signal(row, candidates, pointed, timeout=timeout, moment=moment,
                                  version=version)
            else:
                decisions, ties = _group(query_of(row), candidates, pointed, timeout=timeout)
                # 写之前再看一眼人工决定: 判定这一趟要几十秒, 人可能就在这中间点了「放在
                # 这里」(AIHOT 的 manualDecision 在事务里重读)。
                if db.feed_override_get(row["source_id"], row["external_id"]):
                    report["manual"] = report.get("manual", 0) + 1
                    continue
                outcome = apply(row, decisions[0], moment, version=version)
                # 刚立起来的那件事就是「原帖」: 先到、当时没挂上的帖子现在有地方挂了。只在
                # 立事件/起新事实之后跑 —— 报进已有事实那一趟没有新东西可挂 (AIHOT 同一条)。
                if outcome in ("story", "development"):
                    extra = _reattach(row, timeout=timeout, moment=moment, version=version)
                    report["rematched"] += extra["rematched"]
                    report["reclaimed"] += extra["reclaimed"]
                # 绑住的每**事**都记一笔: 一条报道同时绑着两件事时, 那两件事可能是一件
                # (AIHOT 的 `grouping_decisions` 把候选关系一起存下来, `consolidate` 读它)。
                if len(ties) > 1:
                    db.feed_story_ties_set([
                        {"source_id": row["source_id"], "external_id": row["external_id"],
                         "story_id": _id(story, STORY_PREFIX), "relation": relation,
                         "confidence": confidence}
                        for story, relation, confidence in ties
                        if _id(story, STORY_PREFIX)])
        except llm.LLMUnavailable as exc:
            # 模型不可用（没配 / 被限流 / 答案不合格式）就收手：这一轮不分组，条目留在"未归组"
            # 那一档，下一轮照样轮到它们。半截地写下去会把这一批切成两半。
            report["failed"] += 1
            log.warning("feed: grouping stopped: %s", str(exc)[:200])
            break
        report[outcome] = report.get(outcome, 0) + 1
    return report


def membership(since: datetime) -> dict[tuple[str, str], str]:
    """窗口里已经归好组的条目 → 它的故事键。读取侧据此把规则桶重新并成库里那件事。

    报道与讨论帖一起答: 一条挂在某件事上的推文也要进那个桶 —— 它是那件事的一条证据, 掉在外面
    就会自己凑成一个只有一句话的"事件"。
    """
    stamp = since.isoformat(timespec="seconds")
    out: dict[tuple[str, str], str] = {}
    for read in (db.feed_story_members, db.feed_story_signal_members):
        for row in read(stamp):
            out[(row["source_id"], row["external_id"])] = _key(STORY_PREFIX, row["story_id"])
    return out


def digests(since: datetime) -> dict[str, dict]:
    """窗口里已经写好综述的那些事: 故事键 -> 模型写的四样 (标题 / 综述 / 最新进展 / 状态).

    读取侧据此把模型写的盖到规则版上面 (`views.collect`)。只有 `digest` 非空的事才进来 ——
    "没人写过"和"写出来是空的"在页面上必须是同一个样子。
    """
    out: dict[str, dict] = {}
    for row in db.feed_story_digests(since.isoformat(timespec="seconds")):
        out[_key(STORY_PREFIX, row["story_id"])] = {
            "title": (row.get("title") or "").strip(),
            "digest": (row.get("digest") or "").strip(),
            "latest": (row.get("latest") or "").strip(),
            "status": (row.get("status") or "").strip(),
        }
    return out


def rebucket(events: list[Event], stored: dict[tuple[str, str], str]) -> list[Event]:
    """按库里的分组重新分桶；库里没有的条目留在原来那个桶里。

    「留在原来的桶里」是关键的一条：一个窗口里总会有几条还没轮到分组的（预算截断、模型不可用），
    让它们掉出去等于页面忽然少了几条 —— 而它们并没有做错什么。
    """
    if not stored:
        return events
    buckets: dict[str, list[Item]] = {}
    for event in events:
        for item in event.items:
            key = stored.get((item.discovery.source_id, item.discovery.external_id),
                             event.identity_key)
            buckets.setdefault(key, []).append(item)
    out: list[Event] = []
    for key, items in buckets.items():
        items.sort(key=lambda item: materials.moment(item.discovery.published_at))
        out.append(feed_events.build(key, items))
    return out
