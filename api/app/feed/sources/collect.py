"""抓一轮：挑到点的源、抓、落原始记录、记一本账。

一站一件事。挑谁由 `feed_sources.next_fetch_at` 说（`store.py` 算它），怎么抓由 `kind/` 说，
这里只做编排 —— 而编排里真正重要的只有两条规矩，两条都是为了「不要悄悄丢数据」：

1. **失败的抓取不推进 cursor，也不改这一批记录**：先落库、再记账，顺序错了就会出现「账上
   说读过了、库里没有」。
2. **只落没见过的那几条**：一个列表页每轮都把同一批文章送回来，全收就等于每轮把旧文章重新
   盖上新日期 —— 一篇两周前的文章会一直看起来像刚发的。首见的那一刻，才是这一类源的时刻。

落库之后还有一站：**补正文**（DESIGN.md 8.5 的细节页）。列表页只给链接、RSS 常常只给一句
摘要，而写一条像样的摘要要读原文 —— 这一站按每轮的预算去读，读到多少算多少。预算与「隔多久
再试一次」都在 `config/feed/pipeline.yaml`。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ...sources import http
from ...store import db
from .. import consolidate, digest, htmlbody, materials, stories, writing
from ..models import Discovery
from . import detail, pipeline
from . import store as source_store
from .kind import ConfigRefused, fetcher_for

log = logging.getLogger(__name__)

#: 能去读原文的那几种源。份在 `detail.py` 里 —— 写作层要用同一份名单判"要不要等"，
#: 两处各写一份就是两处会分家的地方。
DETAIL_KINDS = detail.PAGE_KINDS


def _stamped(record: Discovery, moment: datetime) -> Discovery:
    """这一条的时间怎么读：源没说就用我们见到它的时刻，说得离谱也不信。

    两个判断都在 `materials.decide_timeline` 里（AIHOT 的 `decideTimeline`），因为它们共用同一
    条件的两面：源没给时间，和源给了一个三天后的时间，处理是同一件事 —— 用发现时刻代替。

    `Discovery` 是 frozen 的 —— 它出了抓取器就是账，下游按只读用。所以这里换一个实例，
    而不是改它：直接 `record.published_at = ...` 会抛 FrozenInstanceError，而**每一个**
    列表页源（`weblist.py` 的整段说明）都走这条路，于是它们一条也进不来。
    """
    trusted = materials.decide_timeline(record.published_at, moment).published_at
    if trusted == record.published_at:
        return record
    return replace(record, published_at=trusted)


def _media_objects(record: Discovery) -> list:
    """这一条带的图, 统一成 AIHOT 那一格: 对象, 带 url 与宽高.

    源自带的 (X 这类) 只有 url, 宽高是空; 源没给就从正文里抽 (`htmlbody.images`)。
    `media_json` 存对象是给卡片媒体视图与理解档的首图用的, 读到 API 那一层仍是 url 串
    (`htmlbody.media_urls`)。
    """
    if record.media:
        return [{"kind": "image", "url": str(url), "width": None, "height": None}
                for url in record.media if url]
    return htmlbody.images(record.body)


def _json_list(raw) -> list:
    """库里那一列是 JSON 串; 读坏了就当它空 —— 一张图读不出来不该让这一轮停下."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _wants_detail(row: dict, min_chars: int) -> bool:
    """这一条值不值得去读一趟原文。

    两个理由，都是 DESIGN.md 8.5 那两条：正文不够长（列表页干脆是空的、RSS 只给一句摘要），
    或者那个「标题」根本不是标题（「阅读全文」、或者一段一百多字落错地方的正文）。
    """
    return (len(row.get("body") or "") < min_chars
            or not detail.looks_like_title(row.get("title") or ""))


def _fill_details(pending: list[dict], budget: dict, moment: datetime,
                  sources: dict[str, dict]) -> int:
    """补正文：先把欠下的排进持久队列，再把到点的几条读回来。

    为什么是队列而不是一个 for 循环：一轮补正文要走好几分钟，进程在这中间被杀掉，循环手里那批就没了 —— 而
    `feed_items` 上什么都没写，下一轮只看新条目，它们永远等不到。队列把"欠哪几条"写进表、
    租约过期自动回收，所以崩了下一分钟接着跑（1.2）。

    为什么还把预算截断留在这一层：读正文是这一层最贵的一步，一轮最多读 `maxFetches` 篇是这一层的刹车。
    队列负责"不丢"，预算负责"不冲"，两件事分开。

    为什么还接着补以前欠下的：一轮的预算常常用不完（大部分源本来就带正文），而不接着补，前
    面被预算截断的那批条目就永远等不到下一轮 —— 下一轮只看新条目。
    """
    limit = max(0, int(budget.get("maxFetches") or 0))
    if limit <= 0:
        return 0
    min_chars = int(budget.get("minBodyChars") or 200)
    targets: list[dict] = list(pending[:limit])
    if len(targets) < limit:
        hours = float(budget.get("retryHours") or 24)
        before = (moment - timedelta(hours=hours)).isoformat(timespec="seconds")
        targets.extend(db.feed_items_needing_body(min_chars, limit=limit - len(targets),
                                                  before=before))

    queued: list[dict] = []
    seen: set = set()
    for row in targets:
        key = (row["source_id"], row["external_id"])
        if key in seen:
            # 一条记录同时出现在「这一轮新进来的」与「以前欠下的」两份名单里 —— 原文只该读
        # 一次, 预算也只该扣一次。
            continue
        seen.add(key)
        queued.append({"source_id": row["source_id"], "external_id": row["external_id"],
                       "url": row.get("url") or ""})
    if queued:
        db.feed_queue_enqueue(queued)
    return _drain(limit, budget, moment, sources)


def drain(now: datetime | None = None, limit: int | None = None) -> dict:
    """把队列里到点的那几条读回来，并把过期的租约回收。

    它单独存在是为了「队列自己会往前走」：`sources.poll` 一分钟一趟，即使这一趟一个源都不到点，
    它也会把队列里上一轮没读完的接着读完。上一次崩在半路时已经上了租约的那几条，也走这里被回收。
    """
    moment = now or datetime.now(timezone.utc)
    budget = pipeline.detail()
    size = int(budget.get("maxFetches") or 0) if limit is None else int(limit)
    sources = {row["id"]: row for row in db.feed_sources_all()}
    stamp = moment.isoformat(timespec="seconds")
    reclaimed = db.feed_queue_reclaim(stamp)
    return {"filled": _drain(size, budget, moment, sources), "reclaimed": reclaimed,
            "queue": db.feed_queue_stats(stamp)}


def _drain(limit: int, budget: dict, moment: datetime, sources: dict[str, dict]) -> int:
    """认领、读、结账。租约让"崩在半路"变成"下一分钟重来"而不是"这几条丢了"。"""
    if limit <= 0:
        return 0
    stamp = moment.isoformat(timespec="seconds")
    # 租约给得宽：这个循环一次要串好几条，每条最多花 `timeoutSeconds`。它只是抢不到手的保险，
    # 给紧了会把还在读的一条变成重复劳动。
    lease = (moment + timedelta(seconds=float(budget.get("timeoutSeconds") or 20) * 4 + 120))
    rows = db.feed_queue_claim(limit, lease.isoformat(timespec="seconds"), at=stamp)
    filled = 0
    for row in rows:
        item = db.feed_item_get(row["source_id"], row["external_id"]) or {}
        config = (sources.get(row["source_id"]) or {}).get("config") or {}
        patch = {"source_id": row["source_id"], "external_id": row["external_id"]}
        try:
            text = http.get(row.get("url") or item.get("url") or "",
                            http.crawler_headers(config.get("headers")),
                            transport=config.get("transport", "http"),
                            timeout=float(budget.get("timeoutSeconds") or 20),
                            # 一页读不到就让它过去: 这是「尽力而为」那一档, 三次重试花掉的
                            # 时间是整轮预算的, 而一条没补上的正文下一转还能补。
                            tries=2)
            found = detail.parse(text)
        except Exception as exc:
            log.debug("feed: detail %s failed: %s", row.get("url"), str(exc)[:160])
            db.feed_items_set_detail([patch], tried_at=stamp)
            # 失败也记一笔: 坏链接每轮都重新排队, 会把好条目的位置占掉 —— 退避写在
            # 队列行上, 到点之前不再认领它 (AIHOT 的 `next_try_at`)。
            db.feed_queue_finish(row["id"], "failed", str(exc)[:400],
                                 next_try_at=(moment + timedelta(
                                     hours=float(budget.get("retryHours") or 24))
                                 ).isoformat(timespec="seconds"))
            continue
        if len(found["body"]) > len(item.get("body") or ""):
            patch["body"] = found["body"]
        if found["title"] and not detail.looks_like_title(item.get("title") or ""):
            patch["title"] = found["title"]
        stored = item.get("media")
        if stored is None:
            stored = _json_list(item.get("media_json"))
        if found.get("media") and not stored:
            patch["media"] = found["media"]
        if "body" in patch or "title" in patch:
            filled += 1
        # 一条一写, 不攒到轮末: 一轮四十页要走好几分钟, 攒着写的话, 这中间被打断就一条也留
        # 不下, 页面上也看不出它正在补。
        db.feed_items_set_detail([patch], tried_at=stamp)
        db.feed_queue_finish(row["id"], "done")
    return filled

def poll(now: datetime | None = None, limit: int = 40) -> dict:
    """把到点的源抓一轮。回一份这一轮的小结，调用方拿它写日志或画进度。

    `limit` 是这一轮的批大小：四十个源一起冲出去，会把一条线路挤成一次自我限流。
    """
    moment = now or datetime.now(timezone.utc)
    due = db.feed_sources_due(moment.isoformat(timespec="seconds"), limit=limit)
    report: dict = {"due": len(due), "ok": 0, "failed": 0, "new": 0, "filled": 0,
                    "sources": []}
    budget = pipeline.detail()
    pending: list[dict] = []          # 这一轮新进来、还缺正文的

    for source in due:
        sid = source["id"]
        # 占位先占住：同一分钟里的第二个 tick 不会再排到它，而抓崩了也不会把这一格锁死。
        db.feed_source_claim(sid, source_store.lease_until(moment))
        run = db.feed_fetch_run_start(sid)
        try:
            fetch = fetcher_for(source)
            first_import = source.get("last_ok_at") is None
            records, cursor = fetch(source, first_import)

            known = db.feed_items_known(sid, [r.external_id for r in records])
            fresh = [_stamped(r, moment) for r in records if r.external_id not in known]

            if fresh:
                rows = [
                    {
                        "source_id": record.source_id,
                        "external_id": record.external_id,
                        "source_kind": record.source_kind,
                        "identity_key": materials.identity_key(record, ()),
                        "title": record.title,
                        "url": record.url,
                        "author": record.author,
                        "body": record.body,
                        # 源在清单里给的那句话 (纯文字). 与正文分开存:
                        # 一条只有摘要没有正文的记录不是坏记录, 而它的摘要
                        # 正是判定"这条值不值得去读原文"的依据.
                        "excerpt": record.excerpt,
                        # 源没给图就退回正文里抽出来的 (AIHOT: `media` 为空时才用正文那几张);
                        # 卡片媒体视图与理解档的首图都读这一格.
                        "media": _media_objects(record),
                        "published_at": record.published_at,
                    }
                    for record in fresh
                ]
                db.feed_items_put(rows)
                if source["kind"] in DETAIL_KINDS:
                    pending.extend(
                        row for row in rows
                        if _wants_detail(row, int(budget.get("minBodyChars") or 200))
                    )

            db.feed_fetch_run_end(run, "ok", found=len(records), created=len(fresh))
            db.feed_source_ok(sid, json.dumps(cursor, ensure_ascii=False),
                              source_store.next_after_ok(source, moment))
            report["ok"] += 1
            report["new"] += len(fresh)
            report["sources"].append({"id": sid, "status": "ok",
                                      "found": len(records), "created": len(fresh)})
        except Exception as exc:  # 一个源坏了不该拖垮这一轮
            message = "%s: %s" % (type(exc).__name__, exc)
            failed = int(source.get("fail_count") or 0) + 1
            db.feed_fetch_run_end(run, "failed", found=0, created=0, error=message)
            db.feed_source_fail(sid, message, source_store.next_after_fail(source, moment, failed))
            log.warning("feed: %s failed: %s", sid, message[:200])
            report["failed"] += 1
            report["sources"].append({"id": sid, "status": "failed", "error": message[:200]})

    # 一个源都没成也照样补欠下的：补正文不依赖这一轮抓取成不成，只要时钟还在走 —— 而那批
    # 欠账正是「上一轮预算被截断」留下的，等下一次抓取来救它就是永远等不到。
    sources = {row["id"]: row for row in db.feed_sources_all()}
    report["filled"] = _fill_details(pending, budget, moment, sources)

    # 写作站排在补正文之后: 摘要是对正文的摘要, 而正文常常是这几分钟里才补上的 (列表页只
    # 给链接)。顺序反了, 这一轮刚补到的正文要等到下一轮才有人读它 —— 而那时它已经不算新
    # 条目, 也不在第一轮的排序里了。
    names = {sid: (row.get("name") or sid) for sid, row in sources.items()}
    report["writing"] = writing.run(moment, names=names, budget=pipeline.llm())

    # 分组站排在写作站之后: 分组要读写作站刚写下的骨架与中文标题, 顺序反了就是整轮都不分组.
    # 它按条数 (maxItems) 与超时 (timeoutSeconds) 走同一个预算, 模型没配时直接返回空报告 ——
    # 读取侧退回规则分组 (feed/stories.py).
    report["grouping"] = stories.run(moment, budget=pipeline.llm())

    # 合并与相关 (AIHOT 的 consolidate/linkRelatedStories): 判定站刚写下的「一条报道绑住
    # 两件事」的边在这里比两个根, 是同一件就合; 合不了又绑得够多的互为相关。它排在综述站
    # **之前**: 合并动了成员, 综述站这一轮就该按新成员重写, 而不是等下一轮。
    report["consolidate"] = consolidate.run(moment, budget=pipeline.llm())
    report["related"] = consolidate.links(moment)

    # 综述站排在分组站之后: 综述的材料是「这件事下面有哪些报道」, 没有分组就没有这件事.
    report["digests"] = digest.run(moment, budget=pipeline.llm())
    return report
