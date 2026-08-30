"""新机会：外部发现的读与跑。

四个端点，两读两写：一次读给出页面要的全部（条目、计数、最近一次扫描、页面文案），
watchlist 单独读，扫描可以启动并按 id 轮询。

扫描是一个可轮询的 job（`opportunity/job.py`），不是一轮：它不写 `runs`，不属于任何链、
任何设定与任何报告库。这一点与 CA 搜索同源（`api/v1/lookup.py`），也是为什么它的行住在
`opportunity_scans` 而不是 `runs`。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...opportunity import heat, job, promote, settings, watchlist
from ...store import db
from . import forms
from .schemas import (OpportunityItemOut, OpportunityListOut, OpportunityScanOut,
                      OpportunityScanRowOut, OpportunitySettingsIn, OpportunitySettingsOut,
                      OpportunitySourceOut, OpportunityStateIn, OpportunityStepOut)

router = APIRouter(prefix="/opportunity", tags=["opportunity"])


def _step(row: dict) -> OpportunityStepOut:
    return OpportunityStepOut(ordinal=row.get("ordinal") or 0, title=row.get("title"),
                              body=row.get("body") or "", source_url=row.get("source_url"),
                              source_at=row.get("source_at"))


def _item(row: dict) -> OpportunityItemOut:
    """One stored entry as the page reads it.

    Two translations happen here and only here: the barrier column is a comma-joined list and
    the page wants a list, and the reason column holds a key while the page prints the sentence.
    Both are the same rule - the frontend does not decode anything.
    """
    barrier = [name for name in str(row.get("barrier") or "").split(",") if name]
    return OpportunityItemOut(
        slug=row["slug"], kind=row["kind"], track=row.get("track") or "actionable",
        category=row.get("category"), title=row["title"],
        organizer=row.get("organizer"), official_url=row.get("official_url"),
        summary=row.get("summary"), trust=row.get("trust") or "unjudged",
        trust_basis=row.get("trust_basis"), ecosystem=row.get("ecosystem"),
        source_type=row.get("source_type"), risk=row.get("risk"), barrier=barrier,
        maturity=row.get("maturity"), started_at=row.get("started_at"),
        deadline=row.get("deadline"), status=row.get("status") or "candidate",
        reason=promote.reason_label(row.get("reason")),
        user_state=row.get("user_state") or "active",
        heat=row.get("heat"), heat_band=heat.band(row.get("heat")),
        heat_basis=row.get("heat_basis") or None,
        discovered_at=row["discovered_at"],
        source_kind=row.get("source_kind"), source_ref=row.get("source_ref"),
        extract_version=row.get("extract_version"),
        novelty=row.get("novelty"),
        steps=[_step(step) for step in row.get("steps") or []],
    )


def _scan(row: dict | None) -> OpportunityScanOut | None:
    if row is None:
        return None
    return OpportunityScanOut(
        id=row["id"], status=row["status"], stage=row.get("stage"),
        stages=row.get("stages") or [], counters=row.get("counters") or {},
        error=row.get("error"), started_at=row["started_at"], finished_at=row.get("finished_at"),
    )


@router.get("", response_model=OpportunityListOut)
def list_opportunities(limit: int = Query(300, ge=1, le=1000)) -> dict:
    """Everything the page draws, in one read.

    The un-promoted rows come back with the promoted ones and are told apart by `status`: a
    page that only ever received successes could not answer "why is this not on the list", and
    that question is the whole reason the gate records a reason.
    """
    watchlist.ensure_seeded()
    items = db.opportunity_items(limit=limit)
    counts = db.opportunity_counts()
    return {"items": [_item(row) for row in items], "counts": counts,
            "scan": _scan(job.latest()),
            # The panel is built from the rows this answer carries: an option nobody used is a
            # switch that can only empty the page, so it is not sent.
            "filters": forms.opportunity_filters(items, counts),
            "sorts": forms.opportunity_sorts(),
            "form": forms.opportunity_form()}


@router.get("/settings", response_model=OpportunitySettingsOut)
def read_settings() -> dict:
    """The knobs this module runs on, as the 设定 tab draws them."""
    return settings.form()


@router.put("/settings", response_model=OpportunitySettingsOut)
def save_settings(body: OpportunitySettingsIn) -> dict:
    """Save what the page sent. Unknown keys are ignored rather than stored."""
    settings.write(body.values or {})
    return settings.form()


@router.get("/scans", response_model=list[OpportunityScanRowOut])
def list_scans(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    """Every scan so far, newest first: the 归档 tab and the strip under the bar."""
    return db.opportunity_scans(limit=limit)


@router.get("/queues")
def read_queues(limit: int = Query(6, ge=0, le=2000)) -> dict:
    """What is still waiting: 未分组 / 未抽取 / 溯源失败，各带条数与分数最高的几条。"""
    return db.opportunity_queues(limit=limit)


@router.patch("/items/{slug}", response_model=OpportunityItemOut)
def set_item_state(slug: str, body: OpportunityStateIn) -> dict:
    """归档 / 删除 / 恢复 one entry, by hand.

    The one write this page has, and it is the reader's own: 归档 is "I know about this and it is
    not a priority", 删除 is "I do not want it" (soft - the row and its evidence stay). Neither
    touches the pipeline's reading, so the card keeps saying why it is not on the list.
    """
    if body.user_state not in db.USER_STATES:
        raise HTTPException(status_code=422,
                            detail="user_state 只能是 %s" % " / ".join(db.USER_STATES))
    if not db.opportunity_user_state(slug, body.user_state):
        raise HTTPException(status_code=404, detail="没有这个条目")
    row = db.opportunity_item(slug)
    return _item(row)


@router.get("/sources", response_model=list[OpportunitySourceOut])
def list_sources() -> list[dict]:
    """The watchlist as it stands: what a scan will read next time."""
    watchlist.ensure_seeded()
    return watchlist.enabled()


@router.post("/scan", response_model=OpportunityScanOut, status_code=202)
def start_scan() -> dict:
    """Start one scan and answer with the row to poll.

    A second scan is refused while one is running rather than queued: two scans read the same
    timelines, the X throttle serialises them anyway, and the second one would only pay twice
    for the same material.
    """
    if job.running():
        raise HTTPException(status_code=409, detail="已经有一次扫描在跑，等它结束再开下一次")
    return _scan(job.start())


@router.post("/scans/{scan_id}/cancel")
def cancel_scan(scan_id: str) -> dict:
    """Ask the running scan to stop. Idempotent, and never an error for a scan that just ended.

    The page may have been polling a row that finished between the click and this call, and "it
    is not running any more" is the answer the reader wanted - so a cancel for an unknown or
    finished scan answers `cancelled: false` with the reason rather than a 404. The scan stops at
    the next X call rather than at the end of one, which is what makes it worth having: the thing
    being waited on is usually a rate limit.
    """
    return job.cancel(scan_id)


@router.get("/scans/{scan_id}", response_model=OpportunityScanOut)
def read_scan(scan_id: str) -> dict:
    row = job.state(scan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这次扫描")
    return _scan(row)
