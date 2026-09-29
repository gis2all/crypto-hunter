"""Screening rounds.

`POST /runs` creates a pending round and returns immediately; the pipeline drives it.
`GET /runs/{id}/batch` returns everything one page draws - the batch strip, the funnel,
the change list, the cards and the落选 table - in a single call: the SPA must not have
to stitch ten requests together.
`GET /runs/{id}/stream` is SSE over real status transitions read from the database.
`POST /runs/{id}/cancel` asks a round that is still going to stop; the round answers at its
own next checkpoint, so the request never waits for it (`app/cancel.py`).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import cancel, config
from ...core.models import RunStatus
from ...pipeline import run as pipeline_run
from ...pipeline import schedule, states
from ...report import assemble as report_assemble
from ...store import db
from .schemas import BatchOut, CardOut, RunOut

router = APIRouter(prefix="/runs", tags=["runs"])

TERMINAL = states.TERMINAL


class RunCreate(BaseModel):
    chain_id: str
    #: Which of the chain's sets of settings to run. Blank means the default one, which
    #: is what a client that never learned about the switcher sends.
    config_id: str | None = None
    trigger: str = "manual"


def _fetch(run_id: str) -> sqlite3.Row | None:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()


@router.get("", response_model=list[RunOut])
def list_runs(chain_id: str | None = None, config_id: str | None = None,
              limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    conn = db.connect()
    try:
        # Read through the set of settings as well as the chain: two sets on one chain are
        # two report libraries, and a list that mixed them would read as one chain having
        # run twice as often.
        where: list[str] = []
        args: list[object] = []
        if chain_id:
            where.append("chain_id = ?")
            args.append(chain_id)
        if config_id:
            where.append("config_id = ?")
            args.append(config_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            "SELECT * FROM runs%s"
            " ORDER BY COALESCE(scheduled_at, started_at) DESC LIMIT ?" % clause,
            (*args, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("", response_model=RunOut, status_code=201)
def create_run(body: RunCreate) -> dict:
    if body.chain_id not in config.load_chains():
        raise HTTPException(status_code=404, detail=f"没有这条链：{body.chain_id}")
    row = pipeline_run.create_run(body.chain_id, trigger=body.trigger,
                                  config_id=body.config_id)
    # The round runs on the scheduler's own thread: a real one takes minutes, and the
    # request that starts it must not be held open for them.
    schedule.submit(body.chain_id, row["id"], row.get("config_id"))
    return row


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: str) -> dict:
    """Stop a round that is still going. Idempotent, and never waits for the round.

    A round is minutes of network on a worker thread, and an HTTP handler cannot reach into
    that thread. So the request leaves a flag for the round to find at its next checkpoint,
    and returns at once: what comes back is the row as it stands, which is `running` for
    another second or two and then `cancelled` - the page already polls the row.

    The one exception is a round nobody has started: nothing will ever read the flag, and the
    scheduler starts every round it finds at `pending` again, so that one is settled here
    instead of made to look like it is still waiting.
    """
    row = _fetch(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这一轮：{run_id}")
    if row["status"] in TERMINAL:
        return dict(row)
    cancel.request(run_id)
    if row["status"] == RunStatus.PENDING.value:
        pipeline_run.stop_run(run_id)
    return dict(_fetch(run_id))


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str) -> dict:
    row = _fetch(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这一轮：{run_id}")
    return dict(row)


@router.get("/{run_id}/batch", response_model=BatchOut)
def get_batch(run_id: str, per_round: int = Query(default=5, ge=1, le=50)) -> dict:
    """Everything the report page draws: batch strip, funnel, changes, cards,落选表."""
    if _fetch(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这一轮：{run_id}")
    return report_assemble.batch(run_id, per_round=per_round)


@router.get("/{run_id}/reports", response_model=list[CardOut])
def get_reports(run_id: str) -> list[dict]:
    if _fetch(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这一轮：{run_id}")
    return report_assemble.cards(run_id)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    timeout_s: int = Query(default=120, ge=5, le=900),
) -> StreamingResponse:
    """SSE. Events: `status` on every transition, then `done`, `timeout` or `error`."""
    row = _fetch(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这一轮：{run_id}")

    async def events():
        yield _sse({"type": "status", "run_id": run_id, "status": row["status"]})
        if row["status"] in TERMINAL:
            yield _sse({"type": "done", "status": row["status"]})
            return
        last = row["status"]
        for _ in range(timeout_s):
            await asyncio.sleep(1)
            fresh = _fetch(run_id)
            if fresh is None:
                yield _sse({"type": "error", "detail": "run disappeared"})
                return
            if fresh["status"] != last:
                last = fresh["status"]
                yield _sse(
                    {"type": "status", "run_id": run_id, "status": last, "error": fresh["error"]}
                )
                if last in TERMINAL:
                    yield _sse({"type": "done", "status": last})
                    return
        yield _sse({"type": "timeout"})

    return StreamingResponse(events(), media_type="text/event-stream")
