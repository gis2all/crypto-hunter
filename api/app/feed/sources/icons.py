"""来源图标 (AIHOT 的 `sources/icons.ts`): 一家在报道旁边、在热点榜上露的那张脸。

清单里写了 `icon:` 的以清单为准 —— 那是人手核过的地址。没写的到这里来找: 读它的首页, 从
`<link rel=icon>` 里挑最好的一枚 (touch 图标优先, 大的优先, svg 次之, `.ico` 最后), 挑不到
就试 `/favicon.ico`。

**空手而归也要等一个周期再试** (RETRY_DAYS)。一个站点没有图标很少会长出来, 而每一轮都去敲
一遍是拿我们的时间换一个不会变的答案。找过的结论 (哪怕是空的) 都写进 `icon_checked_at`,
所以"还没找过"与"找过没有"在表上是两件事。

X 那种源没有首页可读 —— 它的头像是那条推文自己带的图。所以这一站跳过它, 只记一笔"看过了",
留给清单里的 `icon:` 去写。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ...sources import http
from ...store import db
from . import registry

log = logging.getLogger(__name__)

#: 一轮找几家。这一站是"顺手补一格", 不该占着一轮的预算。
BATCH = 40
#: 没找到的隔多久再试。
RETRY_DAYS = 30
#: 首页最多读多少 —— 首页比文章页小得多, 超过这个数多半是读错了东西。
MAX_BYTES = 400_000


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class _Links(HTMLParser):
    """首页里那几枚图标: `<link rel=... href=... sizes=...>`。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str, str]] = []   # (rel, href, sizes)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        rel = values.get("rel", "").lower()
        href = values.get("href", "").strip()
        if "icon" in rel and href and not href.startswith("data:"):
            self.found.append((rel, href, values.get("sizes", "")))


def candidates(text: str, base: str) -> list[str]:
    """首页声明的那几枚图标, 最好的排前面; 末尾永远补一个 `/favicon.ico`。

    打分照 AIHOT 的 `iconCandidates`: touch 图标最稳 (它本来就是为大图标准备的), 其次看
    `sizes` 里最大的那一档, svg 加分、`.ico` 减分 —— 后者常常是一枚 16px 的糊图。
    """
    parser = _Links()
    try:
        parser.feed(text)
    except Exception:   # 坏 HTML 不是这一站的事, 能读到几枚算几枚
        pass
    scored: list[tuple[int, str]] = []
    for rel, href, sizes in parser.found:
        try:
            url = urljoin(base, href)
        except ValueError:
            continue
        biggest = max([int(number) for number in re.findall(r"(\d+)x\d+", sizes)] or [0])
        score = (1000 if "apple-touch-icon" in rel else 0)
        score += min(biggest, 512)
        score += 200 if re.search(r"\.svg(\?|$)", href, re.I) else 0
        score -= 300 if re.search(r"\.ico(\?|$)", href, re.I) else 0
        scored.append((score, url))
    ordered = [url for _, url in sorted(scored, key=lambda pair: -pair[0])]
    fallback = urljoin(base, "/favicon.ico")
    out: list[str] = []
    for url in [*ordered, fallback]:
        if url not in out:
            out.append(url)
    return out


def _home(row: dict) -> str:
    """这一家的首页: 优先清单里写明的地址, 否则从它的订阅地址里取源。

    `feedUrl` 常常是同一台机器上的另一个路径 (`/rss.xml`), 取源就够了 —— 图标挂在站点上,
    不挂在某一个订阅上。
    """
    config = row.get("config") or {}
    for key in ("url", "site", "home", "feedUrl"):
        raw = str(config.get(key) or "").strip()
        if not raw:
            continue
        try:
            parts = urlsplit(raw)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.netloc:
            return "%s://%s/" % (parts.scheme, parts.netloc)
    return ""


def _find(home: str) -> str:
    """读首页, 挑第一枚能用的。读不到就回空 —— 空也是一次结论。"""
    try:
        text = http.get(home, timeout=15.0, tries=1)
    except Exception as exc:
        log.info("feed icons: %s unreadable (%s)", home, repr(exc)[:80])
        return ""
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES]
    for url in candidates(text, home):
        if len(url) <= 300:
            return url
    return ""


def refresh(moment: datetime | None = None, limit: int = BATCH) -> dict:
    """给还没有图标的那几家找一枚。"""
    now = moment or datetime.now(timezone.utc)
    before = _stamp(now - timedelta(days=RETRY_DAYS))
    rows = db.feed_sources_need_icon(before, limit)
    checked = 0
    found = 0
    for row in rows:
        if row.get("kind") in ("x_search", "x"):
            # 推文那头像不在首页上, 没有可找的地方。记一笔"看过了", 免得每一轮都挑中它。
            db.feed_sources_set_icon(row["id"], "", _stamp(now))
            checked += 1
            continue
        home = _home(row)
        icon = _find(home) if home else ""
        db.feed_sources_set_icon(row["id"], icon, _stamp(now))
        checked += 1
        found += 1 if icon else 0
        if icon:
            log.info("feed icons: %s -> %s", row["id"], icon)
    if checked:
        registry.forget()
    return {"checked": checked, "found": found}