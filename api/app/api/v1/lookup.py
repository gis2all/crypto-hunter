"""CA search: one address in, one card out.

The work is in `app/lookup.py` - this is the endpoints the page needs, plus the one
decision the API owns: something that is not an address at all is a 404 with the reason
before any work is queued. Whether the feed has ever seen an address can only be asked
inside the run, so that refusal comes back on the job instead.

`POST` queues the search and answers at once. It used to hold the request open for the whole
of it - a minute or two of network and a model call - and the page had nothing to show for
that time but a spinner. A job is also the honest shape for a failure halfway through: 认链
answers "DexScreener 没见过这个 CA" from inside the run, and the panel then says which stage
stopped and why, the same reading a failed round gives (`api/v1/runs.py`).

`GET /recent` reads the cards already stored, which is why clicking one of them is instant and
free: the search is never run twice by opening it a second time. The two `DELETE`s are
搜索管理's, and they are the same list seen from the other side: a record can be dropped, and
a row the reader starred - a card on the shelf - cannot (see each route).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import lookup as ca_lookup
from ...report import assemble as report_assemble
from .schemas import FavoriteOut, JobOut, RemovedOut

router = APIRouter(prefix="/lookup", tags=["lookup"])


class LookupIn(BaseModel):
    """An address to search. The chain is not part of the ask: an address implies it, and the
    reader typing one does not necessarily know which chain it is on (`app/lookup.py`
    `resolve` answers that from the feed, deepest chain first)."""

    ca: str


@router.post("", response_model=JobOut, status_code=202)
def start_search(body: LookupIn) -> dict:
    """Queue one CA search. Answers with the job to poll, not with the card."""
    try:
        return ca_lookup.start(body.ca)
    except ca_lookup.Refused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/latest", response_model=JobOut | None)
def read_latest_job() -> dict | None:
    """The newest search this app ran, so 运行结果 has something to fold on a first visit.

    Declared above `/jobs/{job_id}` on purpose: FastAPI matches routes in the order they are
    written, and the other one would read "latest" as a job id and answer 404.
    """
    return ca_lookup.latest()


@router.get("/jobs/{job_id}", response_model=JobOut)
def read_job(job_id: str) -> dict:
    """Where a queued search has got to, and the card once it has one."""
    job = ca_lookup.state(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这个搜索任务：%s" % job_id)
    return job


@router.get("/recent", response_model=list[FavoriteOut])
def recent_lookups() -> list[dict]:
    """What has been searched lately, newest first, cards and all - the page's own list."""
    return [report_assemble.row_card(row) for row in ca_lookup.recent()]


@router.delete("/recent", response_model=RemovedOut)
def clear_recent_lookups() -> dict:
    """Empty 最近搜索 of the records that are only records.

    Never a card on the shelf: a row the reader starred is a favourite, and the star is the
    only write that ends that. A list that had nothing to drop answers false, which
    is one answer rather than an error - the same way forgetting a card twice is not one.
    """
    return {"removed": ca_lookup.forget_all() > 0}


@router.delete("/recent/{chain_id}/{ca}", response_model=RemovedOut)
def forget_recent_lookup(chain_id: str, ca: str) -> dict:
    """Drop one record. A row that is not there is not an error, and neither is one the reader
    starred - that row is a card on the shelf, and 搜索管理 greys it out rather than offering
    a button that would silently do nothing."""
    return {"removed": ca_lookup.forget(chain_id, ca)}
