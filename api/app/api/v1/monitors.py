"""Monitors: one standing question per row-set, on its own clock (2026-10-29)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...monitors import job, settings
from ...monitors.registry import MONITORS
from ...store import db
from . import schemas

router = APIRouter(prefix="/monitors", tags=["monitors"])


def _monitor(monitor_id: str):
    monitor = MONITORS.get(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="没有这个监控")
    return monitor


def _scan(scan: dict | None) -> schemas.MonitorScanOut | None:
    if not scan:
        return None
    return schemas.MonitorScanOut(**scan)


@router.get("", response_model=schemas.MonitorListOut)
def list_monitors() -> dict:
    """Every monitor this build knows, with its card count and its last sweep."""
    counts = db.monitor_card_counts()
    rows = []
    for monitor in MONITORS.values():
        values = settings.values(monitor)
        scan = db.monitor_scan_latest(monitor.id)
        rows.append({
            "id": monitor.id,
            "name": monitor.name,
            "tagline": monitor.tagline,
            "fields": list(monitor.fields),
            "enabled": bool(values["enabled"]),
            "schedule": bool(values["schedule"]),
            "notes": list(monitor.notes),
            "cards": counts.get(monitor.id, 0),
            "scan": _scan(scan),
            "running": bool(scan and scan["status"] == "running"),
            # 设定页不再逐条请求：一次列表就把每个监控的旋钮带回。
            "settings": {
                "enabled": values["enabled"],
                "schedule": values["schedule"],
                "interval_hours": values["interval_hours"],
                "start_hour": values["start_hour"],
                "start_minute": values["start_minute"],
                "window_hours": values["window_hours"],
                "slice_hours": values["slice_hours"],
                "min_score": values["min_score"],
                "queries": list(values["queries"]),
            },
        })
    return {"monitors": rows}


@router.get("/{monitor_id}/cards", response_model=schemas.MonitorCardsOut)
def list_cards(monitor_id: str, limit: int = Query(0, ge=0, le=2000)) -> dict:
    monitor = _monitor(monitor_id)
    return {"monitor": monitor.id, "cards": db.monitor_cards(monitor.id, limit=limit)}


@router.get("/{monitor_id}/settings", response_model=schemas.MonitorSettingsOut)
def read_settings(monitor_id: str) -> dict:
    monitor = _monitor(monitor_id)
    return {"monitor": monitor.id, "values": settings.values(monitor),
            "notes": list(monitor.notes)}


@router.put("/{monitor_id}/settings", response_model=schemas.MonitorSettingsOut)
def write_settings(monitor_id: str, body: schemas.MonitorSettingsIn) -> dict:
    monitor = _monitor(monitor_id)
    patch = {key: value for key, value in body.model_dump().items() if value is not None}
    return {"monitor": monitor.id, "values": settings.write(monitor, patch),
            "notes": list(monitor.notes)}


@router.post("/{monitor_id}/scan", status_code=202, response_model=schemas.MonitorScanOut)
def start_scan(monitor_id: str) -> dict:
    monitor = _monitor(monitor_id)
    try:
        return _scan(job.start(monitor.id, trigger="manual"))
    except RuntimeError:
        raise HTTPException(status_code=409, detail="这个监控已经在跑")


@router.get("/{monitor_id}/scans/{scan_id}", response_model=schemas.MonitorScanOut)
def get_scan(monitor_id: str, scan_id: str) -> dict:
    _monitor(monitor_id)
    scan = db.monitor_scan_get(scan_id)
    if not scan or scan["monitor"] != monitor_id:
        raise HTTPException(status_code=404, detail="没有这次扫描")
    return _scan(scan)


@router.post("/{monitor_id}/scans/{scan_id}/cancel", response_model=schemas.MonitorScanOut)
def cancel_scan(monitor_id: str, scan_id: str) -> dict:
    _monitor(monitor_id)
    result = job.cancel(scan_id)
    scan = result.get("scan")
    if not scan or scan["monitor"] != monitor_id:
        raise HTTPException(status_code=404, detail="没有这次扫描")
    return _scan(scan)
