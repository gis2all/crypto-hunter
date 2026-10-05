"""The macro feed's endpoints: read an issue, and run a pass.

There is no `chain_id` and no `config_id` anywhere below, and that is the point: the feed reads
the industry, and the industry is not on one chain. A radar's routes are keyed by both because a
radar's answers only mean something under a chain and a set of settings; nothing here has that
context, so nothing here takes it.

`POST /run` is synchronous. It is honest today - the only source wired is the fixture, so a pass
is milliseconds of pure computation - and it stops being honest the moment a real source or a
model reader lands, at which point this becomes the job that `api/v1/lookup.py` already is.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ...feed import audit, auth, eligibility, media, outlets, service, snapshot_heat, views
from ...feed import eval as feed_eval
from ...store import db
from . import forms
from .schemas import (FeedAdminLoginIn, FeedAdminSessionOut, FeedAdminSettingOut,
                      FeedAdminSettingsIn, FeedAdminSettingsOut,
                      FeedEvalAxisOut, FeedEvalMissOut, FeedEvalOut,
                      FeedEditionOut, FeedEditionRowOut, FeedEligibilityOut, FeedHotOut,
                      FeedItemDetailOut,
                      FeedCoverageOut, FeedItemsOut, FeedLedgerOut, FeedOpsOut, FeedOverrideIn,
                      FeedOverridesOut, FeedOverrideRowOut,
                      FeedStoryDetailOut,
                      FeedTaxonomyOut,
                      FeedTopicItemsOut, FeedTopicsOut, SystemFormOut)

#: 管理台下面那几个改数据的入口挂的一道门 (5.12)。没配口令时它不拦任何人 ——
#: 本地单机的默认状态就是这个, 而「没配口令」在页面上是一个明写的状态。
admin = [Depends(auth.require_admin)]

router = APIRouter(prefix="/feed", tags=["feed"])


def _issue(row: dict) -> dict:
    """A stored issue as the page reads it: the row's scalars, plus the body the row points at.

    Read from the columns rather than from `payload_json`, because a row keeps one fact in one
    place (store/schema.sql): the scalars are columns so the list can draw a row without a body,
    and the body is the payload so the detail route does not re-derive anything.
    """
    payload = json.loads(row["payload_json"])
    neighbors = db.feed_edition_neighbors(row["kind"], row["key"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "key": row["key"],
        "window_from": row["window_from"],
        "window_to": row["window_to"],
        "seen": row["seen"],
        "issue_number": row["issue_number"],
        "headline": row["headline"],
        "metrics": json.loads(row["metrics_json"]),
        "created_at": row["created_at"],
        "entries": payload["entries"],
        "lead": payload.get("lead", ""),
        #: 要点与快讯是出刊那一刻算好、跟着这份快照存下来的 (AIHOT 的 `highlights` /
        #: `flashes`) —— 读的时候现算, 读者看到的就不是那一期刊出来的东西了.
        "highlights": payload.get("highlights", []),
        "flashes": payload.get("flashes", []),
        #: 相邻的两期**现查**: 新出了一期, 旧那一期的"下一期"就该指过去.
        "prev": neighbors["prev"] or "",
        "next": neighbors["next"] or "",
    }


#: 三个视图读的窗口长短不同, 而它们的默认值都在 `feed/views.py` 里 —— 页面不传就是那边的默认.
_VIEW_SCOPES = ("all", "curated")

#: 搜索的并发闸 (AIHOT 的 `pool.ts`): 最多 4 个在算, 8 个排队, 再多的回 503 + Retry-After。
#: 搜索要在全窗口上算一遍再筛, 不拦一下, 十个读者同时按回车就是十遍全窗口。
SEARCH_CONCURRENCY = 4
SEARCH_QUEUE = 8
_gate_lock = threading.Lock()
_gate_state = {"running": 0, "queued": 0}


@contextmanager
def _search_gate():
    while True:
        with _gate_lock:
            if _gate_state["running"] < SEARCH_CONCURRENCY:
                _gate_state["running"] += 1
                break
            if _gate_state["queued"] >= SEARCH_QUEUE:
                raise HTTPException(status_code=503, detail="搜索排队的人太多，请稍后再试。",
                                    headers={"Retry-After": "2"})
            _gate_state["queued"] += 1
        try:
            time.sleep(0.05)
        finally:
            with _gate_lock:
                _gate_state["queued"] -= 1
    try:
        yield
    finally:
        with _gate_lock:
            _gate_state["running"] -= 1


@router.get("/taxonomy", response_model=FeedTaxonomyOut)
def read_taxonomy() -> dict:
    """筛选栏自己的词表.

    页面不认识任何一个类别: 它是画出来的, 不是写死的. 加一个类别只改 `feed/taxonomy.py`,
    前端一个字节都不动 (CLAUDE.md 2.3.3).
    """
    return views.taxonomy_view()


@router.get("/items", response_model=FeedItemsOut)
def read_items(scope: str = "all", category: str = "", channel: str = "all",
               query: str = "", page: int = 1, cursor: str = "",
               page_size: int = 0) -> dict:
    """全部动态 (scope=all) 与精选 (scope=curated), 按天分组.

    两个视图共用一条路由和一份形状, 因为它们问的是同一批条目的两个问题: "窗口里有什么" 与
    "其中哪些够得上精选". 分成两条路由, 两个形状很快就会各自长出对方没有的字段.

    分页走游标 (`next_cursor`): 这一秒新来一条, OFFSET 会让后面的每一页都错位一次。读不懂的
    游标报 422 而不是退回第一页 —— 退回第一页会让"翻到底"变成一个死循环。
    """
    if scope not in _VIEW_SCOPES:
        raise HTTPException(status_code=422, detail="没有这个视图: %s" % scope)
    if channel not in outlets.rules.CHANNELS:
        raise HTTPException(status_code=422, detail="没有这个渠道: %s" % channel)
    try:
        if query:
            with _search_gate():
                return views.items_view(datetime.now(timezone.utc), scope=scope, category=category,
                                        channel=channel, query=query, page=page, cursor=cursor,
                                        page_size=page_size)
        return views.items_view(datetime.now(timezone.utc), scope=scope, category=category,
                                channel=channel, query=query, page=page, cursor=cursor,
                                page_size=page_size)
    except views.cursorlib.InvalidCursor as exc:
        raise HTTPException(status_code=422, detail="游标读不懂：%s" % exc) from exc


@router.get("/hot", response_model=FeedHotOut)
def read_hot(limit: int = 10) -> dict:
    """热点榜: 同一批条目先聚成事件, 再按热度排.

    排序在服务端, 因为热度是 `sources` (有几家独立来源在说) 折扣上时间算出来的, 而"几家"
    这个数页面看不到也不该自己算.

    读的是**写下来的那份榜** (5 分钟内的请求读同一份), 没有或过期才现算一份再落库
    (snapshot_heat.hot_payload / AIHOT 的 `hot.rank` 每 5 分钟一趟): 一次请求算一遍, 卡片上
    的名次和详情页的"榜内第 N 名"迟早会对不上.
    """
    return snapshot_heat.hot_payload(datetime.now(timezone.utc), limit=limit)


@router.get("/topics", response_model=FeedTopicsOut)
def read_topics() -> dict:
    """主题页左栏的目录: 实体名录分组, 每组带一个窗口内的条数.

    它不按类别分, 因为类别是"这条东西是什么" (每条恰好一个, 全部动态的筛选栏已经在用),
    而这里是"这条东西牵了谁" (一条可以牵好几个, 也可以一个都不牵). 两个问题, 两份词表.
    """
    return views.topics_view(datetime.now(timezone.utc))


@router.get("/topics/items", response_model=FeedTopicItemsOut)
def read_topic_items(topic: str = "") -> dict:
    """一条线上的条目, 按天分组 —— 主题页中栏那一列.

    `topic` 走查询串而不是路径段: `BNB Chain` 这样的名字里有空格, 而路径段里出现空格之后,
    每一种客户端都会各自决定怎么转义它一次.
    """
    return views.topic_items_view(datetime.now(timezone.utc), topic=topic)


@router.get("/item", response_model=FeedItemDetailOut)
def read_item(id: str) -> dict:
    """一条的整页.

    按 id 取, 不是把列表里那个对象原样传下来: 详情页要印的东西列表 payload 里没有, 而"传下来"
    会让详情页依赖"用户是从哪个列表点进来的", 三页就长出三个形状.
    """
    now = datetime.now(timezone.utc)
    found = views.find_item(now, id)
    if found is None:
        raise HTTPException(status_code=404, detail="没有这一条：%s" % id)
    item, story = found
    return views.item_detail(item, story, now)


@router.get("/story", response_model=FeedStoryDetailOut)
def read_story(id: str) -> dict:
    """一件事的整页.

    名次在这里补: 它是"这件事在热点榜上排第几", 榜单的次序只有后端算得出来, 页面看不到.
    """
    now = datetime.now(timezone.utc)
    found = views.find_story(now, id)
    if found is None:
        raise HTTPException(status_code=404, detail="没有这件事：%s" % id)
    story, rank = found
    payload = views.story_detail(story, rank, now)
    # 卡片那条 24 小时的线与这张 7 天的曲线都从落库的小时快照读: 图和数据写在同一张表里,
    # 才不会出现"图上在涨、上面的数在跌" (snapshot_heat).
    line = snapshot_heat.sparkline([story.identity_key], now).get(story.identity_key)
    if line and sum(1 for point in line if point is not None) >= 2:
        payload["series"] = line
    payload["heat"] = snapshot_heat.series(story.identity_key, now)
    return payload


@router.get("/sources")
def read_sources(limit: int = 200) -> dict:
    """源体检 (1.9): 每个源的名字、间隔、连续失败与最近一次抓取。

    一个连续失败的源在页面上必须看得见 —— 只有 `fail_count` 一个数在库里,
    没有读口的时候,"这个源三天没吐东西"就只能靠人去查表。
    """
    runs: dict[str, dict] = {}
    for run in db.feed_fetch_runs(limit=limit):
        runs.setdefault(run["source_id"], run)
    rows = []
    for row in db.feed_sources_all():
        last = runs.get(row["id"]) or {}
        rows.append({
            "id": row["id"], "name": row["name"], "kind": row["kind"],
            "enabled": bool(row.get("enabled")),
            "visibility": row.get("visibility") or "public",
            "interval_minutes": row.get("interval_minutes"),
            "fail_count": row.get("fail_count") or 0,
            "health": row.get("health") or "ok",
            "last_fetch_at": row.get("last_fetch_at") or "",
            "next_fetch_at": row.get("next_fetch_at") or "",
            "last_error": row.get("last_error") or "",
            "last_run": {"status": last.get("status") or "", "found": last.get("found") or 0,
                         "created": last.get("created") or 0,
                         "started_at": last.get("started_at") or "",
                         "error": last.get("error") or ""} if last else None,
        })
    rows.sort(key=lambda item: (-item["fail_count"], item["id"]))
    return {"sources": rows, "failing": sum(1 for row in rows if row["health"] != "ok")}



@router.get("/eval", response_model=FeedEvalOut)
def read_eval() -> dict:
    """金标评测 (5.22): 人工标好的那份样本, 与现在的规则对一遍。

    读一下就算, 不写库也不调模型: 它量的是回归 —— 改了规则之后, 那些原本判对的
    还对不对。对不上的一条一条列出来, 因为「不合格」比「合格率 91%」更能告诉人改哪里。
    """
    return feed_eval.score()


@router.get("/eligibility", response_model=FeedEligibilityOut)
def read_eligibility() -> dict:
    """资格的名单 (5.5): 一共有哪几道门槛、各自问什么、为什么是这个口径。

    它是一张**表**而不是一次判定: 判在 `eligibility.py` 一处做, 这里只把那一处读出来。
    """
    return eligibility.explain()


@router.get("/availability")
def read_availability(ids: str = "") -> dict:
    """星标有效性 (5.18): 这些 id 里哪些还在、哪些已经不公开了。

    星标存在浏览器里, 而一条东西可以在被星标之后下架。页面不能自己猜"它还在吧"。
    """
    return {"rows": views.availability(datetime.now(timezone.utc),
                                       [part for part in ids.split(",") if part.strip()])}


@router.get("/v1")
def read_v1(since: str = "", channel: str = "all", limit: int = 60) -> dict:
    """v1 快照与增量 (5.14): `since` 之后的新条目 + 下一次要带的游标。"""
    return outlets.v1_snapshot(datetime.now(timezone.utc), since=since, channel=channel, limit=limit)


@router.get("/agent")
def read_agent(channel: str = "all", limit: int = 60) -> dict:
    """给 agent 读的一份 (5.13): 比 v1 多正文的纯文本, 少版式字段。"""
    return outlets.agent_items(datetime.now(timezone.utc), channel=channel, limit=limit)


@router.get("/item.md", response_class=PlainTextResponse)
def read_item_markdown(id: str) -> str:
    """一条的 Markdown 导出 (5.17): 和页面同一套可见性与许可规则。"""
    now = datetime.now(timezone.utc)
    found = views.find_item(now, id)
    if found is None:
        raise HTTPException(status_code=404, detail="没有这一条：%s" % id)
    item, story = found
    return views.item_markdown(item, story, now)


@router.get("/editions", response_model=list[FeedEditionRowOut])
def list_editions(kind: str = "day", limit: int = 20) -> list[dict]:
    """The issues of one kind that have been put out, newest first. No bodies: a list draws a
    mark, a headline and a time, and nothing else."""
    return db.feed_editions(limit, kind)


@router.get("/editions/latest", response_model=FeedEditionOut | None)
def read_latest_edition(kind: str = "day") -> dict | None:
    """The newest issue, so a first visit has something to draw.

    Declared above `/editions/{edition_id}` on purpose: FastAPI matches routes in the order they
    are written, and the other one would read "latest" as an id and answer 404 - the same trap
    `api/v1/lookup.py` documents for its own two routes.
    """
    row = db.feed_latest_edition(kind)
    return _issue(row) if row else None


@router.get("/editions/{edition_id}", response_model=FeedEditionOut)
def read_edition(edition_id: str) -> dict:
    """One issue, body and all."""
    row = db.feed_edition(edition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这一期：%s" % edition_id)
    return _issue(row)


@router.get("/admin/session", response_model=FeedAdminSessionOut)
def read_admin_session(request: Request) -> dict:
    """现在是设防还是不设防, 以及这一枚 cookie 算不算登录。

    页面拿它决定画不画那个密码框 —— 而不是让每一个请求自己去猜 401 的意思。
    """
    return {"guarded": auth.guarded(), "signed_in": auth.signed_in(request)}


@router.post("/admin/login", response_model=FeedAdminSessionOut)
def admin_login(body: FeedAdminLoginIn, response: Response) -> dict:
    """答对口令换一枚 cookie。答错就是 401 —— 不告诉对方「差一点」还是「差很多」。"""
    if not auth.matches(body.password):
        raise HTTPException(status_code=401, detail="口令不对")
    response.set_cookie(auth.COOKIE, auth.issue(), httponly=True, samesite="lax",
                        max_age=int(auth.TTL_HOURS * 3600), path="/")
    return {"guarded": True, "signed_in": True}


@router.post("/admin/logout", response_model=FeedAdminSessionOut)
def admin_logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"guarded": auth.guarded(), "signed_in": not auth.guarded()}


def _masked(value: str) -> str:
    """已配置的秘密在页面上的样子: 开头几位 + 末尾几位。
    明文永不回浏览器 —— 一个能在页面上读出来的 webhook 就不再是一把 key。
    """
    if len(value) <= 16:
        return "•" * min(len(value), 8)
    return "%s…%s" % (value[:10], value[-4:])


def _setting(key: str, label: str, value: str) -> dict:
    return {"key": key, "label": label, "configured": bool(value),
            "masked": _masked(value) if value else ""}


@router.get("/admin/settings", response_model=FeedAdminSettingsOut,
            dependencies=admin)
def read_admin_settings() -> dict:
    """管理台能改的那几个键 (5.12): 每一个都是这台机器本地的配置,
    不是清单里的一行 (它们是 key, 进 git 就等于公开)。
    """
    return {"settings": [
        _setting("feishu_webhook", "飞书群机器人地址",
                 db.feed_setting_get("feishu_webhook")),
        _setting("admin_password", "管理台口令",
                 db.feed_setting_get("admin_password")),
    ]}


@router.put("/admin/settings", response_model=FeedAdminSettingsOut,
            dependencies=admin)
def write_admin_settings(body: FeedAdminSettingsIn) -> dict:
    """改管理台的几个键。空串 = 清掉这一个键 (而不是写一个空串进去)。
    每一次写都落一条审计 —— 改口令这件事本该有人记得。
    """
    allowed = {"feishu_webhook": "飞书群机器人地址",
               "admin_password": "管理台口令"}
    for key, value in body.values.items():
        if key not in allowed:
            raise HTTPException(status_code=400, detail="不认识这个键：%s" % key)
        if (value or "").strip():
            db.feed_setting_put(key, value.strip())
        else:
            db.feed_setting_delete(key)
        audit.record("feed.admin_setting", subject="feed:setting:%s" % key)
    return read_admin_settings()


@router.post("/admin/image-secret", response_model=FeedAdminSettingsOut,
             dependencies=admin)
def rotate_image_secret() -> dict:
    """换一枚图片代理的签名密钥 (5.9)。换完之前发出去的链接全部失效 ——
    所以它是一个需要登录的动作, 不是一个本地的小技巧。
    """
    db.feed_setting_delete(media.SECRET_KEY)
    media.forget()
    audit.record("feed.rotate_image_secret", subject="feed:setting:img_proxy_secret")
    return read_admin_settings()


@router.post("/run", response_model=FeedEditionOut, status_code=201, dependencies=admin)
def run_one_pass(kind: str = "day") -> dict:
    """Read every source, work out what happened, and put out an issue.

    Answers with the issue that is now on file, which is not always a new one: running the same
    window twice answers with the issue the first run published (store/db.py `feed_edition_put`),
    because a reader who already read this morning is not owed a second draft of it.
    """
    return _issue(service.run_once(kind=kind))


# --- 人工归属与运维 (AIHOT 的 admin 那一半) -------------------------------------------------


@router.get("/overrides", response_model=FeedOverridesOut)
def read_overrides() -> dict:
    """人工归属决定 (4.8b): 哪些条被人钉住了、谁钉的、什么时候。"""
    return {"rows": db.feed_overrides_all()}


@router.post("/override", response_model=FeedOverrideRowOut, dependencies=admin)
def put_override(body: FeedOverrideIn) -> dict:
    """记一条人工决定: 这一条不再自动归组, 只有显式「重新分组」才会再判它。

    人的一票就是结论 —— 不先问「它现在归在哪」; 自动分组在查询那一侧已经排除它。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.feed_override_set({"source_id": body.source_id, "external_id": body.external_id,
                          "reason": body.reason, "actor": body.actor, "created_at": now})
    audit.record("story.override_set", actor=body.actor,
                 subject="feed:item:%s:%s" % (body.source_id, body.external_id),
                 detail={"reason": body.reason})
    return db.feed_override_get(body.source_id, body.external_id) or {}


@router.delete("/override", dependencies=admin)
def drop_override(source_id: str, external_id: str, actor: str = "") -> dict:
    """撤掉一条人工决定: 下一次自动分组再判它一遍。"""
    before = db.feed_override_get(source_id, external_id)
    if before is None:
        raise HTTPException(status_code=404,
                            detail="没有这条人工决定：%s:%s" % (source_id, external_id))
    db.feed_override_clear(source_id, external_id)
    audit.record("story.override_clear", actor=actor,
                 subject="feed:item:%s:%s" % (source_id, external_id))
    return {"ok": True, "row": before}


@router.get("/ops", response_model=FeedOpsOut)
def read_ops(limit: int = 50) -> dict:
    """运维读口 (5.11): 定时活儿什么状态、最近几趟跑成什么样、抓取与审计的尾巴。

    给值守的人和机器看的, 不是页面的一部分: 一条活儿坏掉, 只有这里回答得了
    「它上一次跑在哪个槽、为什么没跑」。
    """
    jobs = []
    for row in db.feed_jobs_all():
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        jobs.append(item)
    return {"jobs": jobs, "job_runs": db.feed_job_runs(limit),
            "fetch_runs": db.feed_fetch_runs(limit), "audit": db.feed_audit_recent(limit),
            "coverage": db.feed_items_coverage(),
            # 补正文队列 (1.2) 与投递台账 (5.8): "欠多少条没补、发出去几条"
            # 只有这里回答得了。
            "queue": db.feed_queue_stats(), "deliveries": db.feed_deliveries_recent(limit)}


@router.get("/ledger", response_model=FeedLedgerOut)
def read_ledger(item_id: str = "", limit: int = 50) -> dict:
    """精选台账 (5.6): 哪一条什么时候上过首页、又什么时候下来了。

    给一个 `item_id` 就只看那一条的历史 —— 它是跟着定时的一趟写的 (jobs 'selected-ledger'),
    而不是请求这一下现算的。
    """
    return {"rows": db.feed_selected_ledger(item_id=item_id, limit=limit)}


#: 信息流设定那一页写的键 (8.9): 出刊节奏和两道预算。都不跟着链走 —— 一个装置一份,
#: 和 `system.py` 同一类门面, 只是它写 `config/feed/pipeline.yaml`。
class FeedSettingsIn(BaseModel):
    """A patch keyed by each row's own path in the file (`llm.maxItems`)."""

    patch: dict = Field(default_factory=dict)


def _settings_form() -> dict:
    """The page, read fresh: 文件现在的样子就是每一行画出来的样子。"""
    return {"path": str(forms.FEED_PATH), "blocks": forms.feed_blocks()}


@router.get("/settings", response_model=SystemFormOut)
def read_settings() -> dict:
    """出刊节奏与两道预算 (8.6 / 3.4)。出刊那三行留空 = 这一期不出, 到点出的是刚过去的那一期。"""
    return _settings_form()


@router.put("/settings", response_model=SystemFormOut)
def write_settings(body: FeedSettingsIn) -> dict:
    """存下改动, 然后把文件现在的样子答回去。

    整份表单回传而不是一个「已保存」: 一次保存可能让某一格看起来和输入的不一样
    (一个时刻被写回 `08:00`, 一格清成 0), 页面要画的是写进去的东西, 不是发出去的东西。
    改这个文件不用重启 —— 每一轮 tick 现读一次 (见 `pipeline.py`)。
    """
    try:
        forms.feed_save(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    for key in sorted(body.patch or {}):
        audit.record("settings.save", actor="feed-settings",
                     subject="feed:%s" % key, detail={"value": body.patch[key]})
    return _settings_form()
