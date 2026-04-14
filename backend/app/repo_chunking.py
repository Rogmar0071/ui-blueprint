from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

_DEFAULT_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
_UPLOADS_ROOT = Path(os.environ.get("REPO_ZIP_UPLOADS_DIR", "/tmp/repo_zip_uploads"))


def default_chunk_size_bytes() -> int:
    """Return the default repo ZIP chunk size used by the client-facing flows."""
    raw_value = os.environ.get("REPO_ZIP_CHUNK_SIZE_BYTES", str(_DEFAULT_CHUNK_SIZE_BYTES))
    try:
        value = int(raw_value)
    except ValueError:
        return _DEFAULT_CHUNK_SIZE_BYTES
    return max(1, value)


def _chunks_dir(upload_id: str) -> Path:
    """Return the safe on-disk chunk directory for *upload_id*."""
    try:
        uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None

    chunks_root = (_UPLOADS_ROOT / "chunks").resolve()
    candidate = (chunks_root / upload_id).resolve()
    try:
        candidate.relative_to(chunks_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _manifest_path(upload_id: str) -> Path:
    return _chunks_dir(upload_id) / "_meta.json"


def load_manifest(upload_id: str) -> dict[str, Any]:
    """Load persisted chunk metadata for *upload_id*."""
    manifest_path = _manifest_path(upload_id)
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found — no chunks received")
    with manifest_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_manifest(upload_id: str, manifest: dict[str, Any]) -> None:
    manifest_path = _manifest_path(upload_id)
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True)


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

    chunk_dir = _chunks_dir(upload_id)
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
    chunk_path = chunk_dir / f"chunk_{chunk_index:05d}"
    with chunk_path.open("wb") as handle:
        handle.write(data)

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


def merge_chunks(upload_id: str, destination_path: str, max_bytes: int) -> dict[str, Any]:
    """Merge all uploaded chunks back into a single ZIP file on disk."""
    manifest = load_manifest(upload_id)
    chunk_dir = _chunks_dir(upload_id)
    total_chunks = int(manifest["total_chunks"])

    missing = [
        index for index in range(total_chunks) if not (chunk_dir / f"chunk_{index:05d}").exists()
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Only {total_chunks - len(missing)}/{total_chunks} chunks received",
        )

    total_written = 0
    destination = Path(destination_path)
    try:
        with destination.open("wb") as output_handle:
            for index in range(total_chunks):
                chunk_path = chunk_dir / f"chunk_{index:05d}"
                chunk_bytes = chunk_path.read_bytes()
                total_written += len(chunk_bytes)
                if total_written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Repo ZIP exceeds maximum allowed size of "
                            f"{max_bytes // (1024 * 1024)} MB"
                        ),
                    )
                output_handle.write(chunk_bytes)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to assemble repo ZIP chunks") from exc

    return manifest


def cleanup(upload_id: str) -> None:
    """Delete all persisted chunk files for *upload_id*."""
    chunk_dir = _chunks_dir(upload_id)
    for child in chunk_dir.iterdir():
        child.unlink(missing_ok=True)
    chunk_dir.rmdir()
