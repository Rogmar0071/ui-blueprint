"""
backend.app.folder_routes
==========================
FastAPI router implementing folder-based clip-bundle persistence.

Endpoints
---------
POST   /v1/folders                              create folder                       [auth]
GET    /v1/folders                              list folders                        [auth]
GET    /v1/folders/{folder_id}                  get folder detail                   [auth]
PATCH  /v1/folders/{folder_id}                  rename folder                       [auth]
DELETE /v1/folders/{folder_id}                  delete folder (cascade)             [auth]
POST   /v1/folders/{folder_id}/clip             upload clip to folder               [auth]
GET    /v1/folders/{folder_id}/artifacts/{id}   presigned/streaming artifact URL    [auth]
POST   /v1/folders/{folder_id}/messages         send chat message (AI replies)      [auth]
GET    /v1/folders/{folder_id}/messages         list chat messages                  [auth]
POST   /v1/folders/{folder_id}/jobs             enqueue a job                       [auth]
GET    /v1/folders/{folder_id}/jobs             list jobs                           [auth]
GET    /v1/folders/{folder_id}/jobs/{job_id}    get job status                      [auth]
DELETE /v1/folders/{folder_id}/jobs/{job_id}    delete job + linked artifacts       [auth]

All routes require ``Authorization: Bearer <API_KEY>`` when API_KEY is set.

Dependencies
------------
DATABASE_URL   Postgres (or SQLite for tests) connection URL.
               When absent, all folder endpoints return HTTP 503.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from backend.app.auth import require_auth
from backend.app.ops_log import log_event
from ui_blueprint.domain.ir import SCHEMA_VERSION
from ui_blueprint.prompt_security import (
    append_prompt_injection_defense,
    format_untrusted_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/folders", tags=["folders"])


class FolderChatMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    folder_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str | None = None


class FolderChatListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    messages: list[FolderChatMessageResponse]


class FolderChatPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class FolderChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    user_message: FolderChatMessageResponse
    assistant_message: FolderChatMessageResponse
    tools_available: list[str]
    enqueued_job: dict[str, Any] | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_RE_STR = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _parse_uuid(value: str, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value!r}") from None


def _db_session():
    """FastAPI dependency – yields a DB session or raises 503 if unconfigured."""
    try:
        from backend.app.database import get_session

        yield from get_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _folder_or_404(session, folder_id: uuid.UUID):
    from backend.app.models import Folder

    folder = session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


def _dt(dt: datetime) -> str:
    """Serialise a datetime to ISO 8601 string (UTC)."""
    return dt.isoformat() if dt else None


def _folder_dict(folder) -> dict[str, Any]:
    return {
        "id": str(folder.id),
        "title": folder.title,
        "status": folder.status,
        "clip_object_key": folder.clip_object_key,
        "audio_object_key": folder.audio_object_key,
        "created_at": _dt(folder.created_at),
        "updated_at": _dt(folder.updated_at),
    }


def _job_dict(job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "folder_id": str(job.folder_id),
        "type": job.type,
        "status": job.status,
        "progress": job.progress,
        "error": job.error,
        "rq_job_id": job.rq_job_id,
        "source_artifact_id": str(job.source_artifact_id) if job.source_artifact_id else None,
        "options": job.analyze_options,
        "analyze_clip_object_key": job.analyze_clip_object_key,
        "created_at": _dt(job.created_at),
        "updated_at": _dt(job.updated_at),
    }


def _artifact_dict(artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "folder_id": str(artifact.folder_id),
        "job_id": str(artifact.job_id) if artifact.job_id else None,
        "type": artifact.type,
        "object_key": artifact.object_key,
        "display_name": artifact.display_name,
        "created_at": _dt(artifact.created_at),
    }


def _message_dict(msg) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "folder_id": str(msg.folder_id),
        "role": msg.role,
        "content": msg.content,
        "created_at": _dt(msg.created_at),
    }


def _json_response(model: BaseModel, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=model.model_dump(mode="json", exclude_none=True),
    )


def _next_upload_sequence(db, folder_id: uuid.UUID, artifact_type: str) -> int:
    from sqlmodel import select

    from backend.app.models import Artifact

    existing = db.exec(
        select(Artifact).where(Artifact.folder_id == folder_id, Artifact.type == artifact_type)
    ).all()
    return len(existing) + 1


def _build_upload_identity(
    db,
    folder_id: uuid.UUID,
    artifact_type: str,
    fallback_extension: str,
) -> tuple[str, str]:
    label = {
        "clip": "Clip",
        "audio_m4a": "Audio",
        "repo_zip": "Repository",
        "folder_upload_zip": "Folder",
    }.get(artifact_type, "Upload")
    sequence = _next_upload_sequence(db, folder_id, artifact_type)
    filename_stem = label.lower().replace(" ", "_")
    return (
        f"{label} {sequence}",
        f"{filename_stem}-{sequence}{fallback_extension}",
    )


def _artifact_for_object_key(db, folder_id: uuid.UUID, object_key: str):
    from sqlmodel import select

    from backend.app.models import Artifact

    if not object_key:
        return None
    return db.exec(
        select(Artifact)
        .where(Artifact.folder_id == folder_id)
        .where(Artifact.object_key == object_key)
        .order_by(Artifact.created_at.desc())
    ).first()


def _persist_repo_upload(db, folder_id: uuid.UUID, local_path: str):
    """Upload a repo ZIP file and create its artifact row."""
    from backend.app import storage
    from backend.app.models import Artifact

    display_name, storage_filename = _build_upload_identity(db, folder_id, "repo_zip", ".zip")
    key = storage.upload_file(str(folder_id), storage_filename, local_path, "application/zip")

    artifact = Artifact(
        folder_id=folder_id,
        type="repo_zip",
        object_key=key,
        display_name=display_name,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    return artifact, key


def _persist_folder_upload(db, folder_id: uuid.UUID, local_path: str):
    """Upload a folder archive and create its artifact row."""
    from backend.app import storage
    from backend.app.models import Artifact

    display_name, storage_filename = _build_upload_identity(
        db, folder_id, "folder_upload_zip", ".zip"
    )
    key = storage.upload_file(str(folder_id), storage_filename, local_path, "application/zip")

    artifact = Artifact(
        folder_id=folder_id,
        type="folder_upload_zip",
        object_key=key,
        display_name=display_name,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact, key


def _enqueue_repo_analysis_job(db, folder_id: uuid.UUID, artifact_id: uuid.UUID):
    """Create and enqueue an analyze_repo job anchored to a repo_zip artifact."""
    from sqlmodel import select

    from backend.app import worker
    from backend.app.models import Job

    existing = db.exec(
        select(Job)
        .where(Job.folder_id == folder_id)
        .where(Job.type == "analyze_repo")
        .where(Job.source_artifact_id == artifact_id)
        .where(Job.status.in_(["queued", "running"]))
        .order_by(Job.created_at.asc())
    ).first()
    if existing is not None:
        return existing, True

    job = Job(folder_id=folder_id, type="analyze_repo", source_artifact_id=artifact_id)
    db.add(job)
    db.commit()
    db.refresh(job)

    job_id_str = str(job.id)
    rq_id = worker.enqueue_job(job_id_str, "analyze_repo")
    if rq_id:
        job.rq_job_id = rq_id
        db.add(job)
        db.commit()
        db.refresh(job)

    return job, False


def _find_active_analyze_job(db, folder_id: uuid.UUID):
    """
    Return an existing ``queued`` or ``running`` analyze job for *folder_id*,
    or ``None`` if none exists.

    Called by /clip and /messages before creating a new analyze job so that
    duplicate submissions are deduplicated.
    """
    from sqlmodel import select

    from backend.app.models import Job

    return db.exec(
        select(Job)
        .where(Job.folder_id == folder_id)
        .where(Job.type == "analyze")
        .where(Job.status.in_(["queued", "running"]))
        .order_by(Job.created_at.asc())
    ).first()


def _recompute_folder_status(db, folder, folder_id: uuid.UUID) -> None:
    """
    Recompute and persist ``folder.status`` based on the remaining jobs.

    Priority order (highest wins):
      running → queued → succeeded (done) → failed → pending

    If no jobs remain and there is no clip, the folder reverts to ``pending``.
    If no jobs remain but a clip exists the folder is also ``pending`` (ready
    for a new analysis run).
    """
    from sqlmodel import select

    from backend.app.models import Job

    remaining = db.exec(
        select(Job).where(Job.folder_id == folder_id)
    ).all()

    if not remaining:
        new_status = "pending"
    else:
        statuses = {j.status for j in remaining}
        if "running" in statuses:
            new_status = "running"
        elif "queued" in statuses:
            new_status = "queued"
        elif "succeeded" in statuses:
            new_status = "done"
        elif "failed" in statuses:
            new_status = "failed"
        else:
            new_status = "pending"

    folder.status = new_status
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)
    db.commit()


# ---------------------------------------------------------------------------
# POST /v1/folders  — create folder
# ---------------------------------------------------------------------------


@router.post("", status_code=201, dependencies=[Depends(require_auth)])
def create_folder(body: dict[str, Any] = None, db=Depends(_db_session)) -> JSONResponse:
    """Create a new empty folder.  ``title`` is optional."""
    from backend.app.models import Folder

    if body is None:
        body = {}
    title: str | None = body.get("title")
    folder = Folder(title=title)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    log_event(
        source="backend",
        level="info",
        event_type="folders.create",
        message=f"Folder created: {folder.id}",
        folder_id=str(folder.id),
        details_json={"title": title},
    )
    return JSONResponse(content=_folder_dict(folder), status_code=201)


# ---------------------------------------------------------------------------
# GET /v1/folders  — list folders
# ---------------------------------------------------------------------------


@router.get("", dependencies=[Depends(require_auth)])
def list_folders(db=Depends(_db_session)) -> JSONResponse:
    """Return all folders ordered by created_at descending."""
    from sqlmodel import select

    from backend.app.models import Folder

    folders = db.exec(select(Folder).order_by(Folder.created_at.desc())).all()
    return JSONResponse(content={"folders": [_folder_dict(f) for f in folders]})


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}  — get folder detail
# ---------------------------------------------------------------------------


@router.get("/{folder_id}", dependencies=[Depends(require_auth)])
def get_folder(folder_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return folder + latest job status + artifact list."""
    from sqlmodel import select

    from backend.app.models import Artifact, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    # Expire stalled jobs before building the response so callers always see
    # up-to-date status.
    _mark_stalled_jobs(db, fid)
    # Re-fetch folder in case watchdog updated its status.
    db.refresh(folder)

    jobs = db.exec(
        select(Job).where(Job.folder_id == fid).order_by(Job.created_at.desc())
    ).all()
    artifacts = db.exec(
        select(Artifact).where(Artifact.folder_id == fid).order_by(Artifact.created_at.desc())
    ).all()

    data = _folder_dict(folder)
    data["jobs"] = [_job_dict(j) for j in jobs]
    data["artifacts"] = [_artifact_dict(a) for a in artifacts]
    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# DELETE /v1/folders/{folder_id}  — delete folder
# ---------------------------------------------------------------------------


@router.delete("/{folder_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_folder(folder_id: str, db=Depends(_db_session)) -> None:
    """Delete folder and all cascade-linked rows."""
    import botocore.exceptions
    from sqlmodel import select

    from backend.app import storage
    from backend.app.models import Artifact, FolderMessage, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    artifacts = db.exec(select(Artifact).where(Artifact.folder_id == fid)).all()

    if storage.storage_available():
        for artifact in artifacts:
            try:
                storage.delete_object(artifact.object_key)
            except (RuntimeError, OSError, botocore.exceptions.BotoCoreError) as exc:
                logger.warning(
                    "Failed to delete storage object %s while deleting folder %s: %s",
                    artifact.object_key,
                    fid,
                    exc,
                )

    # Cascade deletes (for DBs/FK settings that don't auto-cascade).
    for model in (FolderMessage, Job):
        rows = db.exec(select(model).where(model.folder_id == fid)).all()
        for row in rows:
            db.delete(row)
    for artifact in artifacts:
        db.delete(artifact)

    db.delete(folder)
    db.commit()
    log_event(
        source="backend",
        level="info",
        event_type="folders.delete",
        message=f"Folder deleted: {fid}",
        folder_id=str(fid),
    )


# ---------------------------------------------------------------------------
# PATCH /v1/folders/{folder_id}  — rename folder
# ---------------------------------------------------------------------------

_TITLE_MAX_LEN = 120


@router.patch("/{folder_id}", dependencies=[Depends(require_auth)])
def patch_folder(
    folder_id: str, body: dict[str, Any] = None, db=Depends(_db_session)
) -> JSONResponse:
    """Rename a folder.

    Request body::

        {"title": "New name"}

    Rules:
    - ``title`` is required.
    - Whitespace is trimmed.
    - Blank title after trim → HTTP 422.
    - Title exceeding 120 characters → HTTP 422.
    """
    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    raw_title = str((body or {}).get("title", "")).strip()
    if not raw_title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    if len(raw_title) > _TITLE_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"title must not exceed {_TITLE_MAX_LEN} characters",
        )

    folder.title = raw_title
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    log_event(
        source="backend",
        level="info",
        event_type="folders.rename",
        message=f"Folder renamed: {folder.id}",
        folder_id=str(folder.id),
        details_json={"title": raw_title},
    )
    return JSONResponse(content=_folder_dict(folder))


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/clip  — upload clip
# ---------------------------------------------------------------------------


_MAX_CLIP_BYTES: int = int(os.environ.get("MAX_CLIP_BYTES", 200 * 1024 * 1024))  # 200 MB
_MAX_AUDIO_BYTES: int = int(os.environ.get("MAX_AUDIO_BYTES", 50 * 1024 * 1024))  # 50 MB
_UPLOAD_CHUNK_SIZE: int = 64 * 1024  # 64 KB
_ARTIFACT_DISPLAY_NAME_MAX_LEN: int = 120


@router.post("/{folder_id}/clip", status_code=202, dependencies=[Depends(require_auth)])
async def upload_clip(folder_id: str, clip: UploadFile, db=Depends(_db_session)) -> JSONResponse:
    """
    Accept a multipart clip upload, stream it directly to disk, store it in R2
    (when configured), create an Artifact record, and enqueue an ``analyze`` job.

    Returns 202 Accepted with the created job info.
    """
    from backend.app import storage, worker
    from backend.app.models import Artifact, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    original_filename = clip.filename or "clip.mp4"
    content_type = (clip.content_type or "").split(";")[0].strip() or "video/mp4"

    # MIME type validation — allow all video/* types plus octet-stream for
    # clients that don't set an explicit content-type.
    if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported clip type '{content_type}'. Expected a video/* MIME type.",
        )

    log_event(
        source="backend",
        level="info",
        event_type="clip.upload.started",
        message=f"Clip upload started for folder {fid}",
        folder_id=str(fid),
        details_json={"filename": original_filename},
    )

    # --- Stream file to a temporary location on disk -------------------------
    ext = os.path.splitext(original_filename)[1] or ".mp4"
    display_name, storage_filename = _build_upload_identity(db, fid, "clip", ext)
    tmp_path: str | None = None
    total_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await clip.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_CLIP_BYTES:
                    limit_mb = _MAX_CLIP_BYTES // (1024 * 1024)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Clip exceeds maximum allowed size of {limit_mb} MB",
                    )
                tmp.write(chunk)
    except HTTPException:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass
        logger.exception("Failed to stream clip upload to disk")
        raise HTTPException(status_code=500, detail="Failed to save uploaded clip") from exc

    # --- Storage (R2) -------------------------------------------------------
    clip_key: str | None = None
    artifact: Artifact | None = None
    try:
        if storage.storage_available():
            try:
                clip_key = storage.upload_file(
                    folder_id, storage_filename, tmp_path, content_type
                )
            except Exception as exc:
                logger.error("R2 upload failed: %s", exc)
                log_event(
                    source="storage",
                    level="error",
                    event_type="storage.put_object.failed",
                    message=f"R2 upload failed for folder {fid}: {exc}",
                    folder_id=str(fid),
                    error_type=type(exc).__name__,
                    error_detail=str(exc)[:2000],
                )
                raise HTTPException(
                    status_code=502, detail=f"Storage upload failed: {exc}"
                ) from exc

            artifact = Artifact(
                folder_id=fid,
                type="clip",
                object_key=clip_key,
                display_name=display_name,
            )
            db.add(artifact)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass

    # Update folder clip_object_key.
    folder.clip_object_key = clip_key
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)
    db.commit()

    if artifact is not None:
        db.refresh(artifact)

    # Deduplicate: if an analyze job is already queued/running, return it.
    _mark_stalled_jobs(db, fid)
    existing_job = _find_active_analyze_job(db, fid)
    if existing_job is not None:
        log_event(
            source="backend",
            level="info",
            event_type="jobs.deduped",
            message=(
                f"Duplicate analyze job suppressed for folder {fid} (clip upload); "
                f"returning existing job {existing_job.id} (status={existing_job.status})"
            ),
            folder_id=str(fid),
            job_id=str(existing_job.id),
        )
        return JSONResponse(
            content={
                "folder_id": folder_id,
                "job": _job_dict(existing_job),
                "clip_object_key": clip_key,
                "deduped": True,
            },
            status_code=202,
        )

    folder.status = "queued"
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)

    # Create job row.
    job = Job(
        folder_id=fid,
        type="analyze",
        analyze_clip_object_key=clip_key,
        source_artifact_id=artifact.id if artifact is not None else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Enqueue / run job.
    job_id_str = str(job.id)
    rq_id = worker.enqueue_job(job_id_str, "analyze")
    if rq_id:
        job.rq_job_id = rq_id
        db.add(job)
        db.commit()
        db.refresh(job)

    log_event(
        source="backend",
        level="info",
        event_type="clip.upload.succeeded",
        message=f"Clip upload succeeded for folder {fid}, job {job.id} enqueued",
        folder_id=str(fid),
        job_id=str(job.id),
        rq_job_id=rq_id,
        details_json={"clip_object_key": clip_key},
    )

    return JSONResponse(
        content={
            "folder_id": folder_id,
            "job": _job_dict(job),
            "clip_object_key": clip_key,
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/audio  — upload audio file
# ---------------------------------------------------------------------------


@router.post("/{folder_id}/audio", dependencies=[Depends(require_auth)])
async def upload_audio(
    folder_id: str,
    audio: UploadFile,
    db=Depends(_db_session),
) -> JSONResponse:
    """
    Accept a multipart audio upload, stream it directly to disk, store it in R2
    (when configured), create an Artifact record, and update the folder's
    audio_object_key.

    Returns 200 with the audio_object_key and artifact_id.
    """
    from backend.app import storage
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    content_type = (audio.content_type or "").split(";")[0].strip() or "audio/mp4"
    display_name, storage_filename = _build_upload_identity(db, fid, "audio_m4a", ".m4a")
    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported audio type '{content_type}'. Expected an audio/* MIME type.",
        )

    tmp_path: str | None = None
    total_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await audio.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_AUDIO_BYTES:
                    limit_mb = _MAX_AUDIO_BYTES // (1024 * 1024)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Audio file exceeds maximum allowed size of {limit_mb} MB",
                    )
                tmp.write(chunk)
    except HTTPException:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass
        logger.exception("Failed to stream audio upload to disk")
        raise HTTPException(status_code=500, detail="Failed to save uploaded audio") from exc

    try:
        object_key = storage.upload_file(
            folder_id, storage_filename, tmp_path, "audio/mp4"
        )
    except Exception as exc:
        logger.error("Audio R2 upload failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Storage upload failed: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:  # noqa: BLE001
            pass

    folder.audio_object_key = object_key
    if not folder.clip_object_key:
        folder.status = "audio_ready"
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)

    artifact = Artifact(
        folder_id=fid,
        type="audio_m4a",
        object_key=object_key,
        display_name=display_name,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    log_event(
        source="backend",
        level="info",
        event_type="audio.upload.succeeded",
        message=f"Audio upload succeeded for folder {fid}",
        folder_id=str(fid),
        details_json={"audio_object_key": object_key},
    )

    return JSONResponse(
        content={
            "audio_object_key": object_key,
            "artifact_id": str(artifact.id),
        }
    )


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/repo  — upload repository ZIP
# ---------------------------------------------------------------------------


@router.post("/{folder_id}/repo", status_code=202, dependencies=[Depends(require_auth)])
async def upload_repo(
    folder_id: str,
    repo: UploadFile,
    db=Depends(_db_session),
) -> JSONResponse:
    """
    Accept a .zip repository upload, store it in R2, and create an Artifact
    record (type='repo_zip').

    Returns 202 Accepted with the created artifact info. Analysis is triggered
    separately once the user marks the upload for analysis.
    """
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    if not repo_chunking.is_repo_zip_upload(repo.filename or "repo.zip", repo.content_type):
        raise HTTPException(status_code=415, detail="Repo upload must be a .zip file")

    max_repo_zip_bytes = int(os.environ.get("MAX_REPO_ZIP_BYTES", 200 * 1024 * 1024))
    _CHUNK_SIZE = 1024 * 1024  # 1 MB

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            total_bytes = 0
            while True:
                chunk = await repo.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_repo_zip_bytes:
                    limit_mb = max_repo_zip_bytes // (1024 * 1024)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Repo ZIP exceeds maximum allowed size of {limit_mb} MB",
                    )
                tmp.write(chunk)

        artifact, key = _persist_repo_upload(db, fid, tmp_path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass

    log_event(
        source="backend",
        level="info",
        event_type="repo.upload.succeeded",
        message=f"Repo upload succeeded for folder {fid}, artifact {artifact.id} stored",
        folder_id=str(fid),
        artifact_id=str(artifact.id),
        details_json={"repo_object_key": key},
    )

    return JSONResponse(
        content={
            "folder_id": folder_id,
            "artifact": _artifact_dict(artifact),
            "repo_object_key": key,
            "chunking": {
                "enabled": False,
                "chunk_size_bytes": 0,
                "total_chunks": 1,
            },
        },
        status_code=202,
    )


@router.post(
    "/{folder_id}/repo/chunks/start",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def start_repo_chunk_upload(
    folder_id: str,
    body: dict[str, Any],
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    file_name = str(body.get("file_name", "repo.zip")).strip() or "repo.zip"
    content_type = str(body.get("content_type", "application/zip")).strip() or "application/zip"
    total_bytes = int(body.get("total_bytes", 0))
    requested_chunk_size = int(
        body.get("chunk_size_bytes", repo_chunking.default_chunk_size_bytes())
    )
    if not repo_chunking.is_repo_zip_upload(file_name, content_type):
        raise HTTPException(status_code=415, detail="Repo upload must be a .zip file")

    max_repo_zip_bytes = int(os.environ.get("MAX_REPO_ZIP_BYTES", 200 * 1024 * 1024))
    if total_bytes > max_repo_zip_bytes:
        limit_mb = max_repo_zip_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Repo ZIP exceeds maximum allowed size of {limit_mb} MB",
        )

    manifest = repo_chunking.start_upload(
        folder_id=folder_id,
        file_name=file_name,
        content_type=content_type,
        total_bytes=total_bytes,
        chunk_size_bytes=requested_chunk_size,
    )
    retry_count = int(os.environ.get("REPO_ZIP_UPLOAD_RETRY_COUNT", "3"))
    return JSONResponse(
        content={
            "upload_id": manifest["upload_id"],
            "chunk_size_bytes": manifest["chunk_size_bytes"],
            "total_bytes": manifest["total_bytes"],
            "total_chunks": manifest["total_chunks"],
            "retry_count": max(0, retry_count),
        },
        status_code=201,
    )


@router.post("/{folder_id}/repo/chunks", status_code=202, dependencies=[Depends(require_auth)])
async def upload_repo_chunk(
    folder_id: str,
    chunk: UploadFile,
    x_upload_id: str = Header(..., alias="X-Upload-Id"),
    x_chunk_index: int = Header(..., alias="X-Chunk-Index"),
    x_total_chunks: int = Header(..., alias="X-Total-Chunks"),
    x_chunk_size: int = Header(..., alias="X-Chunk-Size"),
    x_total_bytes: int = Header(..., alias="X-Total-Bytes"),
    x_file_name: str = Header(default="repo.zip", alias="X-File-Name"),
    content_range: str | None = Header(default=None, alias="Content-Range"),
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    max_repo_zip_bytes = int(os.environ.get("MAX_REPO_ZIP_BYTES", 200 * 1024 * 1024))
    if x_total_bytes > max_repo_zip_bytes:
        limit_mb = max_repo_zip_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Repo ZIP exceeds maximum allowed size of {limit_mb} MB",
        )

    content_type = (chunk.content_type or "application/zip").lower()
    if not repo_chunking.is_repo_zip_upload(x_file_name, content_type):
        raise HTTPException(status_code=415, detail="Repo upload must be a .zip file")

    chunk_bytes = await chunk.read()
    if content_range is not None:
        range_start, range_end, range_total = repo_chunking.parse_content_range(content_range)
        expected_start = x_chunk_index * x_chunk_size
        expected_end = expected_start + len(chunk_bytes) - 1
        if (
            range_total != x_total_bytes
            or range_start != expected_start
            or range_end != expected_end
        ):
            raise HTTPException(
                status_code=400,
                detail="Content-Range does not match chunk metadata",
            )

    manifest = repo_chunking.write_chunk(
        x_upload_id,
        folder_id=folder_id,
        file_name=x_file_name,
        content_type=content_type,
        chunk_index=x_chunk_index,
        total_chunks=x_total_chunks,
        chunk_size_bytes=x_chunk_size,
        total_bytes=x_total_bytes,
        data=chunk_bytes,
    )
    return JSONResponse(
        content={
            "upload_id": x_upload_id,
            "chunk_index": x_chunk_index,
            "chunks_received": manifest["chunks_received"],
            "total_chunks": x_total_chunks,
            "total_bytes": x_total_bytes,
            "complete": manifest["complete"],
        },
        status_code=202,
    )


@router.delete(
    "/{folder_id}/repo/chunks/{upload_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def cancel_repo_chunk_upload(
    folder_id: str,
    upload_id: str,
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    manifest = repo_chunking.load_manifest(upload_id)
    if manifest.get("folder_id") != folder_id:
        raise HTTPException(status_code=404, detail="Upload not found for folder")

    repo_chunking.cleanup(upload_id)
    return JSONResponse(content={"upload_id": upload_id, "status": "cancelled"})


@router.put(
    "/{folder_id}/repo/chunks/{upload_id}/finalize",
    status_code=202,
    dependencies=[Depends(require_auth)],
)
async def finalize_repo_chunk_upload(
    folder_id: str,
    upload_id: str,
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    manifest = repo_chunking.load_manifest(upload_id)
    if manifest.get("folder_id") != folder_id:
        raise HTTPException(status_code=404, detail="Upload not found for folder")

    max_repo_zip_bytes = int(os.environ.get("MAX_REPO_ZIP_BYTES", 200 * 1024 * 1024))
    tmp_path: str | None = None
    try:
        manifest, tmp_path = repo_chunking.merge_chunks(upload_id, max_repo_zip_bytes)
        artifact, key = _persist_repo_upload(db, fid, tmp_path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass

    repo_chunking.cleanup(upload_id)
    log_event(
        source="backend",
        level="info",
        event_type="repo.upload.chunked.finalized",
        message=f"Chunked repo upload finalized for folder {fid}, artifact {artifact.id} stored",
        folder_id=str(fid),
        artifact_id=str(artifact.id),
        details_json={
            "repo_object_key": key,
            "upload_id": upload_id,
            "total_chunks": manifest["total_chunks"],
            "chunk_size_bytes": manifest["chunk_size_bytes"],
            "total_bytes": manifest["total_bytes"],
        },
    )
    return JSONResponse(
        content={
            "folder_id": folder_id,
            "artifact": _artifact_dict(artifact),
            "repo_object_key": key,
            "chunking": {
                "enabled": True,
                "upload_id": upload_id,
                "chunk_size_bytes": manifest["chunk_size_bytes"],
                "total_chunks": manifest["total_chunks"],
                "total_bytes": manifest["total_bytes"],
            },
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Chunked folder archive uploads
# ---------------------------------------------------------------------------


@router.post(
    "/{folder_id}/folder-uploads/start",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def start_folder_upload(
    folder_id: str,
    body: dict[str, Any],
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    folder_name = str(body.get("folder_name", "")).strip() or "folder-upload"
    total_bytes = int(body.get("total_bytes", 0))
    total_files = int(body.get("total_files", 0))
    requested_chunk_size = int(
        body.get("chunk_size_bytes", repo_chunking.default_chunk_size_bytes())
    )
    if total_files < 1:
        raise HTTPException(status_code=400, detail="total_files must be at least 1")

    max_folder_upload_bytes = int(os.environ.get("MAX_FOLDER_UPLOAD_BYTES", 500 * 1024 * 1024))
    if total_bytes > max_folder_upload_bytes:
        limit_mb = max_folder_upload_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Folder upload exceeds maximum allowed size of {limit_mb} MB",
        )

    manifest = repo_chunking.start_upload(
        folder_id=folder_id,
        file_name=f"{folder_name}.zip",
        content_type="application/zip",
        total_bytes=total_bytes,
        chunk_size_bytes=requested_chunk_size,
    )
    manifest["folder_name"] = folder_name
    manifest["total_files"] = total_files
    manifest["structure"] = body.get("structure")
    repo_chunking.save_manifest(manifest["upload_id"], manifest)

    retry_count = int(os.environ.get("REPO_ZIP_UPLOAD_RETRY_COUNT", "3"))
    return JSONResponse(
        content={
            "upload_id": manifest["upload_id"],
            "chunk_size_bytes": manifest["chunk_size_bytes"],
            "total_chunks": manifest["total_chunks"],
            "total_bytes": manifest["total_bytes"],
            "total_files": total_files,
            "retry_count": max(0, retry_count),
        },
        status_code=201,
    )


@router.post(
    "/{folder_id}/folder-uploads/chunks",
    status_code=202,
    dependencies=[Depends(require_auth)],
)
async def upload_folder_chunk(
    folder_id: str,
    chunk: UploadFile,
    x_upload_id: str = Header(..., alias="X-Upload-Id"),
    content_range: str = Header(..., alias="Content-Range"),
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    manifest = repo_chunking.load_manifest(x_upload_id)
    if manifest.get("folder_id") != folder_id:
        raise HTTPException(status_code=404, detail="Upload not found for folder")

    range_start, range_end, range_total = repo_chunking.parse_content_range(content_range)
    if range_total != int(manifest["total_bytes"]):
        raise HTTPException(status_code=400, detail="Content-Range total does not match upload")

    chunk_bytes = await chunk.read()
    chunk_size_bytes = int(manifest["chunk_size_bytes"])
    chunk_index = range_start // chunk_size_bytes
    expected_end = range_start + len(chunk_bytes) - 1
    if range_start % chunk_size_bytes != 0 or range_end != expected_end:
        raise HTTPException(status_code=400, detail="Content-Range does not match chunk payload")

    updated_manifest = repo_chunking.write_chunk(
        x_upload_id,
        folder_id=folder_id,
        file_name=str(manifest["file_name"]),
        content_type="application/zip",
        chunk_index=chunk_index,
        total_chunks=int(manifest["total_chunks"]),
        chunk_size_bytes=chunk_size_bytes,
        total_bytes=int(manifest["total_bytes"]),
        data=chunk_bytes,
    )
    return JSONResponse(
        content={
            "upload_id": x_upload_id,
            "chunk_index": chunk_index,
            "chunks_received": updated_manifest["chunks_received"],
            "total_chunks": updated_manifest["total_chunks"],
            "complete": updated_manifest["complete"],
        },
        status_code=202,
    )


@router.put(
    "/{folder_id}/folder-uploads/{upload_id}/finalize",
    status_code=202,
    dependencies=[Depends(require_auth)],
)
async def finalize_folder_upload(
    folder_id: str,
    upload_id: str,
    db=Depends(_db_session),
) -> JSONResponse:
    from backend.app import repo_chunking, storage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    if not storage.storage_available():
        raise HTTPException(status_code=502, detail="Storage not configured")

    manifest = repo_chunking.load_manifest(upload_id)
    if manifest.get("folder_id") != folder_id:
        raise HTTPException(status_code=404, detail="Upload not found for folder")

    max_folder_upload_bytes = int(os.environ.get("MAX_FOLDER_UPLOAD_BYTES", 500 * 1024 * 1024))
    tmp_path: str | None = None
    try:
        manifest, tmp_path = repo_chunking.merge_chunks(upload_id, max_folder_upload_bytes)
        artifact, key = _persist_folder_upload(db, fid, tmp_path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass

    repo_chunking.cleanup(upload_id)
    log_event(
        source="backend",
        level="info",
        event_type="folder.upload.finalized",
        message=f"Folder upload finalized for folder {fid}, artifact {artifact.id} stored",
        folder_id=str(fid),
        artifact_id=str(artifact.id),
        details_json={
            "folder_upload_object_key": key,
            "upload_id": upload_id,
            "total_chunks": manifest["total_chunks"],
            "total_bytes": manifest["total_bytes"],
            "total_files": manifest.get("total_files"),
        },
    )
    return JSONResponse(
        content={
            "folder_id": folder_id,
            "artifact": _artifact_dict(artifact),
            "folder_upload_object_key": key,
            "chunking": {
                "enabled": True,
                "upload_id": upload_id,
                "chunk_size_bytes": manifest["chunk_size_bytes"],
                "total_chunks": manifest["total_chunks"],
                "total_bytes": manifest["total_bytes"],
                "total_files": manifest.get("total_files"),
            },
        },
        status_code=202,
    )


@router.delete(
    "/{folder_id}/folder-uploads/{upload_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def cancel_folder_upload(folder_id: str, upload_id: str, db=Depends(_db_session)) -> JSONResponse:
    from backend.app import repo_chunking

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    manifest = repo_chunking.load_manifest(upload_id)
    if manifest.get("folder_id") != folder_id:
        raise HTTPException(status_code=404, detail="Upload not found for folder")

    repo_chunking.cleanup(upload_id)
    return JSONResponse(content={"upload_id": upload_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/artifacts/{artifact_id}  — download/presign
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/artifacts/{artifact_id}", dependencies=[Depends(require_auth)])
def get_artifact(
    folder_id: str, artifact_id: str, db=Depends(_db_session)
) -> JSONResponse:
    """
    Return a presigned GET URL for the artifact, or a download redirect.

    When R2 is not configured, returns the object_key in the response for
    debugging.
    """
    from backend.app import storage
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    aid = _parse_uuid(artifact_id, "artifact_id")

    _folder_or_404(db, fid)

    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.folder_id != fid:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if storage.storage_available():
        try:
            url = storage.get_presigned_url(artifact.object_key)
            return RedirectResponse(url=url, status_code=302)
        except Exception as exc:
            logger.error("Presign failed: %s", exc)
            raise HTTPException(status_code=502, detail="Could not generate download URL") from exc

    # R2 not configured – return metadata only.
    return JSONResponse(
        content={
            **_artifact_dict(artifact),
            "download_url": None,
            "note": "R2 storage not configured; object_key provided for reference.",
        }
    )


@router.get("/{folder_id}/artifacts/{artifact_id}/url", dependencies=[Depends(require_auth)])
def get_artifact_url(
    folder_id: str, artifact_id: str, db=Depends(_db_session)
) -> JSONResponse:
    """Return a JSON payload containing the artifact's download URL."""
    from backend.app import storage
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    aid = _parse_uuid(artifact_id, "artifact_id")

    _folder_or_404(db, fid)

    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.folder_id != fid:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if not storage.storage_available():
        return JSONResponse(content={"url": None, "artifact": _artifact_dict(artifact)})

    try:
        url = storage.get_presigned_url(artifact.object_key)
    except Exception as exc:
        logger.error("Presign failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not generate download URL") from exc

    return JSONResponse(content={"url": url, "artifact": _artifact_dict(artifact)})


@router.post(
    "/{folder_id}/artifacts/{artifact_id}/analyze",
    status_code=202,
    dependencies=[Depends(require_auth)],
)
def analyze_artifact(folder_id: str, artifact_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Mark an uploaded artifact for analysis and enqueue the matching job."""
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    aid = _parse_uuid(artifact_id, "artifact_id")
    _folder_or_404(db, fid)

    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.folder_id != fid:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.type != "repo_zip":
        raise HTTPException(status_code=400, detail="Only repo ZIP uploads can be analyzed here")

    job, deduped = _enqueue_repo_analysis_job(db, fid, artifact.id)
    log_event(
        source="backend",
        level="info",
        event_type="repo.analysis.enqueued" if not deduped else "repo.analysis.deduped",
        message=(
            f"Repo analysis {'deduped' if deduped else 'enqueued'} for artifact {artifact.id}"
        ),
        folder_id=str(fid),
        job_id=str(job.id),
        artifact_id=str(artifact.id),
    )
    return JSONResponse(content={"job": _job_dict(job), "deduped": deduped}, status_code=202)


@router.delete(
    "/{folder_id}/artifacts/{artifact_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def delete_artifact(
    folder_id: str,
    artifact_id: str,
    db=Depends(_db_session),
) -> JSONResponse:
    """Delete an artifact and any completed derived artifacts anchored to it."""
    import botocore.exceptions
    from sqlmodel import select

    from backend.app import storage
    from backend.app.models import Artifact, Job

    fid = _parse_uuid(folder_id, "folder_id")
    aid = _parse_uuid(artifact_id, "artifact_id")
    _folder_or_404(db, fid)

    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.folder_id != fid:
        raise HTTPException(status_code=404, detail="Artifact not found")

    linked_jobs = db.exec(
        select(Job)
        .where(Job.folder_id == fid)
        .where(Job.source_artifact_id == artifact.id)
        .order_by(Job.created_at.desc())
    ).all()
    active_job = next((job for job in linked_jobs if job.status in ("queued", "running")), None)
    if active_job is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete artifact while linked job '{active_job.type}' is "
                f"{active_job.status}. Cancel it first."
            ),
        )

    derived_artifacts = db.exec(
        select(Artifact)
        .where(Artifact.folder_id == fid)
        .where(Artifact.job_id.in_([job.id for job in linked_jobs] or [uuid.uuid4()]))
    ).all()

    to_delete = [artifact, *derived_artifacts]
    deleted_artifact_ids: list[str] = []
    deleted_job_ids: list[str] = []

    if storage.storage_available():
        for row in to_delete:
            try:
                storage.delete_object(row.object_key)
            except (RuntimeError, OSError, botocore.exceptions.BotoCoreError):
                logger.warning("Failed to delete storage object %s", row.object_key)

    for row in derived_artifacts:
        deleted_artifact_ids.append(str(row.id))
        db.delete(row)
    for job in linked_jobs:
        deleted_job_ids.append(str(job.id))
        db.delete(job)

    deleted_artifact_ids.append(str(artifact.id))
    db.delete(artifact)
    db.commit()

    log_event(
        source="backend",
        level="info",
        event_type="artifacts.delete",
        message=f"Artifact deleted: {artifact.id}",
        folder_id=str(fid),
        artifact_id=str(artifact.id),
        details_json={
            "deleted_artifact_ids": deleted_artifact_ids,
            "deleted_job_ids": deleted_job_ids,
        },
    )
    return JSONResponse(
        content={
            "deleted_artifact_ids": deleted_artifact_ids,
            "deleted_job_ids": deleted_job_ids,
        }
    )


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/messages  — folder chat
# ---------------------------------------------------------------------------

_FOLDER_CHAT_SYSTEM_PROMPT = (
    "You are UI Blueprint Assistant. "
    "You help users understand their recorded screen clips and derived blueprints. "
    "You can suggest analysis steps, explain blueprint output, and answer questions about "
    "UI structure. Be concise, practical, and friendly. "
    "The folder context below shows the current processing status, jobs, and artifacts. "
    "When the user asks to analyze, compile, or check status, confirm the action taken and "
    "summarize the current state."
)

_FOLDER_TOOLS_AVAILABLE = [
    "folders.analyze",
    "folders.status",
    "folders.compile",
    "folders.list_artifacts",
]

# Intent detection patterns.
_RE_ANALYZE = re.compile(r"\b(analy[sz]e|extract|run\s+analy|start\s+analy)\b", re.I)
_RE_COMPILE = re.compile(
    r"\b(compile|generate\s+blueprint|build\s+blueprint|create\s+blueprint|run\s+blueprint)\b",
    re.I,
)
_RE_STATUS = re.compile(
    r"\b(status|progress|how\s+(is|are|long)|done\??|finished|complete\??)\b",
    re.I,
)


def _detect_intent(message: str) -> str | None:
    """Return 'analyze', 'blueprint', 'status', or None."""
    if _RE_ANALYZE.search(message):
        return "analyze"
    if _RE_COMPILE.search(message):
        return "blueprint"
    if _RE_STATUS.search(message):
        return "status"
    return None


def _build_folder_context(folder, jobs: list, artifacts: list) -> str:
    """Build a plain-text context string describing the folder's current state."""
    lines = [f"Folder status: {folder.status}"]

    if jobs:
        lines.append("Jobs (most recent first):")
        for job in jobs[:5]:
            line = f"  - {job.type}: {job.status}"
            if job.progress:
                line += f" ({job.progress}%)"
            if job.error:
                line += f" [error: {job.error[:80]}]"
            lines.append(line)
    else:
        lines.append("Jobs: none yet.")

    if artifacts:
        lines.append("Artifacts:")
        for artifact in artifacts[:5]:
            lines.append(f"  - {artifact.type}")
    else:
        lines.append("Artifacts: none yet.")

    return "\n".join(lines)


def _call_openai_responses_api(
    message: str,
    history: list,
    api_key: str,
    folder_context: str = "",
) -> str:
    """Call the OpenAI Responses API with conversation history and folder context."""
    from openai import OpenAI

    model = os.environ.get("OPENAI_MODEL_CHAT", "gpt-4.1-mini")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30"))

    client = OpenAI(api_key=api_key, base_url=f"{base_url}/v1", timeout=timeout)

    instructions = append_prompt_injection_defense(_FOLDER_CHAT_SYSTEM_PROMPT)
    if folder_context:
        instructions += f"\n\n--- Current folder state ---\n{folder_context}"

    input_messages = []
    total_history = len(history)
    for idx, msg in enumerate(history, start=1):
        if msg.role in ("user", "assistant"):
            content = (
                format_untrusted_text(
                    f"Quoted prior user message ({idx} of {total_history})",
                    msg.content,
                )
                if msg.role == "user"
                else msg.content
            )
            input_messages.append({"role": msg.role, "content": content})
    input_messages.append(
        {"role": "user", "content": format_untrusted_text("Latest user message", message)}
    )

    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=input_messages,
        max_output_tokens=400,
    )
    return response.output_text


@router.post("/{folder_id}/messages", status_code=201, dependencies=[Depends(require_auth)])
def post_message(
    folder_id: str, body: dict[str, Any], db=Depends(_db_session)
) -> JSONResponse:
    """
    Send a user message to the folder's chat.

    Requires ``OPENAI_API_KEY`` to be set; returns HTTP 503 otherwise.

    Intent routing: if the message asks to analyze/compile a clip or check status,
    the appropriate RQ job is enqueued automatically and its details are included
    in the response alongside the AI reply.

    Request body::

        {"message": "analyze this clip"}

    Response::

        {
          "user_message": {...},
          "assistant_message": {...},
          "tools_available": [...],
          "enqueued_job": {...}   // present only when a job was enqueued
        }
    """
    from sqlmodel import select

    from backend.app.models import Artifact, FolderMessage, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    try:
        request = FolderChatPostRequest.model_validate(body or {})
    except ValidationError as exc:
        if any(error["loc"] == ("message",) for error in exc.errors()):
            raise HTTPException(
                status_code=400,
                detail="message is required and must not be empty.",
            ) from None
        raise HTTPException(status_code=422, detail=exc.errors()) from None

    content = request.message

    # Require OpenAI API key — no stub fallback.
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "OPENAI_API_KEY is not configured on this server. "
                "Folder chat requires OpenAI to be enabled."
            ),
        )

    # Persist user message.
    user_msg = FolderMessage(folder_id=fid, role="user", content=content)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # Build conversation history (last 10 messages, excluding the one just saved).
    history = db.exec(
        select(FolderMessage)
        .where(FolderMessage.folder_id == fid)
        .order_by(FolderMessage.created_at.desc())
        .limit(10)
    ).all()
    history = list(reversed(history))

    # ---------- Intent routing -------------------------------------------
    intent = _detect_intent(content)
    enqueued_job = None

    if intent in ("analyze", "blueprint"):
        from backend.app import worker

        # Deduplicate analyze jobs: reuse existing queued/running job.
        if intent == "analyze":
            _mark_stalled_jobs(db, fid)
            existing_job = _find_active_analyze_job(db, fid)
            if existing_job is not None:
                log_event(
                    source="backend",
                    level="info",
                    event_type="jobs.deduped",
                    message=(
                        f"Duplicate analyze job suppressed for folder {fid} (chat message); "
                        f"returning existing job {existing_job.id} (status={existing_job.status})"
                    ),
                    folder_id=str(fid),
                    job_id=str(existing_job.id),
                )
                enqueued_job = existing_job
            else:
                new_job = Job(folder_id=fid, type=intent)
                db.add(new_job)
                db.commit()
                db.refresh(new_job)
                rq_id = worker.enqueue_job(str(new_job.id), intent)
                if rq_id:
                    new_job.rq_job_id = rq_id
                    db.add(new_job)
                    db.commit()
                    db.refresh(new_job)
                enqueued_job = new_job
        else:
            new_job = Job(folder_id=fid, type=intent)
            db.add(new_job)
            db.commit()
            db.refresh(new_job)
            rq_id = worker.enqueue_job(str(new_job.id), intent)
            if rq_id:
                new_job.rq_job_id = rq_id
                db.add(new_job)
                db.commit()
                db.refresh(new_job)
            enqueued_job = new_job
    # -----------------------------------------------------------------------

    # Build folder context (refresh job/artifact lists after possible enqueue).
    jobs = db.exec(
        select(Job).where(Job.folder_id == fid).order_by(Job.created_at.desc()).limit(10)
    ).all()
    artifacts = db.exec(
        select(Artifact).where(Artifact.folder_id == fid).order_by(Artifact.created_at.desc())
    ).all()

    folder_context = _build_folder_context(folder, jobs, artifacts)
    if enqueued_job:
        folder_context += (
            f"\n\nAction taken: enqueued a new {enqueued_job.type} job "
            f"(id={enqueued_job.id}, status=queued)."
        )

    # Call OpenAI Responses API.  history[-1] is the user message we just
    # saved, so we pass history[:-1] (prior conversation) to avoid duplicating
    # it — _call_openai_responses_api appends `content` as the last message.
    reply_text = _call_openai_responses_api(content, history[:-1], openai_key, folder_context)

    # Persist assistant reply.
    assistant_msg = FolderMessage(folder_id=fid, role="assistant", content=reply_text)
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    return _json_response(
        FolderChatPostResponse(
            user_message=FolderChatMessageResponse(**_message_dict(user_msg)),
            assistant_message=FolderChatMessageResponse(**_message_dict(assistant_msg)),
            tools_available=_FOLDER_TOOLS_AVAILABLE,
            enqueued_job=_job_dict(enqueued_job) if enqueued_job else None,
        ),
        status_code=201,
    )


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/messages  — list messages
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/messages", dependencies=[Depends(require_auth)])
def list_messages(folder_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return all chat messages for the folder in chronological order."""
    from sqlmodel import select

    from backend.app.models import FolderMessage

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    messages = db.exec(
        select(FolderMessage)
        .where(FolderMessage.folder_id == fid)
        .order_by(FolderMessage.created_at.asc())
    ).all()
    return _json_response(
        FolderChatListResponse(
            messages=[FolderChatMessageResponse(**_message_dict(message)) for message in messages]
        )
    )


# ---------------------------------------------------------------------------
# Stalled-job watchdog
# ---------------------------------------------------------------------------

_STALLED_JOB_TYPES = {"analyze", "analyze_repo", "blueprint"}

# Maximum seconds a job may remain in "running" state before being declared
# stalled.  Configurable via MAX_JOB_RUNTIME_SECONDS env var (default 900 = 15 min).
_MAX_JOB_RUNTIME_SECONDS = int(os.environ.get("MAX_JOB_RUNTIME_SECONDS", "900"))


def _mark_stalled_jobs(db, folder_id: uuid.UUID) -> None:
    """
    Inspect running jobs for *folder_id* and fail any that have exceeded
    ``MAX_JOB_RUNTIME_SECONDS`` since their ``updated_at`` timestamp.

    Called lazily on every folder/job read so that stale jobs are surfaced
    even when the worker is restarted and never writes a failure record.
    """
    from sqlmodel import select

    from backend.app.models import Job

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_MAX_JOB_RUNTIME_SECONDS)

    stalled = db.exec(
        select(Job)
        .where(Job.folder_id == folder_id)
        .where(Job.status == "running")
        .where(Job.type.in_(list(_STALLED_JOB_TYPES)))
        .where(Job.updated_at < cutoff)
    ).all()

    for job in stalled:
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.updated_at = now
        job.error = (
            f"Job exceeded maximum runtime of {_MAX_JOB_RUNTIME_SECONDS}s "
            f"(worker likely restarted). Marked stalled at {now.isoformat()}."
        )
        db.add(job)
        log_event(
            source="backend",
            level="warning",
            event_type="jobs.stalled",
            message=(
                f"Job {job.id} ({job.type}) marked stalled after "
                f"{_MAX_JOB_RUNTIME_SECONDS}s"
            ),
            folder_id=str(folder_id),
            job_id=str(job.id),
            details_json={
                "error_type": "stalled",
                "max_runtime_seconds": _MAX_JOB_RUNTIME_SECONDS,
            },
        )

    if stalled:
        db.commit()
        # Sync folder status if no other running jobs remain.
        from backend.app.models import Folder

        still_running = db.exec(
            select(Job)
            .where(Job.folder_id == folder_id)
            .where(Job.status == "running")
        ).first()
        if still_running is None:
            folder = db.get(Folder, folder_id)
            if folder is not None and folder.status == "running":
                folder.status = "failed"
                folder.updated_at = datetime.now(timezone.utc)
                db.add(folder)
                db.commit()


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/jobs  — enqueue job
# ---------------------------------------------------------------------------


@router.post("/{folder_id}/jobs", status_code=202, dependencies=[Depends(require_auth)])
def create_job(folder_id: str, body: dict[str, Any], db=Depends(_db_session)) -> JSONResponse:
    """
    Enqueue a background job for the folder.

    Request body::

        {"type": "analyze"}
        {"type": "analyze", "options": {
            "additional_analysis": {"enabled": true, "keyframes": true}}}
        {"type": "blueprint"}

    **options** (analyze / analyze_optional only, optional):
      - ``additional_analysis.enabled`` (bool, default ``false``): master switch.
      - ``additional_analysis.keyframes`` (bool): per-segment keyframes.json.
      - ``additional_analysis.ocr`` (bool): per-segment ocr.json.
      - ``additional_analysis.transcript`` (bool): per-segment transcript.json.
      - ``additional_analysis.events`` (bool): per-segment events.json.
      - ``additional_analysis.segment_summaries`` (bool): per-segment summary.json.

    Omitting ``options`` is equivalent to ``{"additional_analysis": {"enabled": false}}``.
    All unknown keys inside ``options`` are rejected with HTTP 400.

    Idempotency: if an ``analyze`` job for this folder is already ``queued``
    or ``running``, the existing job is returned (HTTP 202) without creating a
    duplicate.  A ``jobs.deduped`` ops event is recorded.
    """
    from sqlmodel import select

    from backend.app import worker
    from backend.app.models import Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    job_type = str(body.get("type", "")).strip()
    if job_type not in ("analyze", "analyze_optional", "blueprint"):
        raise HTTPException(
            status_code=400,
            detail="type must be 'analyze', 'analyze_optional', or 'blueprint'",
        )

    # Validate and normalise the optional per-job options block.
    analyze_options: dict[str, Any] | None = None
    if job_type in ("analyze", "analyze_optional") and "options" in body:
        raw_options = body["options"]
        if not isinstance(raw_options, dict):
            raise HTTPException(status_code=400, detail="options must be a JSON object")

        # Only known top-level keys are allowed.
        _OPTIONS_ALLOWED_KEYS = {"additional_analysis"}
        unknown_top = set(raw_options) - _OPTIONS_ALLOWED_KEYS
        if unknown_top:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown options keys: {sorted(unknown_top)}. "
                f"Allowed: {sorted(_OPTIONS_ALLOWED_KEYS)}",
            )

        aa = raw_options.get("additional_analysis", {})
        if not isinstance(aa, dict):
            raise HTTPException(
                status_code=400, detail="options.additional_analysis must be a JSON object"
            )
        _AA_ALLOWED_KEYS = {
            "enabled", "keyframes", "ocr", "transcript", "events", "segment_summaries"
        }
        unknown_aa = set(aa) - _AA_ALLOWED_KEYS
        if unknown_aa:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown additional_analysis keys: {sorted(unknown_aa)}. "
                f"Allowed: {sorted(_AA_ALLOWED_KEYS)}",
            )

        # Build canonical options dict (only known fields; booleans).
        analyze_options = {
            "additional_analysis": {
                "enabled": bool(aa.get("enabled", False)),
                "keyframes": bool(aa.get("keyframes", False)),
                "ocr": bool(aa.get("ocr", False)),
                "transcript": bool(aa.get("transcript", False)),
                "events": bool(aa.get("events", False)),
                "segment_summaries": bool(aa.get("segment_summaries", False)),
            }
        }

    # Run watchdog before the dedupe check so stalled jobs are cleared first.
    _mark_stalled_jobs(db, fid)

    # Idempotency: deduplicate active analyze/analyze_optional jobs.
    if job_type in ("analyze", "analyze_optional"):
        existing = db.exec(
            select(Job)
            .where(Job.folder_id == fid)
            .where(Job.type == job_type)
            .where(Job.status.in_(["queued", "running"]))
            .order_by(Job.created_at.asc())
        ).first()
        if existing is not None:
            log_event(
                source="backend",
                level="info",
                event_type="jobs.deduped",
                message=(
                    f"Duplicate analyze job suppressed for folder {fid}; "
                    f"returning existing job {existing.id} (status={existing.status})"
                ),
                folder_id=str(fid),
                job_id=str(existing.id),
            )
            return JSONResponse(content={"job": _job_dict(existing)}, status_code=202)

    source_artifact = _artifact_for_object_key(db, fid, folder.clip_object_key or "")

    job = Job(
        folder_id=fid,
        type=job_type,
        analyze_options=analyze_options,
        analyze_clip_object_key=folder.clip_object_key,
        source_artifact_id=source_artifact.id if source_artifact is not None else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    job_id_str = str(job.id)
    rq_id = worker.enqueue_job(job_id_str, job_type)
    if rq_id:
        job.rq_job_id = rq_id
        db.add(job)
        db.commit()
        db.refresh(job)

    log_event(
        source="backend",
        level="info",
        event_type="jobs.enqueue",
        message=f"Job enqueued: {job_type} for folder {fid}",
        folder_id=str(fid),
        job_id=str(job.id),
        rq_job_id=rq_id,
        details_json={"job_type": job_type, "options": analyze_options},
    )
    return JSONResponse(content={"job": _job_dict(job)}, status_code=202)


@router.patch("/{folder_id}/artifacts/{artifact_id}", dependencies=[Depends(require_auth)])
def rename_artifact(
    folder_id: str,
    artifact_id: str,
    body: dict[str, Any] | None = None,
    db=Depends(_db_session),
) -> JSONResponse:
    """Rename an artifact by updating its display_name."""
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    aid = _parse_uuid(artifact_id, "artifact_id")
    _folder_or_404(db, fid)

    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.folder_id != fid:
        raise HTTPException(status_code=404, detail="Artifact not found")

    new_display_name = str((body or {}).get("display_name", "")).strip()
    if not new_display_name:
        raise HTTPException(status_code=422, detail="display_name must not be blank or whitespace")
    if len(new_display_name) > _ARTIFACT_DISPLAY_NAME_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"display_name must not exceed {_ARTIFACT_DISPLAY_NAME_MAX_LEN} characters",
        )

    artifact.display_name = new_display_name
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    log_event(
        source="backend",
        level="info",
        event_type="artifacts.rename",
        message=f"Artifact renamed: {artifact.id}",
        folder_id=str(fid),
        artifact_id=str(artifact.id),
        details_json={"display_name": new_display_name, "type": artifact.type},
    )
    return JSONResponse(content=_artifact_dict(artifact))


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/jobs  — list jobs
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/jobs", dependencies=[Depends(require_auth)])
def list_jobs(folder_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return all jobs for the folder ordered by created_at descending."""
    from sqlmodel import select

    from backend.app.models import Job

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    _mark_stalled_jobs(db, fid)

    jobs = db.exec(
        select(Job).where(Job.folder_id == fid).order_by(Job.created_at.desc())
    ).all()
    return JSONResponse(content={"jobs": [_job_dict(j) for j in jobs]})


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/jobs/{job_id}  — get job status
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_job(folder_id: str, job_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return a single job row."""
    from backend.app.models import Job

    fid = _parse_uuid(folder_id, "folder_id")
    jid = _parse_uuid(job_id, "job_id")

    _folder_or_404(db, fid)

    _mark_stalled_jobs(db, fid)

    job = db.get(Job, jid)
    if job is None or job.folder_id != fid:
        raise HTTPException(status_code=404, detail="Job not found")

    return JSONResponse(content={"job": _job_dict(job)})


# ---------------------------------------------------------------------------
# DELETE /v1/folders/{folder_id}/jobs/{job_id}  — delete job + artifacts
# ---------------------------------------------------------------------------


@router.delete(
    "/{folder_id}/jobs/{job_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def delete_job(folder_id: str, job_id: str, db=Depends(_db_session)) -> JSONResponse:
    """
    Delete a job and all artifacts that were produced by it, then update the
    folder status to reflect the remaining jobs.

    Rules
    -----
    - The job must belong to the specified folder (404 otherwise).
    - Jobs in ``queued`` or ``running`` state cannot be deleted (HTTP 409).
      Cancel / wait for the job to finish first.
    - Artifact object-storage objects are deleted best-effort when R2 is
      configured; a storage failure does **not** prevent the DB row from
      being removed.

    Response body
    -------------
    .. code-block:: json

        {
          "deleted_job_id": "<uuid>",
          "deleted_artifact_ids": ["<uuid>", ...],
          "folder_status": "<new-status>"
        }
    """
    from sqlmodel import select

    from backend.app import storage
    from backend.app.models import Artifact, Job

    fid = _parse_uuid(folder_id, "folder_id")
    jid = _parse_uuid(job_id, "job_id")

    folder = _folder_or_404(db, fid)

    job = db.get(Job, jid)
    if job is None or job.folder_id != fid:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete a job with status '{job.status}'. "
                "Wait for the job to finish or mark it as failed first."
            ),
        )

    # Collect artifacts linked to this job.
    artifacts = db.exec(
        select(Artifact).where(
            Artifact.folder_id == fid,
            Artifact.job_id == jid,
        )
    ).all()

    deleted_artifact_ids: list[str] = []

    # Delete storage objects best-effort (ignore missing objects and storage errors).
    if storage.storage_available():
        for artifact in artifacts:
            try:
                storage.delete_object(artifact.object_key)
            except Exception as exc:
                logger.warning(
                    "Failed to delete storage object %s for artifact %s: %s",
                    artifact.object_key,
                    artifact.id,
                    exc,
                )

    # Delete artifact DB rows.
    for artifact in artifacts:
        deleted_artifact_ids.append(str(artifact.id))
        db.delete(artifact)

    # Delete the job row.
    db.delete(job)
    db.commit()

    # Recompute and persist the folder status.
    db.refresh(folder)
    _recompute_folder_status(db, folder, fid)

    log_event(
        source="backend",
        level="info",
        event_type="jobs.delete",
        message=(
            f"Job {jid} deleted from folder {fid}; "
            f"{len(deleted_artifact_ids)} artifact(s) removed"
        ),
        folder_id=str(fid),
        job_id=str(jid),
        details_json={
            "deleted_artifact_ids": deleted_artifact_ids,
            "new_folder_status": folder.status,
        },
    )

    return JSONResponse(
        content={
            "deleted_job_id": str(jid),
            "deleted_artifact_ids": deleted_artifact_ids,
            "folder_status": folder.status,
        }
    )


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/intent  — get IntentPack
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/intent", dependencies=[Depends(require_auth)])
def get_intent_pack(folder_id: str, db=Depends(_db_session)) -> JSONResponse:
    """
    Return the latest IntentPack artifact for the folder as parsed JSON.

    The IntentPack is an agent-consumable structured document containing
    inferred app domain, screens, user flows, and code hints derived from
    the segment analysis pipeline.

    Returns 404 if no intent_pack artifact exists yet.
    Returns 503 if storage is unavailable.
    """
    import json
    import tempfile

    from sqlmodel import select

    from backend.app import storage
    from backend.app.models import Artifact

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    # Find the most recent intent_pack artifact
    artifact = db.exec(
        select(Artifact)
        .where(Artifact.folder_id == fid)
        .where(Artifact.type == "intent_pack")
        .order_by(Artifact.created_at.desc())
    ).first()

    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail="No IntentPack available yet. Run analysis first.",
        )

    if not storage.storage_available():
        raise HTTPException(status_code=503, detail="Storage not configured")

    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            storage.get_object_to_file(artifact.object_key, tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                intent_pack = json.load(f)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        logger.error("Failed to load IntentPack artifact: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load IntentPack") from exc

    return JSONResponse(content={
        "folder_id": folder_id,
        "artifact_id": str(artifact.id),
        "created_at": _dt(artifact.created_at),
        "intent_pack": intent_pack,
    })
