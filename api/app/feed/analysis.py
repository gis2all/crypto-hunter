"""The analysis station: 一条原始记录读成一个 `Item`.

This is the station that decides what an item *is* - its category, its tags, its entities, and
the 0-100 score that 精选 is drawn on. Everything above it works on the answer, and everything
below it (the sources) knows nothing about it.

It reads by rules today, for the same reason `sources/mock.py` carries no analysis: a fixture
that arrived pre-scored would let this station be skipped, and a pipeline that skipped it is one
that cannot survive its first real source. When a model takes over, it fills the same fields -
the rules here are the contract, not the ceiling.

Two things it deliberately does NOT do:

  * 它不判"真假". 一条公告和一篇转述拿到的分不一样, 但那是因为谁在说、说了什么类别, 不是因为
    这个模块认为哪一条更可信. 判断留给阅读 pass.
  * 它不看热度. 分数是"值不值得看", 热度是"多少家在看", 两个问题, 两组数, 不许互相借.
"""

from __future__ import annotations

import re
from datetime import datetime
from statistics import mode

from . import heat, htmlbody, materials, media, rules, taxonomy
from .models import Discovery, Event, Item
from .sources import registry

#: 类别本身的分量. 不是"哪个类别更重要"的价值判断, 是"哪个类别会改变更多人的处境": 一次被盗
#: 会让同类协议一起被重新定价, 一篇工具发布不会. 改这张表就是改精选的取向.
CATEGORY_WEIGHT = {
    "security": 46,
    "policy": 42,
    "exchange": 38,
    "funding": 38,
    "protocol": 36,
    "infra": 30,
    "defi": 30,
    "research": 30,
    "product": 28,
    "industry": 26,
}
DEFAULT_CATEGORY_WEIGHT = 26

#: 谁在说. 官方一手是原始记录, 媒体是转述, 监测账号是推测.
TIER_BONUS = {"T1": 16, "T1_5": 11, "T2": 5}
FIRST_PARTY_BONUS = 9

#: 说到了谁、说到什么程度. 两者都封顶, 免得一条把所有人名点一遍的稿子自动拿满分.
ENTITY_BONUS, ENTITY_CAP = 3, 9
TAG_BONUS, TAG_CAP = 2, 6

#: 新鲜度. 三小时以内满分, 之后线性衰减到 48 小时归零 —— 一条两天前的稿子不该靠"类别很重"
#: 一直挂在精选上.
FRESH_HOURS, STALE_HOURS, FRESH_BONUS = 3.0, 48.0, 12

#: 每个类别一句"为什么这一行的读者要看它". 规则版按类别给, 模型接手后逐条写.
REASONS = {
    "security": "资金安全出了问题; 同类协议通常会被一起重新定价。",
    "policy": "监管动作会改一个赛道的合规成本, 影响面通常大于当事方。",
    "exchange": "上币改的是能买到的资金池, 短期流动性和定价都会跟着动。",
    "funding": "谁在给这个方向掏钱, 通常比金额本身更能说明风向。",
    "protocol": "主网与升级改的是这条链能做什么, 是后面所有应用的地基。",
    "product": "产品发布决定普通用户能不能真的用上, 是叙事的落地部分。",
    "defi": "池子里的钱怎么进怎么出, 直接决定这条链的活跃度。",
    "infra": "基建决定成本上限; 一条桥或一个排序器出问题, 上面所有应用一起受牵连。",
    "research": "数据与观点给的是判断的输入, 不是结论, 但缺了它只能凭感觉。",
    "industry": "公司层面的动作会先于产品变化, 值得先记一笔。",
}
DEFAULT_REASON = REASONS["industry"]


def age_bonus(published_at: str, now: datetime) -> int:
    """新鲜度分, 0 到 `FRESH_BONUS`."""
    hours = heat.age_hours(materials.moment(published_at), now)
    if hours <= FRESH_HOURS:
        return FRESH_BONUS
    if hours >= STALE_HOURS:
        return 0
    span = STALE_HOURS - FRESH_HOURS
    return round(FRESH_BONUS * (STALE_HOURS - hours) / span)


def score(discovery: Discovery, read: dict, now: datetime) -> int:
    """0-100, and the sum of five things a reader could point at if asked "why this number"."""
    source = registry.of(discovery.source_id)
    total = CATEGORY_WEIGHT.get(read["category"], DEFAULT_CATEGORY_WEIGHT)
    total += TIER_BONUS.get(source.tier, TIER_BONUS["T2"])
    if source.first_party:
        total += FIRST_PARTY_BONUS
    total += min(len(read["entities"]) * ENTITY_BONUS, ENTITY_CAP)
    total += min(len(read["tags"]) * TAG_BONUS, TAG_CAP)
    total += age_bonus(discovery.published_at, now)
    return max(0, min(100, total))


def normalised(body: str) -> str:
    """正文的块. 库里存的就是块; 采集层改之前留下的旧行与纯文本源还没有, 空行分段补上.

    少了这一手, 那些行在页面上会变成"没有正文", 而且看不出是为什么; 多了这一手, 采集层改与
    没改之间的数据都画得出来. 只在**一块都没有**时才认为是纯文本 —— 一段真的 `<p>` 正文不该
    被再切一次.
    """
    if body and not htmlbody.blocks(body):
        return htmlbody.from_plain(body)
    return body


def _summary(body: str, title: str) -> str:
    """摘要: 正文的第一段. 模型写过的那一条不印这一份, 它只兜底.

    整篇正文印在列表的一行上, 那是把全文塞进列表, 不是摘要 —— 一页里每一条都占三行, 读者就
    扫不动了. 第一段至少是他一眼能看完的量. 没有正文就退回标题.
    """
    first = htmlbody.first_paragraph(body)
    if first:
        return first
    texts = htmlbody.blocks(body)
    return texts[0] if texts else title

#: 精选的下限. 它是这一页唯一的门槛, 而且它是**分数**不是热度: 精选选的是"值不值得看".
#: 分数在这个文件里算出来, 所以线也在这里: 页面按它分精选, 入库时的写作站按它决定要不要译正文.
#: 写两个数迟早有一个先改, 而那条线一旦分家, 页面上"精选"与"译了正文"就是两批条目.
CURATED_MIN_SCORE = 65

def article(discovery: Discovery) -> dict:
    """这条记录能整页读的那一份: 正文的块, 和兜底的摘要.

    正文是**源自己的块** (`<p>` 与源写的 `<h2>`/`<h3>`), 小标题不是我们按位置编的 —— 一篇
    文章四十段, 按位置命名就会印出三十七个「后续」, 那是把一个编出来的名字重复三十七遍
    (DESIGN.md 9). 模型接手后换的是块里的字, 结构仍是这一份.
    """
    body = normalised(discovery.body or "")
    # 正文里的图也过签名代理 (5.9): 一条转发出去的正文不该因为源站今天改了防盗链规则而变成
    # 一排破图。裁到块边界之后再改写 —— 被裁掉的那几张不值得为它们签一个名。
    return {"body_html": media.proxied_html(htmlbody.clip(body)),
            "summary": _summary(body, discovery.title.strip())}


def digest(event: Event) -> list[str]:
    """规则版综述: 最新的几家各自说了什么, 一家一段. 模型接手后重写成一整段话.

    不把 `sources` 写进句子里 —— 那个数在页面上另外印着, 抄进正文就是同一个事实两个出处.
    """
    newest = sorted(event.items, key=lambda item: materials.moment(item.discovery.published_at),
                    reverse=True)[:3]
    return ["%s: %s" % (registry.of(item.discovery.source_id).name, item.summary)
            for item in newest if item.summary]


def read(discovery: Discovery, now: datetime) -> Item:
    """One raw record as the item the page draws."""
    read_out = taxonomy.classify("%s %s" % (discovery.title, discovery.body))
    source = registry.of(discovery.source_id)
    page = article(discovery)
    value = score(discovery, read_out, now)
    has_body = bool((discovery.body or "").strip())
    return Item(
        body_mode=rules.body_mode_of(source, "ok" if has_body else "missing", has_body),
        discovery=discovery,
        category=read_out["category"],
        category_label=taxonomy.category_label(read_out["category"]),
        tags=read_out["tags"],
        entities=read_out["entities"],
        first_party=source.first_party,
        score=value,
        selected=value >= CURATED_MIN_SCORE,
        reason=REASONS.get(read_out["category"], DEFAULT_REASON),
        summary=page["summary"],
        body_html=page["body_html"],
    )


def annotate(events: list[Event], now: datetime) -> list[Event]:
    """Read every item in every event, in place, and promote the answers to the event.

    The promotion is `mode` for the category and `max` for the score, and neither is an
    average: an event's weight is set by its hardest item, not by averaging it with its
    softest, and its category is what most of the coverage is *about* rather than what the
    first wire happened to mention.
    """
    for event in events:
        for item in event.items:
            if item.score == 0 and item.category == "":
                fresh = read(item.discovery, now)
                item.category = fresh.category
                item.category_label = fresh.category_label
                item.tags = fresh.tags
                item.entities = fresh.entities
                item.first_party = fresh.first_party
                item.score = fresh.score
                item.selected = fresh.selected
                item.reason = fresh.reason
                item.summary = fresh.summary
                item.body_html = fresh.body_html
                item.body_mode = fresh.body_mode
        if not event.items:
            continue
        event.category = mode(item.category for item in event.items)
        event.category_label = taxonomy.category_label(event.category)
        event.tags = sorted({tag for item in event.items for tag in item.tags})
        event.entities = sorted({name for item in event.items for name in item.entities})
        event.score = max(item.score for item in event.items)
        event.selected = any(item.selected for item in event.items)
        # 最新进展取"最新一条"的标题. 按发布时间而不是按数组顺序: 采集顺序不是时间顺序.
        newest = max(event.items, key=lambda item: materials.moment(item.discovery.published_at))
        event.latest = newest.discovery.title.strip()
        event.summary = digest(event)
    return events
