"""发布规则：一条规则只写一次，所有出口都调它（AIHOT 的 `publication/rules.ts`）。

为什么单独成一个文件：可见性、发布门、资格、正文档位、能不能再分发 —— 这些
从前散在 `views.py`、`db.py` 与几段 SQL 里，同一件事在三处各写一遍，于是
“精选里有它、RSS 里没有它”这种分歧只能靠读三个文件才发现。

这一层不读库、不写库：它只把“什么样的东西能印”说清楚，库里那一行对不对得上由
`scope.py` 去问。

**没有全部门槛的名单**。这一层放的是判据本身（写一次，所有出口调它）；"一共几道门槛、
谁卡在哪一道"在 `eligibility.py` —— 那是唯一一处能把资格看全的地方（5.5），
它引用本文件的谓词，反过来不成立。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .lib import calendar as cal

#: 发布门：一条从被读到到公开的缓冲（AIHOT 的 `visible_after`）。
#: 三分钟里还能补正文、补来源名、把一条误判收回来 —— 而读者看不见这个窗口。
RELEASE_DELAY_SECONDS = 180

#: 卡片上的渠道（AIHOT 的 `CHANNEL_KEYS`）。`all` 与 `firstParty` 是筛选键，
#: `news` / `x` 是每一条自己属于哪一路 —— 前一对是读者的问题，后一对是材料的属性。
CHANNELS = ("all", "news", "x", "firstParty")

#: 出口分页的一份规格（AIHOT 的 `pool.ts`）。
PAGE_SIZE = 40
MAX_PAGES = 50


def channel_of(source_kind: str, has_x_post: bool) -> str:
    """这一条走哪一路。X 源与带推文的源都算 `x`。"""
    return "x" if (source_kind in ("x", "x_search") or has_x_post) else "news"


def is_pool_eligible(participation: str, relevance: str | None,
                     title: str | None, summary: str | None) -> bool:
    """能不能进池子：编辑类源、预筛过了、中文标题与摘要都在。"""
    return (participation == "editorial" and relevance == "pass"
            and bool(title) and bool(summary))


def has_item_page(visibility: str, source_mode: str) -> bool:
    """这一条有没有自己的页（下架的不算）。"""
    return visibility != "withdrawn" and source_mode == "editorial"


def is_selectable(eligible: bool, judged_selected: bool | None, tier: str) -> bool:
    """能不能进精选：进了池子、判过选了、而且这一档允许。"""
    return eligible and judged_selected is True and tier != "EXCLUDE_MP"


def body_mode_of(source, body_status: str, has_body: bool) -> str:
    """页面上印全文还是摘要（AIHOT 的 `bodyModeOf`）。

    两件事同时成立才给 `full`：清单允许印全文，而且这一版确实读到了正文。
    “能抓到”不等于“可以印”—— 一个图书馆/付费墙源下清单里写的就是不允许。
    """
    allowed = bool(getattr(source, "site_fulltext", False))
    return "full" if (allowed and body_status == "ok" and has_body) else "summary"


def may_redistribute(source, body_mode: str) -> bool:
    """能不能把正文再分发给出口（RSS / API）。比“印在站内”严一档。"""
    return bool(getattr(source, "syndicate_fulltext", False)) and body_mode == "full"


def is_indexable(visibility: str, has_summary: bool, selected: bool,
                 seo_indexed_at: str | None, seo_excluded_at: str | None) -> bool:
    """搜索引擎能不能收录：公开、有摘要、没被排除，而且要么入选、要么编辑手动收录。"""
    return (visibility == "public" and has_summary
            and not seo_excluded_at and (selected or bool(seo_indexed_at)))


def release_at(seen_at: str) -> str:
    """这一条什么时候可以公开。"""
    return (cal.moment_of(seen_at) + timedelta(seconds=RELEASE_DELAY_SECONDS)).isoformat(timespec="seconds")


def release_deadline(now: datetime) -> str:
    """现在这一刻能公开的最晚边界：比它早公开的都过了门。"""
    return (now - timedelta(seconds=RELEASE_DELAY_SECONDS)).isoformat(timespec="seconds")
