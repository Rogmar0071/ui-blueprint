"""
backend.app.ops_routes
=======================
Read-only endpoints for querying the server-side operations log.

Endpoints
---------
GET /v1/ops
    Return the most-recent ops events globally, with optional filters.

GET /v1/folders/{folder_id}/ops
    Return the most-recent ops events scoped to a specific folder.

Query parameters (both endpoints)
----------------------------------
source      Filter by source (backend/worker/storage/rq/db/auth).
level       Filter by level (debug/info/warning/error).
event_type  Filter by event_type (exact match).
limit       Max rows to return (default 100, max 500).
before      ISO-8601 datetime — return events with created_at < before.

All routes require ``Authorization: Bearer <API_KEY>`` when API_KEY is set.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from backend.app.auth import require_auth

router = APIRouter(tags=["ops"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _event_dict(ev) -> dict[str, Any]:
    def _dt(v):
        return v.isoformat() if isinstance(v, datetime) else v

    return {
        "id": str(ev.id),
        "created_at": _dt(ev.created_at),
        "source": ev.source,
        "level": ev.level,
        "event_type": ev.event_type,
        "message": ev.message,
        "folder_id": str(ev.folder_id) if ev.folder_id else None,
        "job_id": str(ev.job_id) if ev.job_id else None,
        "artifact_id": str(ev.artifact_id) if ev.artifact_id else None,
        "rq_job_id": ev.rq_job_id,
        "request_id": ev.request_id,
        "http_method": ev.http_method,
        "http_path": ev.http_path,
        "http_status": ev.http_status,
        "duration_ms": ev.duration_ms,
        "error_type": ev.error_type,
        "error_detail": ev.error_detail,
        "details_json": ev.details_json,
    }


# ---------------------------------------------------------------------------
# Shared DB dependency
# ---------------------------------------------------------------------------


def _db_session():
    try:
        from backend.app.database import get_session

        yield from get_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Shared query builder
# ---------------------------------------------------------------------------


def _query_events(
    db,
    *,
    folder_id_filter: Optional[uuid.UUID] = None,
    source: Optional[str] = None,
    level: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    before: Optional[datetime] = None,
) -> list:
    from sqlmodel import select

    from backend.app.models import OpsEvent

    stmt = select(OpsEvent)

    if folder_id_filter is not None:
        stmt = stmt.where(OpsEvent.folder_id == folder_id_filter)
    if source:
        stmt = stmt.where(OpsEvent.source == source)
    if level:
        stmt = stmt.where(OpsEvent.level == level)
    if event_type:
        stmt = stmt.where(OpsEvent.event_type == event_type)
    if before:
        stmt = stmt.where(OpsEvent.created_at < before)

    stmt = stmt.order_by(OpsEvent.created_at.desc()).limit(limit)
    return db.exec(stmt).all()


# ---------------------------------------------------------------------------
# GET /v1/ops  — global ops log
# ---------------------------------------------------------------------------


@router.get("/v1/ops", dependencies=[Depends(require_auth)])
def list_ops_global(
    source: Optional[str] = Query(default=None),
    level: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: Optional[str] = Query(default=None, description="ISO-8601 datetime upper bound"),
    db=Depends(_db_session),
) -> JSONResponse:
    """Return the most-recent ops events across all folders."""
    before_dt = _parse_before(before)
    events = _query_events(
        db,
        source=source,
        level=level,
        event_type=event_type,
        limit=limit,
        before=before_dt,
    )
    return JSONResponse(content={"events": [_event_dict(e) for e in events]})


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/ops  — per-folder ops log
# ---------------------------------------------------------------------------


@router.get("/v1/folders/{folder_id}/ops", dependencies=[Depends(require_auth)])
def list_ops_for_folder(
    folder_id: str,
    source: Optional[str] = Query(default=None),
    level: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: Optional[str] = Query(default=None, description="ISO-8601 datetime upper bound"),
    db=Depends(_db_session),
) -> JSONResponse:
    """Return the most-recent ops events for a specific folder."""
    try:
        fid = uuid.UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid folder_id: {folder_id!r}") from None

    # Verify folder exists.
    from backend.app.models import Folder

    folder = db.get(Folder, fid)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    before_dt = _parse_before(before)
    events = _query_events(
        db,
        folder_id_filter=fid,
        source=source,
        level=level,
        event_type=event_type,
        limit=limit,
        before=before_dt,
    )
    return JSONResponse(content={"events": [_event_dict(e) for e in events]})


# ---------------------------------------------------------------------------
# Internal helper: summarized ops window for AI context injection
# ---------------------------------------------------------------------------

_OPS_WINDOW_LIMIT = 15


def build_ops_context_snippet(db) -> str:
    """
    Return a compact, token-efficient text summary of the most recent ops
    events for injection into the Global AI system prompt.

    Each line is: ``<created_at_short> [<source>/<level>] <event_type>: <message>``
    """
    from sqlmodel import select

    from backend.app.models import OpsEvent

    events = db.exec(
        select(OpsEvent)
        .order_by(OpsEvent.created_at.desc())
        .limit(_OPS_WINDOW_LIMIT)
    ).all()

    if not events:
        return ""

    lines = []
    for ev in reversed(events):
        ts = ev.created_at.strftime("%H:%M:%S") if ev.created_at else "??:??:??"
        fid_part = f" folder={str(ev.folder_id)[:8]}" if ev.folder_id else ""
        lines.append(
            f"{ts} [{ev.source}/{ev.level}] {ev.event_type}: {ev.message}{fid_part}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _parse_before(before: Optional[str]) -> Optional[datetime]:
    if not before:
        return None
    try:
        return datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid 'before' datetime: {before!r}. Use ISO-8601 format.",
        ) from None
