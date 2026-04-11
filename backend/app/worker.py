"""
backend.app.worker
==================
Background job functions for RQ (Redis Queue) workers.

Each function is designed to be enqueued via ``rq`` but also callable
directly for synchronous execution (tests / DISABLE_JOBS mode).

Job types
---------
analyze    Run ``ui_blueprint extract`` on the clip stored in R2, then
           upload the resulting blueprint JSON + Markdown to R2, and
           create the corresponding Artifact rows.

blueprint  Compile a blueprint from an existing analysis_json artifact,
           producing blueprint_json + blueprint_md artifacts.

Environment
-----------
REDIS_URL              Redis / Valkey connection URL (e.g. redis://localhost:6379/0).
                       When absent, jobs are executed synchronously in a thread.
BACKEND_DISABLE_JOBS   If "1", skip job execution entirely (for unit tests).
DATABASE_URL           Required by the job to persist status updates.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

from backend.app.ops_log import log_event as _log_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RQ integration helpers
# ---------------------------------------------------------------------------


def _redis_queue(name: str = "default"):
    """Return an RQ Queue connected to REDIS_URL, or None if not configured."""
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(redis_url)
        return Queue(name, connection=conn)
    except Exception as exc:  # pragma: no cover – connection errors in prod
        logger.warning("RQ unavailable (%s); will run jobs synchronously.", exc)
        return None


def enqueue_job(job_id: str, job_type: str) -> Optional[str]:
    """
    Enqueue *job_type* for *job_id*.

    Returns the RQ job ID string on success, or ``None`` when running
    synchronously (no Redis) or when BACKEND_DISABLE_JOBS=1.

    When BACKEND_DISABLE_JOBS is set the job function is called directly
    on the current thread so tests get predictable behaviour.

    RQ job timeouts are configurable via env vars:
    - RQ_JOB_TIMEOUT_S: hard job timeout in seconds (default 1800)
    - RQ_RESULT_TTL_S: how long to retain job result metadata (default 86400)
    """
    disable = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        return None

    job_timeout = int(os.environ.get("RQ_JOB_TIMEOUT_S", 1800))
    result_ttl = int(os.environ.get("RQ_RESULT_TTL_S", 86400))

    q = _redis_queue()
    if q is not None:
        fn = _JOB_FUNCTIONS.get(job_type)
        if fn is None:
            raise ValueError(f"Unknown job type: {job_type!r}")
        rq_job = q.enqueue(fn, job_id, job_timeout=job_timeout, result_ttl=result_ttl)
        return rq_job.id

    # No Redis – run synchronously in a thread pool (same behaviour as the
    # legacy sessions implementation).
    from concurrent.futures import ThreadPoolExecutor

    fn = _JOB_FUNCTIONS.get(job_type)
    if fn is None:
        raise ValueError(f"Unknown job type: {job_type!r}")
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(fn, job_id)
    executor.shutdown(wait=False)
    return None


# ---------------------------------------------------------------------------
# Shared DB helpers (used inside job functions)
# ---------------------------------------------------------------------------


def _update_job(job_id: str, **kwargs) -> None:
    """Persist job-status fields to the database."""
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    kwargs["updated_at"] = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        for k, v in kwargs.items():
            setattr(job, k, v)
        session.add(job)
        session.commit()


def _get_job(job_id: str):
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    with Session(get_engine()) as session:
        return session.get(Job, uuid.UUID(job_id))


def _get_folder(folder_id: str):
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Folder

    with Session(get_engine()) as session:
        return session.get(Folder, uuid.UUID(folder_id))


def _create_artifact(folder_id: str, artifact_type: str, object_key: str) -> None:
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Artifact

    with Session(get_engine()) as session:
        artifact = Artifact(
            folder_id=uuid.UUID(folder_id),
            type=artifact_type,
            object_key=object_key,
        )
        session.add(artifact)
        session.commit()


def _update_folder_status(folder_id: str, status: str) -> None:
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Folder

    with Session(get_engine()) as session:
        folder = session.get(Folder, uuid.UUID(folder_id))
        if folder is None:
            return
        folder.status = status
        folder.updated_at = datetime.now(timezone.utc)
        session.add(folder)
        session.commit()


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

# Default seconds allowed for the extractor subprocess in run_analyze.
# Overridable via ANALYZE_EXTRACT_TIMEOUT_S env var.
_EXTRACTOR_TIMEOUT_SECONDS_DEFAULT = 900


def run_analyze(job_id: str) -> None:
    """
    Download the clip from R2, run ``ui_blueprint extract``, upload outputs.

    Updates the ``jobs`` row with running/succeeded/failed status throughout.
    Produces ``analysis.json`` (and ``analysis.md``) and stores them in R2 as
    ``analysis_json`` / ``analysis_md`` Artifact rows.
    """
    job = _get_job(job_id)
    if job is None:
        logger.error("run_analyze: job %s not found", job_id)
        _log_event(
            source="worker",
            level="error",
            event_type="worker.abandoned",
            message=f"run_analyze: job {job_id} not found in DB",
            job_id=job_id,
        )
        return

    folder_id = str(job.folder_id)
    extract_timeout = int(
        os.environ.get("ANALYZE_EXTRACT_TIMEOUT_S", _EXTRACTOR_TIMEOUT_SECONDS_DEFAULT)
    )
    _update_job(job_id, status="running", progress=5)
    _update_folder_status(folder_id, "running")
    _log_event(
        source="worker",
        level="info",
        event_type="jobs.start",
        message=f"Job analyze started: {job_id}",
        folder_id=folder_id,
        job_id=job_id,
        rq_job_id=job.rq_job_id,
    )

    try:
        from backend.app import storage

        folder = _get_folder(folder_id)
        if folder is None or not folder.clip_object_key:
            raise RuntimeError("Folder has no clip to analyze")

        # Download clip from R2 to a temp file.
        clip_bytes = storage.get_object_bytes(folder.clip_object_key)
        if clip_bytes is None:
            raise RuntimeError(f"Clip not found in storage: {folder.clip_object_key}")

        _update_job(job_id, progress=15)
        _log_event(
            source="worker",
            level="info",
            event_type="jobs.progress",
            message=f"Job analyze progress 15%: {job_id}",
            folder_id=folder_id,
            job_id=job_id,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            clip_path = os.path.join(tmpdir, "clip.mp4")
            analysis_path = os.path.join(tmpdir, "analysis.json")

            with open(clip_path, "wb") as fh:
                fh.write(clip_bytes)

            _update_job(job_id, progress=20)

            # Run extractor — output saved as analysis.json.
            logger.info("run_analyze: extraction starting for job %s", job_id)
            result = subprocess.run(
                [sys.executable, "-m", "ui_blueprint", "extract", clip_path, "-o", analysis_path],
                capture_output=True,
                text=True,
                timeout=extract_timeout,
            )
            if result.returncode != 0:
                # Capture last 1000 characters of stderr for diagnostics.
                stderr_tail = (result.stderr or "")[-1000:].strip()
                logger.error(
                    "run_analyze: extraction failed for job %s (rc=%d); stderr tail: %s",
                    job_id,
                    result.returncode,
                    stderr_tail,
                )
                raise RuntimeError(
                    f"Extraction failed (rc={result.returncode}). "
                    f"stderr: {stderr_tail}"
                )
            logger.info("run_analyze: extraction finished for job %s", job_id)

            _update_job(job_id, progress=70)
            _log_event(
                source="worker",
                level="info",
                event_type="jobs.progress",
                message="Job analyze progress 70%: extraction complete",
                folder_id=folder_id,
                job_id=job_id,
            )

            # Upload analysis.json as analysis_json artifact.
            with open(analysis_path, "rb") as fh:
                analysis_bytes = fh.read()

            analysis_key = storage.upload_bytes(
                folder_id, "analysis.json", analysis_bytes, "application/json"
            )
            _create_artifact(folder_id, "analysis_json", analysis_key)
            _log_event(
                source="worker",
                level="info",
                event_type="artifacts.created",
                message=f"Artifact analysis_json created for folder {folder_id}",
                folder_id=folder_id,
                job_id=job_id,
                details_json={"object_key": analysis_key},
            )

            _update_job(job_id, progress=90)

            # Upload analysis.md as analysis_md artifact if produced alongside.
            md_path = analysis_path.replace(".json", ".md")
            if os.path.exists(md_path):
                with open(md_path, "rb") as fh:
                    md_bytes = fh.read()
                md_key = storage.upload_bytes(
                    folder_id, "analysis.md", md_bytes, "text/markdown"
                )
                _create_artifact(folder_id, "analysis_md", md_key)
                _log_event(
                    source="worker",
                    level="info",
                    event_type="artifacts.created",
                    message=f"Artifact analysis_md created for folder {folder_id}",
                    folder_id=folder_id,
                    job_id=job_id,
                    details_json={"object_key": md_key},
                )

        _update_job(job_id, status="succeeded", progress=100)
        _update_folder_status(folder_id, "done")
        _log_event(
            source="worker",
            level="info",
            event_type="jobs.succeeded",
            message=f"Job analyze succeeded: {job_id}",
            folder_id=folder_id,
            job_id=job_id,
        )

    except subprocess.TimeoutExpired as exc:
        logger.exception("run_analyze timed out for job %s", job_id)
        _update_job(job_id, status="failed", error=str(exc))
        _update_folder_status(folder_id, "failed")
        _log_event(
            source="worker",
            level="error",
            event_type="jobs.failed",
            message=f"Job analyze timed out: {job_id}",
            folder_id=folder_id,
            job_id=job_id,
            error_type="timeout",
            error_detail=(
                f"Extractor subprocess exceeded {extract_timeout}s timeout. "
                f"Original error: {str(exc)[:1900]}"
            ),
        )

    except Exception as exc:
        logger.exception("run_analyze failed for job %s", job_id)
        _update_job(job_id, status="failed", error=str(exc))
        _update_folder_status(folder_id, "failed")
        _log_event(
            source="worker",
            level="error",
            event_type="jobs.failed",
            message=f"Job analyze failed: {job_id}: {exc}",
            folder_id=folder_id,
            job_id=job_id,
            error_type=type(exc).__name__,
            error_detail=str(exc)[:2000],
        )


def run_blueprint(job_id: str) -> None:
    """
    Compile a blueprint from the folder's ``analysis_json`` artifact.

    Downloads the analysis JSON, runs ``ui_blueprint preview`` to render
    preview PNGs (stored as ``preview_png`` artifacts), then uploads the
    analysis JSON as ``blueprint.json`` and generates a Markdown summary
    as ``blueprint.md`` (``blueprint_json`` / ``blueprint_md`` artifacts).
    """
    job = _get_job(job_id)
    if job is None:
        logger.error("run_blueprint: job %s not found", job_id)
        _log_event(
            source="worker",
            level="error",
            event_type="worker.abandoned",
            message=f"run_blueprint: job {job_id} not found in DB",
            job_id=job_id,
        )
        return

    folder_id = str(job.folder_id)
    _update_job(job_id, status="running", progress=10)
    _log_event(
        source="worker",
        level="info",
        event_type="jobs.start",
        message=f"Job blueprint started: {job_id}",
        folder_id=folder_id,
        job_id=job_id,
        rq_job_id=job.rq_job_id,
    )

    try:
        import json

        from sqlmodel import Session, select

        from backend.app import storage
        from backend.app.database import get_engine
        from backend.app.models import Artifact

        # Find the latest analysis_json artifact.
        with Session(get_engine()) as session:
            artifact = session.exec(
                select(Artifact)
                .where(Artifact.folder_id == uuid.UUID(folder_id))
                .where(Artifact.type == "analysis_json")
                .order_by(Artifact.created_at.desc())
            ).first()

        if artifact is None:
            raise RuntimeError("No analysis_json artifact found; run analyze first.")

        analysis_bytes = storage.get_object_bytes(artifact.object_key)
        if analysis_bytes is None:
            raise RuntimeError(f"Analysis JSON not found in storage: {artifact.object_key}")

        _update_job(job_id, progress=30)
        _log_event(
            source="worker",
            level="info",
            event_type="jobs.progress",
            message="Job blueprint progress 30%: analysis loaded",
            folder_id=folder_id,
            job_id=job_id,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            analysis_path = os.path.join(tmpdir, "analysis.json")
            preview_dir = os.path.join(tmpdir, "preview")
            os.makedirs(preview_dir)

            with open(analysis_path, "wb") as fh:
                fh.write(analysis_bytes)

            # Run preview to generate PNG frames.
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ui_blueprint",
                    "preview",
                    analysis_path,
                    "--out",
                    preview_dir,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Preview failed: {result.stderr.strip()}")

            _update_job(job_id, progress=70)
            _log_event(
                source="worker",
                level="info",
                event_type="jobs.progress",
                message="Job blueprint progress 70%: preview generated",
                folder_id=folder_id,
                job_id=job_id,
            )

            # Upload preview PNGs as preview_png artifacts.
            for fname in sorted(os.listdir(preview_dir)):
                if not fname.endswith(".png"):
                    continue
                with open(os.path.join(preview_dir, fname), "rb") as fh:
                    png_bytes = fh.read()
                key = storage.upload_bytes(folder_id, f"preview/{fname}", png_bytes, "image/png")
                _create_artifact(folder_id, "preview_png", key)
                _log_event(
                    source="worker",
                    level="info",
                    event_type="artifacts.created",
                    message=f"Artifact preview_png created for folder {folder_id}",
                    folder_id=folder_id,
                    job_id=job_id,
                    details_json={"object_key": key},
                )

            _update_job(job_id, progress=85)

            # Upload analysis JSON as blueprint.json (blueprint_json artifact).
            bp_key = storage.upload_bytes(
                folder_id, "blueprint.json", analysis_bytes, "application/json"
            )
            _create_artifact(folder_id, "blueprint_json", bp_key)
            _log_event(
                source="worker",
                level="info",
                event_type="artifacts.created",
                message=f"Artifact blueprint_json created for folder {folder_id}",
                folder_id=folder_id,
                job_id=job_id,
                details_json={"object_key": bp_key},
            )

            # Generate and upload a Markdown summary (blueprint_md artifact).
            analysis_data = json.loads(analysis_bytes)
            bp_md_bytes = _analysis_to_blueprint_md(analysis_data).encode("utf-8")
            md_key = storage.upload_bytes(
                folder_id, "blueprint.md", bp_md_bytes, "text/markdown"
            )
            _create_artifact(folder_id, "blueprint_md", md_key)
            _log_event(
                source="worker",
                level="info",
                event_type="artifacts.created",
                message=f"Artifact blueprint_md created for folder {folder_id}",
                folder_id=folder_id,
                job_id=job_id,
                details_json={"object_key": md_key},
            )

        _update_job(job_id, status="succeeded", progress=100)
        _log_event(
            source="worker",
            level="info",
            event_type="jobs.succeeded",
            message=f"Job blueprint succeeded: {job_id}",
            folder_id=folder_id,
            job_id=job_id,
        )

    except Exception as exc:
        logger.exception("run_blueprint failed for job %s", job_id)
        _update_job(job_id, status="failed", error=str(exc))
        _log_event(
            source="worker",
            level="error",
            event_type="jobs.failed",
            message=f"Job blueprint failed: {job_id}: {exc}",
            folder_id=folder_id,
            job_id=job_id,
            error_type=type(exc).__name__,
            error_detail=str(exc)[:2000],
        )


def _analysis_to_blueprint_md(data: dict) -> str:
    """Generate a human-readable Markdown summary from an analysis JSON dict."""
    lines = ["# Blueprint\n"]

    meta = data.get("meta", {})
    if meta:
        lines.append("## Recording Details\n")
        for key, val in list(meta.items())[:12]:
            lines.append(f"- **{key}**: {val}")
        lines.append("")

    elements = data.get("elements_catalog", [])
    if elements:
        by_type: dict[str, int] = {}
        for el in elements:
            t = el.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        lines.append(f"## UI Elements ({len(elements)} detected)\n")
        for t, count in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"- **{t}**: {count}")
        lines.append("")

    chunks = data.get("chunks", [])
    if chunks:
        lines.append(f"## Timeline ({len(chunks)} chunk(s))\n")
        for chunk in chunks[:8]:
            t0 = chunk.get("t0_ms", 0)
            t1 = chunk.get("t1_ms", 0)
            tracks = chunk.get("tracks", [])
            events = chunk.get("events", [])
            lines.append(f"- **{t0}–{t1} ms**: {len(tracks)} tracks, {len(events)} events")
        if len(chunks) > 8:
            lines.append(f"- *…and {len(chunks) - 8} more chunk(s)*")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Job-function registry
# ---------------------------------------------------------------------------

_JOB_FUNCTIONS = {
    "analyze": run_analyze,
    "blueprint": run_blueprint,
}
