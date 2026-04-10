"""
backend.app.folder_routes
==========================
FastAPI router implementing folder-based clip-bundle persistence.

Endpoints
---------
POST   /v1/folders                              create folder                       [auth]
GET    /v1/folders                              list folders                        [auth]
GET    /v1/folders/{folder_id}                  get folder detail                   [auth]
DELETE /v1/folders/{folder_id}                  delete folder (cascade)             [auth]
POST   /v1/folders/{folder_id}/clip             upload clip to folder               [auth]
GET    /v1/folders/{folder_id}/artifacts/{id}   presigned/streaming artifact URL    [auth]
POST   /v1/folders/{folder_id}/messages         send chat message (AI replies)      [auth]
GET    /v1/folders/{folder_id}/messages         list chat messages                  [auth]
POST   /v1/folders/{folder_id}/jobs             enqueue a job                       [auth]
GET    /v1/folders/{folder_id}/jobs             list jobs                           [auth]
GET    /v1/folders/{folder_id}/jobs/{job_id}    get job status                      [auth]

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
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from backend.app.auth import require_auth
from ui_blueprint.domain.ir import SCHEMA_VERSION

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
        "created_at": _dt(job.created_at),
        "updated_at": _dt(job.updated_at),
    }


def _artifact_dict(artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "folder_id": str(artifact.folder_id),
        "type": artifact.type,
        "object_key": artifact.object_key,
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
    from sqlmodel import select

    from backend.app.models import Artifact, FolderMessage, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    # Cascade deletes (for DBs/FK settings that don't auto-cascade).
    for model in (FolderMessage, Job, Artifact):
        rows = db.exec(select(model).where(model.folder_id == fid)).all()
        for row in rows:
            db.delete(row)

    db.delete(folder)
    db.commit()


# ---------------------------------------------------------------------------
# POST /v1/folders/{folder_id}/clip  — upload clip
# ---------------------------------------------------------------------------


@router.post("/{folder_id}/clip", status_code=202, dependencies=[Depends(require_auth)])
async def upload_clip(folder_id: str, clip: UploadFile, db=Depends(_db_session)) -> JSONResponse:
    """
    Accept a multipart clip upload, store it in R2 (when configured), create
    an Artifact record, and enqueue an ``analyze`` job.

    Returns 202 Accepted with the created job info.
    """
    from backend.app import storage, worker
    from backend.app.models import Artifact, Job

    fid = _parse_uuid(folder_id, "folder_id")
    folder = _folder_or_404(db, fid)

    clip_bytes = await clip.read()
    filename = clip.filename or "clip.mp4"

    # --- Storage (R2) -------------------------------------------------------
    clip_key: str | None = None
    if storage.storage_available():
        try:
            clip_key = storage.upload_bytes(
                folder_id, filename, clip_bytes, clip.content_type or "video/mp4"
            )
        except Exception as exc:
            logger.error("R2 upload failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Storage upload failed: {exc}") from exc

        # Persist clip artifact.
        artifact = Artifact(
            folder_id=fid,
            type="clip",
            object_key=clip_key,
        )
        db.add(artifact)

    # Update folder clip_object_key.
    folder.clip_object_key = clip_key
    folder.status = "queued"
    folder.updated_at = datetime.now(timezone.utc)
    db.add(folder)

    # Create analyze job only when there is no active one for this folder.
    from sqlmodel import select

    existing_job = db.exec(
        select(Job)
        .where(Job.folder_id == fid)
        .where(Job.type == "analyze")
        .where(Job.status.in_(["queued", "running"]))
    ).first()

    if existing_job is None:
        # Create job row.
        job = Job(folder_id=fid, type="analyze")
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
    else:
        job = existing_job

    return JSONResponse(
        content={
            "folder_id": folder_id,
            "job": _job_dict(job),
            "clip_object_key": clip_key,
        },
        status_code=202,
    )


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
                line += f" [error: {job.error[:100]}]"
            lines.append(line)
    else:
        lines.append("Jobs: none yet.")

    if artifacts:
        lines.append("Artifacts:")
        for artifact in artifacts[:10]:
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

    instructions = _FOLDER_CHAT_SYSTEM_PROMPT
    if folder_context:
        instructions += f"\n\n--- Current folder state ---\n{folder_context}"

    input_messages = []
    for msg in history:
        if msg.role in ("user", "assistant"):
            input_messages.append({"role": msg.role, "content": msg.content})
    input_messages.append({"role": "user", "content": message})

    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=input_messages,
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

    # Build conversation history (last 20 messages, excluding the one just saved).
    history = db.exec(
        select(FolderMessage)
        .where(FolderMessage.folder_id == fid)
        .order_by(FolderMessage.created_at.desc())
        .limit(20)
    ).all()
    history = list(reversed(history))

    # ---------- Intent routing -------------------------------------------
    intent = _detect_intent(content)
    enqueued_job = None

    if intent in ("analyze", "blueprint"):
        from backend.app import worker

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
# POST /v1/folders/{folder_id}/jobs  — enqueue job
# ---------------------------------------------------------------------------


@router.post("/{folder_id}/jobs", status_code=202, dependencies=[Depends(require_auth)])
def create_job(folder_id: str, body: dict[str, Any], db=Depends(_db_session)) -> JSONResponse:
    """
    Enqueue a background job for the folder.

    Request body::

        {"type": "analyze"}   or   {"type": "blueprint"}
    """
    from backend.app import worker
    from backend.app.models import Job

    fid = _parse_uuid(folder_id, "folder_id")
    _folder_or_404(db, fid)

    job_type = str(body.get("type", "")).strip()
    if job_type not in ("analyze", "blueprint"):
        raise HTTPException(
            status_code=400,
            detail="type must be 'analyze' or 'blueprint'",
        )

    # Reject duplicate active analyze/blueprint jobs for the same folder.
    if job_type in ("analyze", "blueprint"):
        from sqlmodel import select

        existing = db.exec(
            select(Job)
            .where(Job.folder_id == fid)
            .where(Job.type == job_type)
            .where(Job.status.in_(["queued", "running"]))
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"A {job_type} job is already queued or running for this folder.",
            )

    job = Job(folder_id=fid, type=job_type)
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

    return JSONResponse(content={"job": _job_dict(job)}, status_code=202)


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

    job = db.get(Job, jid)
    if job is None or job.folder_id != fid:
        raise HTTPException(status_code=404, detail="Job not found")

    return JSONResponse(content={"job": _job_dict(job)})
