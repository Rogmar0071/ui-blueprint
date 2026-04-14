from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

_DEFAULT_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
_CONTENT_RANGE_RE = re.compile(r"^bytes (?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)$")
_DEFAULT_UPLOADS_ROOT = (
    Path(os.environ.get("DATA_DIR", "/tmp/ui_blueprint_data")) / "repo_zip_uploads"
)
_UPLOADS_ROOT = Path(os.environ.get("REPO_ZIP_UPLOADS_DIR", str(_DEFAULT_UPLOADS_ROOT)))


def default_chunk_size_bytes() -> int:
    """Return the default repo ZIP chunk size used by the client-facing flows."""
    raw_value = os.environ.get("REPO_ZIP_CHUNK_SIZE_BYTES", str(_DEFAULT_CHUNK_SIZE_BYTES))
    try:
        value = int(raw_value)
    except ValueError:
        return _DEFAULT_CHUNK_SIZE_BYTES
    return max(1, value)


def is_repo_zip_upload(file_name: str, content_type: str | None) -> bool:
    """Return True when the supplied upload metadata clearly identifies a repo ZIP."""
    normalized_name = file_name.strip().lower()
    normalized_type = (content_type or "").strip().lower()
    return normalized_name.endswith(".zip") or normalized_type in {
        "application/zip",
        "application/x-zip-compressed",
    }


def parse_content_range(value: str) -> tuple[int, int, int]:
    """Parse a Content-Range header of the form ``bytes start-end/total``."""
    match = _CONTENT_RANGE_RE.match(value.strip())
    if match is None:
        raise HTTPException(status_code=400, detail="Invalid Content-Range header")

    start = int(match.group("start"))
    end = int(match.group("end"))
    total = int(match.group("total"))
    if start < 0 or end < start or total < 1 or end >= total:
        raise HTTPException(status_code=400, detail="Invalid Content-Range header")
    return start, end, total


def _validated_upload_id(upload_id: str) -> str:
    try:
        return str(uuid.UUID(upload_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None


def _upload_storage_key(upload_id: str) -> str:
    """Derive a filesystem-safe opaque directory name from the client upload id."""
    canonical_upload_id = _validated_upload_id(upload_id)
    return hashlib.sha256(canonical_upload_id.encode("utf-8")).hexdigest()


def _chunks_dir(upload_id: str, *, create: bool = True) -> Path:
    """Return the safe on-disk chunk directory for *upload_id*."""
    safe_upload_id = _upload_storage_key(upload_id)
    chunks_root = (_UPLOADS_ROOT / "chunks").resolve()
    candidate = (chunks_root / safe_upload_id).resolve()
    try:
        candidate.relative_to(chunks_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _manifest_path(upload_id: str) -> Path:
    return _chunks_dir(upload_id, create=False) / "_meta.json"


def load_manifest(upload_id: str) -> dict[str, Any]:
    """Load persisted chunk metadata for *upload_id*."""
    manifest_path = _manifest_path(upload_id)
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found — no chunks received")
    with manifest_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_manifest(upload_id: str, manifest: dict[str, Any]) -> None:
    manifest_path = _manifest_path(upload_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True)


def save_manifest(upload_id: str, manifest: dict[str, Any]) -> None:
    """Persist a validated manifest update for an existing upload session."""
    _write_manifest(upload_id, manifest)


def _write_chunk_bytes(chunk_dir: Path, chunk_index: int, data: bytes) -> None:
    """Write one chunk beneath a previously validated directory using a fixed filename."""
    chunk_path = chunk_dir / f"chunk_{chunk_index:05d}"
    with chunk_path.open("wb") as handle:
        handle.write(data)


def start_upload(
    *,
    folder_id: str,
    file_name: str,
    content_type: str,
    total_bytes: int,
    chunk_size_bytes: int,
) -> dict[str, Any]:
    """Create an empty chunk manifest and return its upload metadata."""
    if total_bytes < 1:
        raise HTTPException(status_code=400, detail="Invalid total_bytes")
    if chunk_size_bytes < 1:
        raise HTTPException(status_code=400, detail="Invalid chunk_size_bytes")

    upload_id = str(uuid.uuid4())
    total_chunks = (total_bytes + chunk_size_bytes - 1) // chunk_size_bytes
    manifest = {
        "upload_id": upload_id,
        "folder_id": folder_id,
        "file_name": file_name,
        "content_type": content_type,
        "chunk_size_bytes": chunk_size_bytes,
        "total_bytes": total_bytes,
        "total_chunks": total_chunks,
        "received_chunks": [],
    }
    _write_manifest(upload_id, manifest)
    return manifest


def _validate_manifest_compatibility(
    manifest: dict[str, Any],
    *,
    folder_id: str,
    file_name: str,
    total_chunks: int,
    total_bytes: int,
) -> None:
    if (
        manifest.get("folder_id") != folder_id
        or manifest.get("file_name") != file_name
        or manifest.get("total_chunks") != total_chunks
        or manifest.get("total_bytes") != total_bytes
    ):
        raise HTTPException(status_code=409, detail="Chunk metadata does not match existing upload")


def write_chunk(
    upload_id: str,
    *,
    folder_id: str,
    file_name: str,
    content_type: str,
    chunk_index: int,
    total_chunks: int,
    chunk_size_bytes: int,
    total_bytes: int,
    data: bytes,
) -> dict[str, Any]:
    """Persist one chunk and update its manifest so retries stay idempotent."""
    if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk index or total")
    if total_bytes < 1:
        raise HTTPException(status_code=400, detail="Invalid total_bytes")
    if chunk_size_bytes < 1:
        raise HTTPException(status_code=400, detail="Invalid chunk_size_bytes")

    chunk_dir = _chunks_dir(upload_id, create=True)
    manifest_path = chunk_dir / "_meta.json"
    manifest = (
        load_manifest(upload_id)
        if manifest_path.exists()
        else {
            "folder_id": folder_id,
            "file_name": file_name,
            "content_type": content_type,
            "chunk_size_bytes": chunk_size_bytes,
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "received_chunks": [],
        }
    )

    _validate_manifest_compatibility(
        manifest,
        folder_id=folder_id,
        file_name=file_name,
        total_chunks=total_chunks,
        total_bytes=total_bytes,
    )

    # Each chunk is stored under its stable ordinal so re-uploading a failed chunk
    # simply replaces the old bytes without duplicating state.
    _write_chunk_bytes(chunk_dir, chunk_index, data)

    received_chunks = set(int(index) for index in manifest.get("received_chunks", []))
    received_chunks.add(chunk_index)
    manifest["received_chunks"] = sorted(received_chunks)
    manifest["content_type"] = content_type or manifest.get("content_type") or "application/zip"
    _write_manifest(upload_id, manifest)

    chunks_received = len(manifest["received_chunks"])
    return {
        **manifest,
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "chunks_received": chunks_received,
        "complete": chunks_received >= total_chunks,
    }


def merge_chunks(upload_id: str, max_bytes: int) -> tuple[dict[str, Any], str]:
    """Merge all uploaded chunks back into a single ZIP file on disk."""
    manifest = load_manifest(upload_id)
    chunk_dir = _chunks_dir(upload_id, create=False)
    total_chunks = int(manifest["total_chunks"])

    present_chunk_indexes = {
        int(path.name.removeprefix("chunk_"))
        for path in chunk_dir.iterdir()
        if path.name.startswith("chunk_")
    }
    missing = [index for index in range(total_chunks) if index not in present_chunk_indexes]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Only {total_chunks - len(missing)}/{total_chunks} chunks received",
        )

    total_written = 0
    assembled_root = (_UPLOADS_ROOT / "assembled").resolve()
    assembled_root.mkdir(parents=True, exist_ok=True)
    current_chunk_path: Path | None = None
    with tempfile.NamedTemporaryFile(
        prefix=f"{_upload_storage_key(upload_id)}-",
        suffix=".zip",
        dir=assembled_root,
        delete=False,
    ) as temporary_file:
        destination = Path(temporary_file.name)
    try:
        with destination.open("wb") as output_handle:
            for index in range(total_chunks):
                current_chunk_path = chunk_dir / f"chunk_{index:05d}"
                chunk_bytes = current_chunk_path.read_bytes()
                total_written += len(chunk_bytes)
                if total_written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Repo ZIP exceeds maximum allowed size of "
                            f"{max_bytes / (1024 * 1024):.2f} MB"
                        ),
                    )
                output_handle.write(chunk_bytes)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        detail = f"Failed to assemble repo ZIP chunks ({type(exc).__name__})"
        if current_chunk_path is not None:
            detail = f"{detail} at {current_chunk_path.name}"
        raise HTTPException(status_code=500, detail=detail) from exc

    return manifest, str(destination)


def cleanup(upload_id: str) -> None:
    """Delete all persisted chunk files for *upload_id*."""
    chunk_dir = _chunks_dir(upload_id, create=False)
    if not chunk_dir.exists():
        return
    for child in chunk_dir.iterdir():
        child.unlink(missing_ok=True)
    chunk_dir.rmdir()
