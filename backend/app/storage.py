"""
backend.app.storage
===================
Cloudflare R2 / S3-compatible object-storage helpers.

Configuration (environment variables)
--------------------------------------
R2_ENDPOINT          https://<accountid>.r2.cloudflarestorage.com
R2_BUCKET            Bucket name
R2_ACCESS_KEY_ID     R2 access-key ID
R2_SECRET_ACCESS_KEY R2 secret access key

When any of the four env-vars is missing the module still imports cleanly;
calls to ``upload_bytes`` / ``get_presigned_url`` raise ``RuntimeError``
(HTTP 503 to the caller).

Object-key convention
----------------------
All folder artifacts are stored under ``folders/{folder_id}/{filename}``.
"""

from __future__ import annotations

import io
import os
from typing import Optional

_s3_client = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_client():
    """Return (and lazily create) the shared boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        import boto3
        from botocore.config import Config

        endpoint = os.environ.get("R2_ENDPOINT", "").strip()
        access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

        if not all([endpoint, access_key, secret_key]):
            raise RuntimeError(
                "R2 storage is not configured. "
                "Set R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY."
            )

        _s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


def _bucket() -> str:
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("R2_BUCKET environment variable is not set.")
    return bucket


def _reset_client() -> None:
    """Reset cached client – used in tests."""
    global _s3_client
    _s3_client = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def folder_object_key(folder_id: str, filename: str) -> str:
    """Return the canonical object key for a folder artifact."""
    return f"folders/{folder_id}/{filename}"


def upload_bytes(
    folder_id: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """
    Upload *data* to R2 under ``folders/{folder_id}/{filename}``.

    Returns the object key.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    client = _get_client()
    key = folder_object_key(folder_id, filename)
    client.upload_fileobj(
        io.BytesIO(data),
        _bucket(),
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def get_presigned_url(object_key: str, expires_in: int = 3600) -> str:
    """
    Generate a presigned GET URL for *object_key*.

    Returns a URL string valid for *expires_in* seconds.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": object_key},
        ExpiresIn=expires_in,
    )


def storage_available() -> bool:
    """Return True iff all required R2 env-vars are present."""
    return all(
        os.environ.get(k, "").strip()
        for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    )


def get_object_bytes(object_key: str) -> Optional[bytes]:
    """
    Download and return the raw bytes for *object_key*.

    Returns ``None`` if the object does not exist.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    import botocore.exceptions

    client = _get_client()
    try:
        buf = io.BytesIO()
        client.download_fileobj(_bucket(), object_key, buf)
        return buf.getvalue()
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def get_object_to_file(object_key: str, local_path: str) -> bool:
    """
    Stream *object_key* from R2 directly to *local_path* on disk.

    Bytes are written incrementally via boto3's ``download_file``, so the
    full object is never held in memory.

    Returns ``True`` on success, ``False`` if the object does not exist.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    import botocore.exceptions

    client = _get_client()
    try:
        client.download_file(_bucket(), object_key, local_path)
        return True
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise


def delete_object(object_key: str) -> bool:
    """
    Delete *object_key* from R2.

    Returns ``True`` if the object was deleted, ``False`` if it did not exist.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    import botocore.exceptions

    client = _get_client()
    try:
        client.delete_object(Bucket=_bucket(), Key=object_key)
        return True
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise


def upload_file(
    folder_id: str,
    filename: str,
    local_path: str,
    content_type: str = "application/octet-stream",
) -> str:
    """
    Upload a local file to R2 under ``folders/{folder_id}/{filename}``.

    Uses boto3's ``upload_file`` which streams from disk, avoiding loading
    the full file into memory.

    Returns the object key.
    Raises ``RuntimeError`` if R2 is not configured.
    """
    client = _get_client()
    key = folder_object_key(folder_id, filename)
    client.upload_file(
        local_path,
        _bucket(),
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key
