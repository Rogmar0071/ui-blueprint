"""
Folder routes tests
===================
Tests for the folder-based clip-bundle API endpoints.

Uses SQLite in-memory so no Postgres instance is required.
RQ / Redis is disabled via BACKEND_DISABLE_JOBS=1 (inherited from test env).
R2 storage is not configured so storage-dependent paths return expected errors.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

# Must be set before importing the app so modules pick them up at import time.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Use an isolated SQLite DB for each test."""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    # Reset so next test gets a fresh engine.
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


# ---------------------------------------------------------------------------
# Helper: create a folder via API
# ---------------------------------------------------------------------------


def _create_folder(client: TestClient, title: str | None = None) -> dict:
    body = {}
    if title:
        body["title"] = title
    resp = client.post("/v1/folders", json=body, headers=_auth())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# POST /v1/folders
# ---------------------------------------------------------------------------


class TestCreateFolder:
    def test_create_returns_201(self, client: TestClient) -> None:
        resp = client.post("/v1/folders", json={}, headers=_auth())
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["status"] == "pending"

    def test_create_with_title(self, client: TestClient) -> None:
        resp = client.post("/v1/folders", json={"title": "My Clip"}, headers=_auth())
        assert resp.status_code == 201
        assert resp.json()["title"] == "My Clip"

    def test_create_requires_auth(self, client: TestClient) -> None:
        resp = client.post("/v1/folders", json={})
        assert resp.status_code == 401

    def test_create_wrong_token_returns_403(self, client: TestClient) -> None:
        resp = client.post("/v1/folders", json={}, headers=_auth("wrong"))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/folders
# ---------------------------------------------------------------------------


class TestListFolders:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/v1/folders", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["folders"] == []

    def test_list_returns_created_folders(self, client: TestClient) -> None:
        _create_folder(client, "Alpha")
        _create_folder(client, "Beta")
        resp = client.get("/v1/folders", headers=_auth())
        assert resp.status_code == 200
        folders = resp.json()["folders"]
        assert len(folders) == 2
        titles = {f["title"] for f in folders}
        assert titles == {"Alpha", "Beta"}

    def test_list_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/v1/folders")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/folders/{id}
# ---------------------------------------------------------------------------


class TestGetFolder:
    def test_get_returns_folder(self, client: TestClient) -> None:
        folder = _create_folder(client, "Test")
        fid = folder["id"]
        resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == fid
        assert body["title"] == "Test"
        assert "jobs" in body
        assert "artifacts" in body

    def test_get_nonexistent_returns_404(self, client: TestClient) -> None:
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/v1/folders/{fake_id}", headers=_auth())
        assert resp.status_code == 404

    def test_get_invalid_uuid_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/folders/not-a-uuid", headers=_auth())
        assert resp.status_code == 400

    def test_get_requires_auth(self, client: TestClient) -> None:
        resp = client.get(f"/v1/folders/{uuid.uuid4()}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /v1/folders/{id}
# ---------------------------------------------------------------------------


class TestDeleteFolder:
    def test_delete_returns_204(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.delete(f"/v1/folders/{fid}", headers=_auth())
        assert resp.status_code == 204

    def test_delete_removes_folder(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        client.delete(f"/v1/folders/{fid}", headers=_auth())
        get_resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        assert get_resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.delete(f"/v1/folders/{uuid.uuid4()}", headers=_auth())
        assert resp.status_code == 404

    def test_delete_requires_auth(self, client: TestClient) -> None:
        resp = client.delete(f"/v1/folders/{uuid.uuid4()}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/folders/{id}/clip  — upload (no R2 configured → 502)
# ---------------------------------------------------------------------------


class TestUploadClip:
    def test_upload_without_r2_returns_202_when_no_r2(
        self, client: TestClient, monkeypatch
    ) -> None:
        """
        When R2 is not configured and BACKEND_DISABLE_JOBS=1 the endpoint still
        creates a Job row and returns 202 (no storage upload attempted).
        """
        # Clear all R2 env-vars so storage.storage_available() returns False.
        for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(k, raising=False)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/clip",
            files={"clip": ("test.mp4", b"\x00\x01\x02", "video/mp4")},
            headers=_auth(),
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "job" in body
        assert body["job"]["type"] == "analyze"
        assert body["job"]["status"] == "queued"

    def test_upload_nonexistent_folder_returns_404(self, client, monkeypatch) -> None:
        for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(k, raising=False)
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/v1/folders/{fake_id}/clip",
            files={"clip": ("test.mp4", b"\x00", "video/mp4")},
            headers=_auth(),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /v1/folders/{id}/messages  — chat
# ---------------------------------------------------------------------------

_MOCK_REPLY = "Mocked AI reply."


def _mock_openai(message, history, api_key, folder_context=""):
    """Test-only stand-in for _call_openai_responses_api."""
    return _MOCK_REPLY


class TestFolderChat:
    def test_post_message_no_openai_key_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OPENAI_API_KEY is absent the endpoint returns HTTP 503."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "What does this clip show?"},
            headers=_auth(),
        )
        assert resp.status_code == 503
        assert "OPENAI_API_KEY" in resp.json()["detail"]

    def test_post_message_returns_ai_reply(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OPENAI_API_KEY is set, the AI reply is persisted and returned."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "What does this clip show?"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["schema_version"]
        assert "user_message" in body
        assert "assistant_message" in body
        assert body["user_message"]["role"] == "user"
        assert body["assistant_message"]["role"] == "assistant"
        assert body["assistant_message"]["content"] == _MOCK_REPLY
        assert "tools_available" in body

    def test_post_message_persisted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User + assistant messages are both stored in the database."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "Hello"},
            headers=_auth(),
        )

        resp = client.get(f"/v1/folders/{fid}/messages", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"]
        messages = body["messages"]
        # 2 messages: user + assistant
        assert len(messages) == 2

    def test_post_message_empty_returns_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty message is rejected before the key check."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": ""},
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_post_message_nonexistent_folder_returns_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-existent folder is detected before the key check."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            f"/v1/folders/{uuid.uuid4()}/messages",
            json={"message": "Hello"},
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_analyze_intent_enqueues_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A message matching the 'analyze' intent auto-enqueues an analyze job."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "Please analyze this clip"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "enqueued_job" in body
        assert body["enqueued_job"]["type"] == "analyze"
        assert body["enqueued_job"]["status"] == "queued"

    def test_compile_intent_enqueues_blueprint_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A message matching the 'compile' intent auto-enqueues a blueprint job."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "compile the blueprint now"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "enqueued_job" in body
        assert body["enqueued_job"]["type"] == "blueprint"

    def test_status_intent_does_not_enqueue_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A status-check message does NOT enqueue a job."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "what is the status?"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "enqueued_job" not in body

    def test_generic_message_does_not_enqueue_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generic question does not trigger job enqueuing."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]

        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "What is a blueprint?"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "enqueued_job" not in body


# ---------------------------------------------------------------------------
# GET /v1/folders/{id}/messages
# ---------------------------------------------------------------------------


class TestListMessages:
    def test_list_empty(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        folder = _create_folder(client)
        resp = client.get(f"/v1/folders/{folder['id']}/messages", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"]
        assert body["messages"] == []

    def test_list_requires_auth(self, client: TestClient) -> None:
        resp = client.get(f"/v1/folders/{uuid.uuid4()}/messages")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST/GET /v1/folders/{id}/jobs
# ---------------------------------------------------------------------------


class TestJobs:
    def test_create_analyze_job(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "analyze"},
            headers=_auth(),
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job"]["type"] == "analyze"
        assert body["job"]["status"] == "queued"

    def test_create_blueprint_job(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "blueprint"},
            headers=_auth(),
        )
        assert resp.status_code == 202

    def test_create_invalid_type_returns_400(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "unknown"},
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_list_jobs(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) == 1

    def test_get_job_by_id(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        create_resp = client.post(
            f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth()
        )
        job_id = create_resp.json()["job"]["id"]

        resp = client.get(f"/v1/folders/{fid}/jobs/{job_id}", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["job"]["id"] == job_id

    def test_get_nonexistent_job_returns_404(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.get(f"/v1/folders/{fid}/jobs/{uuid.uuid4()}", headers=_auth())
        assert resp.status_code == 404

    def test_create_job_requires_auth(self, client: TestClient) -> None:
        resp = client.post(f"/v1/folders/{uuid.uuid4()}/jobs", json={"type": "analyze"})
        assert resp.status_code == 401

    def test_duplicate_analyze_job_returns_409(self, client: TestClient) -> None:
        """Second analyze job is rejected when one is already queued/running."""
        folder = _create_folder(client)
        fid = folder["id"]
        resp1 = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "analyze"},
            headers=_auth(),
        )
        assert resp1.status_code == 202
        resp2 = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "analyze"},
            headers=_auth(),
        )
        assert resp2.status_code == 409
        assert "queued or running" in resp2.json()["detail"]

    def test_duplicate_blueprint_job_returns_409(self, client: TestClient) -> None:
        """Second blueprint job is rejected when one is already queued/running."""
        folder = _create_folder(client)
        fid = folder["id"]
        resp1 = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "blueprint"},
            headers=_auth(),
        )
        assert resp1.status_code == 202
        resp2 = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "blueprint"},
            headers=_auth(),
        )
        assert resp2.status_code == 409

    def test_upload_clip_reuses_existing_analyze_job(
        self, client: TestClient, monkeypatch
    ) -> None:
        """
        When an analyze job is already queued for a folder, a subsequent clip
        upload must reuse that job rather than creating a second one.
        """
        for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(k, raising=False)

        folder = _create_folder(client)
        fid = folder["id"]

        # Enqueue an analyze job manually.
        resp1 = client.post(
            f"/v1/folders/{fid}/jobs",
            json={"type": "analyze"},
            headers=_auth(),
        )
        assert resp1.status_code == 202
        first_job_id = resp1.json()["job"]["id"]

        # Upload a clip – should reuse the existing queued job.
        resp2 = client.post(
            f"/v1/folders/{fid}/clip",
            files={"clip": ("test.mp4", b"\x00\x01", "video/mp4")},
            headers=_auth(),
        )
        assert resp2.status_code == 202
        body = resp2.json()
        assert body["job"]["id"] == first_job_id, (
            "upload_clip should reuse the existing queued analyze job"
        )

        # Verify only one job exists.
        jobs_resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        assert len(jobs_resp.json()["jobs"]) == 1


# ---------------------------------------------------------------------------
# GET /v1/folders/{id}/artifacts/{artifact_id}  — no R2 configured
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_artifact_not_found_returns_404(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.get(f"/v1/folders/{fid}/artifacts/{uuid.uuid4()}", headers=_auth())
        assert resp.status_code == 404
