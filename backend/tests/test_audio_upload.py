"""
Tests for POST /v1/folders/{id}/audio endpoint.
"""

from __future__ import annotations

import io
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_folder(client: TestClient) -> dict:
    resp = client.post("/v1/folders", json={}, headers=_auth())
    assert resp.status_code == 201
    return resp.json()


class TestUploadAudio:
    def test_happy_path(self, client: TestClient) -> None:
        """Upload audio: sets audio_object_key on folder, creates artifact, returns 200."""
        folder = _create_folder(client)
        folder_id = folder["id"]

        audio_bytes = b"fake-audio-data"
        fake_object_key = f"folders/{folder_id}/audio.m4a"

        mock_upload = MagicMock(return_value=fake_object_key)
        with patch("backend.app.storage.storage_available", return_value=True), \
             patch("backend.app.storage.upload_file", mock_upload):
            resp = client.post(
                f"/v1/folders/{folder_id}/audio",
                files={"audio": ("recording.m4a", io.BytesIO(audio_bytes), "audio/mp4")},
                headers=_auth(),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["audio_object_key"] == fake_object_key
        assert "artifact_id" in body

        # Verify folder has audio_object_key set.
        folder_resp = client.get(f"/v1/folders/{folder_id}", headers=_auth())
        assert folder_resp.status_code == 200
        folder_data = folder_resp.json()
        assert folder_data["audio_object_key"] == fake_object_key
        # Status should be audio_ready since no clip exists.
        assert folder_data["status"] == "audio_ready"

    def test_folder_with_clip_status_unchanged(self, client: TestClient) -> None:
        """When a clip already exists, uploading audio should not change status to audio_ready."""
        folder = _create_folder(client)
        folder_id = folder["id"]

        # Manually set clip_object_key on folder via DB.
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Folder  # noqa: F811

        with Session(db_module._engine) as session:
            f = session.get(Folder, uuid.UUID(folder_id))
            f.clip_object_key = f"folders/{folder_id}/clip.mp4"
            f.status = "done"
            session.add(f)
            session.commit()

        fake_object_key = f"folders/{folder_id}/audio.m4a"
        mock_upload = MagicMock(return_value=fake_object_key)
        with patch("backend.app.storage.storage_available", return_value=True), \
             patch("backend.app.storage.upload_file", mock_upload):
            resp = client.post(
                f"/v1/folders/{folder_id}/audio",
                files={"audio": ("recording.m4a", io.BytesIO(b"audio"), "audio/mp4")},
                headers=_auth(),
            )

        assert resp.status_code == 200, resp.text

        folder_resp = client.get(f"/v1/folders/{folder_id}", headers=_auth())
        assert folder_resp.status_code == 200
        # Status should remain "done" since clip exists.
        assert folder_resp.json()["status"] == "done"

    def test_missing_folder_404(self, client: TestClient) -> None:
        """Uploading audio to a non-existent folder returns 404."""
        fake_id = str(uuid.uuid4())
        with patch("backend.app.storage.storage_available", return_value=True):
            resp = client.post(
                f"/v1/folders/{fake_id}/audio",
                files={"audio": ("recording.m4a", io.BytesIO(b"audio"), "audio/mp4")},
                headers=_auth(),
            )
        assert resp.status_code == 404

    def test_missing_audio_field_422(self, client: TestClient) -> None:
        """Submitting without the 'audio' field returns 422."""
        folder = _create_folder(client)
        folder_id = folder["id"]
        with patch("backend.app.storage.storage_available", return_value=True):
            resp = client.post(
                f"/v1/folders/{folder_id}/audio",
                headers=_auth(),
            )
        assert resp.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        """Audio upload requires authentication."""
        folder = _create_folder(client)
        folder_id = folder["id"]
        resp = client.post(
            f"/v1/folders/{folder_id}/audio",
            files={"audio": ("recording.m4a", io.BytesIO(b"audio"), "audio/mp4")},
        )
        assert resp.status_code == 401

    def test_no_storage_returns_502(self, client: TestClient) -> None:
        """When storage is not configured, endpoint returns 502."""
        folder = _create_folder(client)
        folder_id = folder["id"]
        with patch("backend.app.storage.storage_available", return_value=False):
            resp = client.post(
                f"/v1/folders/{folder_id}/audio",
                files={"audio": ("recording.m4a", io.BytesIO(b"audio"), "audio/mp4")},
                headers=_auth(),
            )
        assert resp.status_code == 502
