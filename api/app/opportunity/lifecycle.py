"""生命周期：一个活动会结束，而结束只有两种可信的来源。

1. **材料写了截止时间**（`deadline`），且那个时间已经过去 —— 这是一条规则，不是猜测：
   原文自己说了它到哪天为止。到点即 `retired`。
2. **人来改**（`retire()`）—— 这一版没有写接口，所以它只服务测试与将来的按钮。

除此之外**什么都不判**：一条没有截止时间的活动不会因为"看着像结束了"被退休，一条官方号
沉默三周的条目也不会 —— 沉默是未判定，把它读成结束就是拿"没问到"当答案。参考产品 126 条
项目里 `deadline` 填充 0 条，说明这件事没有便宜的自动解；这里宁可留着，也不编一个。

截止时间本身**只抽不猜**（`extract.md` 的铁律）：抽取阶段只把原文里的时间抄进来，这里只
负责读它。读不出来的字符串（"待公布"、"TBA"）保留原样显示，不参与判定。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from ..store import db

log = logging.getLogger("crypto-hunter.opportunity")

#: The shapes a deadline is accepted in. The extractor is told to copy the material's own text,
#: so the reader is what has to be generous - and anything it cannot read is simply not a
#: deadline, never a guess.
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
_DOTTED = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")


def read_deadline(text: str | None) -> datetime | None:
    """A deadline as an instant, or None when the text is not one.

    A bare date is read as the **end** of that day (23:59:59 UTC): "10/19 发放" means the
    activity is over once that day is. Reading it as the start of the day would retire a live
    activity twelve hours early, which is the worse of the two mistakes.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in (_ISO, _SLASH, _DOTTED):
        match = pattern.match(raw)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups()[:3])
        hour = int(match.groups()[3]) if match.re.groups >= 6 and match.groups()[3] else 23
        minute = int(match.groups()[4]) if match.re.groups >= 6 and match.groups()[4] else 59
        second = 0 if match.re.groups >= 6 and match.groups()[3] else 59
        try:
            return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def passed(text: str | None, *, now: datetime | None = None) -> bool:
    """Is this deadline behind us? Unreadable text is never a yes."""
    moment = read_deadline(text)
    if moment is None:
        return False
    return moment < (now or datetime.now(timezone.utc))


def sweep(*, now: datetime | None = None) -> dict:
    """Retire what the material itself said was over. Returns what it did.

    Only `promoted` rows are touched: an entry that never made the list has nothing to retire
    from, and rewriting its status would lose the reason it is sitting in the pool.
    """
    retired: list[str] = []
    for item in db.opportunity_items(status="promoted"):
        if passed(item.get("deadline"), now=now):
            db.opportunity_retire(item["slug"], "deadline_passed")
            retired.append(item["slug"])
    if retired:
        log.info("opportunity: retired %d entries whose deadline had passed", len(retired))
    return {"retired": len(retired), "slugs": retired}


def today() -> str:
    """Today, the way the page prints a date it is comparing against."""
    return date.today().isoformat()
