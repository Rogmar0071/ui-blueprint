"""
backend.app.models
==================
SQLModel data models for folder-based clip bundles.

Tables
------
- global_chat_messages : persisted global chat history for /api/chat
- folders          : top-level container for a recorded/picked clip and all derived data
- folder_messages  : per-folder chat history
- jobs             : background processing jobs (analyze / blueprint)
- artifacts        : object-storage references for files produced by jobs
- ops_events       : server-side operations log (backend + worker activity)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# global_chat_messages
# ---------------------------------------------------------------------------


class GlobalChatMessage(SQLModel, table=True):
    __tablename__ = "global_chat_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # user / assistant / system
    role: str
    content: str = Field(sa_column=Column(sa.Text))
    session_id: Optional[str] = Field(default=None, index=True)
    domain_profile_id: Optional[str] = Field(default=None, index=True)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    # When a user message is edited, the original is preserved but marked
    # superseded by the new message's id.
    superseded_by_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


class Folder(SQLModel, table=True):
    __tablename__ = "folders"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )
    # pending / uploading / queued / running / done / failed
    status: str = Field(default="pending")
    clip_object_key: Optional[str] = Field(default=None)

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# folder_messages
# ---------------------------------------------------------------------------


class FolderMessage(SQLModel, table=True):
    __tablename__ = "folder_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # user / assistant / system
    role: str
    content: str = Field(sa_column=Column(sa.Text))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # analyze / blueprint
    type: str
    # queued / running / succeeded / failed
    status: str = Field(default="queued")
    progress: int = Field(default=0)
    error: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )
    rq_job_id: Optional[str] = Field(default=None)

    # ---------------------------------------------------------------------------
    # Pipeline v1 checkpoint fields (analyze stage)
    # ---------------------------------------------------------------------------
    # Current pipeline stage: 'prepare' | 'frames' | 'summarize'
    analyze_stage: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    # How many frames have been extracted and uploaded so far.
    analyze_cursor_frame_index: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True)
    )
    # Estimated total frames to extract (set during prepare; may be None).
    analyze_total_frames: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True)
    )
    # Clip object key cached in the checkpoint for robustness.
    analyze_clip_object_key: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True)
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


class Artifact(SQLModel, table=True):
    __tablename__ = "artifacts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # clip / analysis_json / analysis_md / blueprint_json / blueprint_md / transcript
    type: str
    object_key: str
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# ops_events
# ---------------------------------------------------------------------------


class OpsEvent(SQLModel, table=True):
    __tablename__ = "ops_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, index=True),
    )
    # backend / worker / storage / rq / db / auth
    source: str = Field(sa_column=Column(sa.Text, index=True))
    # debug / info / warning / error
    level: str = Field(sa_column=Column(sa.Text, index=True))
    # e.g. "folders.create", "clip.upload.started", "jobs.enqueue"
    event_type: str = Field(sa_column=Column(sa.Text, index=True))
    message: str = Field(sa_column=Column(sa.Text))
    folder_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    job_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    artifact_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    rq_job_id: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    request_id: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    http_method: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    http_path: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    http_status: Optional[int] = Field(default=None, sa_column=Column(sa.Integer, nullable=True))
    duration_ms: Optional[int] = Field(default=None, sa_column=Column(sa.Integer, nullable=True))
    error_type: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    error_detail: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    details_json: Optional[Any] = Field(
        default=None,
        sa_column=Column(sa.JSON, nullable=True),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        # Truncate error_detail to 2000 chars.
        if data.get("error_detail") and len(data["error_detail"]) > 2000:
            data["error_detail"] = data["error_detail"][:2000]
        super().__init__(**data)
