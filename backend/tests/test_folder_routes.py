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
# PATCH /v1/folders/{id}  — rename
# ---------------------------------------------------------------------------


class TestPatchFolder:
    def test_rename_succeeds(self, client: TestClient) -> None:
        folder = _create_folder(client, "Old Title")
        fid = folder["id"]
        resp = client.patch(f"/v1/folders/{fid}", json={"title": "New Title"}, headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == fid
        assert body["title"] == "New Title"

    def test_rename_trims_whitespace(self, client: TestClient) -> None:
        folder = _create_folder(client, "Original")
        fid = folder["id"]
        resp = client.patch(f"/v1/folders/{fid}", json={"title": "  Trimmed  "}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["title"] == "Trimmed"

    def test_rename_blank_title_returns_422(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.patch(f"/v1/folders/{fid}", json={"title": "   "}, headers=_auth())
        assert resp.status_code == 422

    def test_rename_empty_title_returns_422(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.patch(f"/v1/folders/{fid}", json={"title": ""}, headers=_auth())
        assert resp.status_code == 422

    def test_rename_title_too_long_returns_422(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.patch(
            f"/v1/folders/{fid}", json={"title": "x" * 121}, headers=_auth()
        )
        assert resp.status_code == 422

    def test_rename_title_max_length_succeeds(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.patch(
            f"/v1/folders/{fid}", json={"title": "x" * 120}, headers=_auth()
        )
        assert resp.status_code == 200

    def test_rename_updates_title_in_get(self, client: TestClient) -> None:
        folder = _create_folder(client, "Before")
        fid = folder["id"]
        client.patch(f"/v1/folders/{fid}", json={"title": "After"}, headers=_auth())
        resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        assert resp.json()["title"] == "After"

    def test_rename_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.patch(
            f"/v1/folders/{uuid.uuid4()}", json={"title": "X"}, headers=_auth()
        )
        assert resp.status_code == 404

    def test_rename_requires_auth(self, client: TestClient) -> None:
        resp = client.patch(f"/v1/folders/{uuid.uuid4()}", json={"title": "X"})
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

    def test_analyze_enqueue_is_idempotent(self, client: TestClient) -> None:
        """Second analyze enqueue returns the existing job instead of creating a new one."""
        folder = _create_folder(client)
        fid = folder["id"]

        resp1 = client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        assert resp1.status_code == 202
        job1_id = resp1.json()["job"]["id"]

        resp2 = client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        assert resp2.status_code == 202
        job2_id = resp2.json()["job"]["id"]

        # Same job returned — no duplicate created.
        assert job1_id == job2_id

        # Confirm only one analyze job exists in the DB.
        list_resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        analyze_jobs = [j for j in list_resp.json()["jobs"] if j["type"] == "analyze"]
        assert len(analyze_jobs) == 1

    def test_analyze_deduped_event_logged(self, client: TestClient) -> None:
        """A jobs.deduped ops event is recorded when a duplicate analyze is suppressed."""
        folder = _create_folder(client)
        fid = folder["id"]

        client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())

        ops_resp = client.get(f"/v1/folders/{fid}/ops", headers=_auth())
        events = ops_resp.json()["events"]
        deduped = [e for e in events if e["event_type"] == "jobs.deduped"]
        assert len(deduped) == 1

    def test_blueprint_enqueue_not_deduplicated(self, client: TestClient) -> None:
        """Blueprint jobs are not subject to deduplication (only analyze is)."""
        folder = _create_folder(client)
        fid = folder["id"]

        resp1 = client.post(f"/v1/folders/{fid}/jobs", json={"type": "blueprint"}, headers=_auth())
        resp2 = client.post(f"/v1/folders/{fid}/jobs", json={"type": "blueprint"}, headers=_auth())
        assert resp1.json()["job"]["id"] != resp2.json()["job"]["id"]


# ---------------------------------------------------------------------------
# DELETE /v1/folders/{id}/jobs/{job_id}  — delete job + artifacts
# ---------------------------------------------------------------------------


def _create_job(client: TestClient, folder_id: str, job_type: str = "analyze") -> dict:
    resp = client.post(
        f"/v1/folders/{folder_id}/jobs",
        json={"type": job_type},
        headers=_auth(),
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["job"]


def _seed_artifact(folder_id: str, job_id: str, artifact_type: str = "analysis_json") -> str:
    """Insert an Artifact row directly into the DB and return its id."""
    import uuid as _uuid

    from sqlmodel import Session

    import backend.app.database as db_module
    from backend.app.models import Artifact

    artifact = Artifact(
        folder_id=_uuid.UUID(folder_id),
        job_id=_uuid.UUID(job_id),
        type=artifact_type,
        object_key=f"folders/{folder_id}/{artifact_type}.json",
    )
    with Session(db_module.get_engine()) as session:
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return str(artifact.id)


class TestDeleteJob:
    # ------------------------------------------------------------------
    # Happy-path
    # ------------------------------------------------------------------

    def test_delete_succeeded_job_returns_200(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        # Manually set status to succeeded so delete is allowed.
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "succeeded"
            s.add(row)
            s.commit()

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_job_id"] == jid
        assert isinstance(body["deleted_artifact_ids"], list)
        assert "folder_status" in body

    def test_delete_removes_job_from_list(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "failed"
            s.add(row)
            s.commit()

        client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())

        resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        assert all(j["id"] != jid for j in resp.json()["jobs"])

    def test_delete_removes_linked_artifacts(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Artifact, Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "succeeded"
            s.add(row)
            s.commit()

        aid = _seed_artifact(fid, jid, "analysis_json")

        client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())

        with Session(db_module.get_engine()) as s:
            remaining = s.exec(
                select(Artifact).where(Artifact.id == uuid.UUID(aid))
            ).first()
        assert remaining is None

    def test_delete_returns_artifact_ids(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "succeeded"
            s.add(row)
            s.commit()

        aid1 = _seed_artifact(fid, jid, "analysis_json")
        aid2 = _seed_artifact(fid, jid, "analysis_md")

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())
        deleted_ids = set(resp.json()["deleted_artifact_ids"])
        assert deleted_ids == {aid1, aid2}

    def test_delete_does_not_remove_unrelated_artifacts(self, client: TestClient) -> None:
        """Artifacts from a different job are not touched."""
        folder = _create_folder(client)
        fid = folder["id"]

        job1 = _create_job(client, fid)
        job2 = _create_job(client, fid, "blueprint")
        jid1, jid2 = job1["id"], job2["id"]

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Artifact, Job

        with Session(db_module.get_engine()) as s:
            for jid in (jid1, jid2):
                row = s.get(Job, uuid.UUID(jid))
                row.status = "succeeded"
                s.add(row)
            s.commit()

        _seed_artifact(fid, jid1, "analysis_json")
        kept_aid = _seed_artifact(fid, jid2, "blueprint_json")

        client.delete(f"/v1/folders/{fid}/jobs/{jid1}", headers=_auth())

        with Session(db_module.get_engine()) as s:
            remaining = s.exec(
                select(Artifact).where(Artifact.id == uuid.UUID(kept_aid))
            ).first()
        assert remaining is not None

    def test_delete_updates_folder_status_to_pending_when_no_jobs_remain(
        self, client: TestClient
    ) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "succeeded"
            s.add(row)
            s.commit()

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())
        assert resp.json()["folder_status"] == "pending"

        # Confirm via GET.
        folder_resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        assert folder_resp.json()["status"] == "pending"

    def test_delete_preserves_folder_done_when_other_succeeded_job_remains(
        self, client: TestClient
    ) -> None:
        folder = _create_folder(client)
        fid = folder["id"]

        job1 = _create_job(client, fid)
        job2 = _create_job(client, fid, "blueprint")
        jid1, jid2 = job1["id"], job2["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            for jid in (jid1, jid2):
                row = s.get(Job, uuid.UUID(jid))
                row.status = "succeeded"
                s.add(row)
            s.commit()

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid1}", headers=_auth())
        assert resp.json()["folder_status"] == "done"

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_delete_queued_job_returns_409(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())
        assert resp.status_code == 409
        assert "queued" in resp.json()["detail"]

    def test_delete_running_job_returns_409(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        job = _create_job(client, fid)
        jid = job["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "running"
            s.add(row)
            s.commit()

        resp = client.delete(f"/v1/folders/{fid}/jobs/{jid}", headers=_auth())
        assert resp.status_code == 409

    def test_delete_nonexistent_job_returns_404(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.delete(f"/v1/folders/{fid}/jobs/{uuid.uuid4()}", headers=_auth())
        assert resp.status_code == 404

    def test_delete_job_wrong_folder_returns_404(self, client: TestClient) -> None:
        """A job that exists but belongs to a different folder returns 404."""
        folder1 = _create_folder(client)
        folder2 = _create_folder(client)
        fid1, fid2 = folder1["id"], folder2["id"]

        job = _create_job(client, fid1)
        jid = job["id"]

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        with Session(db_module.get_engine()) as s:
            row = s.get(Job, uuid.UUID(jid))
            row.status = "failed"
            s.add(row)
            s.commit()

        resp = client.delete(f"/v1/folders/{fid2}/jobs/{jid}", headers=_auth())
        assert resp.status_code == 404

    def test_delete_nonexistent_folder_returns_404(self, client: TestClient) -> None:
        resp = client.delete(
            f"/v1/folders/{uuid.uuid4()}/jobs/{uuid.uuid4()}", headers=_auth()
        )
        assert resp.status_code == 404

    def test_delete_requires_auth(self, client: TestClient) -> None:
        resp = client.delete(f"/v1/folders/{uuid.uuid4()}/jobs/{uuid.uuid4()}")
        assert resp.status_code == 401

    def test_delete_invalid_job_uuid_returns_400(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.delete(f"/v1/folders/{fid}/jobs/not-a-uuid", headers=_auth())
        assert resp.status_code == 400


class TestWatchdog:
    """Verify that running jobs exceeding MAX_JOB_RUNTIME_SECONDS are marked failed."""

    def _seed_running_job(self, client: TestClient, fid: str):
        """Create a job and directly set its status to 'running' in the DB."""
        import uuid as _uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        # Create via API (status=queued), then update DB directly.
        resp = client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        assert resp.status_code == 202
        job_id = resp.json()["job"]["id"]

        with Session(db_module.get_engine()) as session:
            job = session.get(Job, _uuid.UUID(job_id))
            job.status = "running"
            session.add(job)
            session.commit()

        return job_id

    def test_stalled_job_marked_failed_on_get_folder(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /v1/folders/{id} marks a running job as failed if it exceeds max runtime."""
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        folder = _create_folder(client)
        fid = folder["id"]
        job_id = self._seed_running_job(client, fid)

        # Wind back the job's updated_at to simulate stall.
        with Session(db_module.get_engine()) as session:
            job = session.get(Job, _uuid.UUID(job_id))
            job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=9999)
            session.add(job)
            session.commit()

        # Override max runtime to 1 second so ANY running job is stalled.
        monkeypatch.setenv("MAX_JOB_RUNTIME_SECONDS", "1")
        import backend.app.folder_routes as fr
        monkeypatch.setattr(fr, "_MAX_JOB_RUNTIME_SECONDS", 1)

        resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        failed_jobs = [j for j in data["jobs"] if j["id"] == job_id and j["status"] == "failed"]
        assert len(failed_jobs) == 1
        assert failed_jobs[0]["error"] is not None

    def test_stalled_job_ops_event_logged(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Watchdog records a jobs.stalled ops event."""
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Job

        folder = _create_folder(client)
        fid = folder["id"]
        job_id = self._seed_running_job(client, fid)

        with Session(db_module.get_engine()) as session:
            job = session.get(Job, _uuid.UUID(job_id))
            job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=9999)
            session.add(job)
            session.commit()

        monkeypatch.setenv("MAX_JOB_RUNTIME_SECONDS", "1")
        import backend.app.folder_routes as fr
        monkeypatch.setattr(fr, "_MAX_JOB_RUNTIME_SECONDS", 1)

        # Trigger watchdog.
        client.get(f"/v1/folders/{fid}", headers=_auth())

        ops_resp = client.get(f"/v1/folders/{fid}/ops", headers=_auth())
        events = ops_resp.json()["events"]
        stalled = [e for e in events if e["event_type"] == "jobs.stalled"]
        assert len(stalled) == 1

    def test_non_stalled_job_unaffected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recently-updated running job is NOT marked failed."""
        folder = _create_folder(client)
        fid = folder["id"]
        job_id = self._seed_running_job(client, fid)

        # Use a very large max runtime so the job is NOT stalled.
        monkeypatch.setenv("MAX_JOB_RUNTIME_SECONDS", "999999")
        import backend.app.folder_routes as fr
        monkeypatch.setattr(fr, "_MAX_JOB_RUNTIME_SECONDS", 999999)

        resp = client.get(f"/v1/folders/{fid}", headers=_auth())
        jobs = resp.json()["jobs"]
        job = next(j for j in jobs if j["id"] == job_id)
        assert job["status"] == "running"


# ---------------------------------------------------------------------------
# GET /v1/folders/{id}/artifacts/{artifact_id}  — no R2 configured
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_artifact_not_found_returns_404(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        resp = client.get(f"/v1/folders/{fid}/artifacts/{uuid.uuid4()}", headers=_auth())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dedupe: /clip and /messages analyze intent
# ---------------------------------------------------------------------------


class TestAnalyzeDedupe:
    """Verify analyze-job deduplication works across /clip and /messages."""

    def _seed_queued_analyze_job(self, client: TestClient, fid: str) -> str:
        """Create a queued analyze job via POST /jobs and return its id."""
        resp = client.post(f"/v1/folders/{fid}/jobs", json={"type": "analyze"}, headers=_auth())
        assert resp.status_code == 202
        return resp.json()["job"]["id"]

    def test_upload_deduped_when_analyze_job_active(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Second POST /clip while analyze queued/running returns deduped + same job id;
        job count unchanged.
        """
        for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(k, raising=False)

        folder = _create_folder(client)
        fid = folder["id"]

        # First clip upload — creates a queued analyze job.
        resp1 = client.post(
            f"/v1/folders/{fid}/clip",
            files={"clip": ("a.mp4", b"\x00\x01", "video/mp4")},
            headers=_auth(),
        )
        assert resp1.status_code == 202
        job1_id = resp1.json()["job"]["id"]

        # Second clip upload — should deduplicate.
        resp2 = client.post(
            f"/v1/folders/{fid}/clip",
            files={"clip": ("b.mp4", b"\x02\x03", "video/mp4")},
            headers=_auth(),
        )
        assert resp2.status_code == 202
        body2 = resp2.json()
        assert body2["job"]["id"] == job1_id, "Expected same job id on deduped response"
        assert body2.get("deduped") is True

        # Only one analyze job row should exist.
        list_resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        analyze_jobs = [j for j in list_resp.json()["jobs"] if j["type"] == "analyze"]
        assert len(analyze_jobs) == 1

    def test_analyze_intent_deduped_when_job_active(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Second analyze intent via POST /messages reuses same queued/running analyze job;
        job count unchanged.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        import backend.app.folder_routes as fr

        monkeypatch.setattr(fr, "_call_openai_responses_api", _mock_openai)

        folder = _create_folder(client)
        fid = folder["id"]
        job1_id = self._seed_queued_analyze_job(client, fid)

        # Send analyze-intent message while job is queued.
        resp = client.post(
            f"/v1/folders/{fid}/messages",
            json={"message": "Please analyze this clip"},
            headers=_auth(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "enqueued_job" in body
        assert body["enqueued_job"]["id"] == job1_id, "Expected deduped job id"

        # No additional analyze job row created.
        list_resp = client.get(f"/v1/folders/{fid}/jobs", headers=_auth())
        analyze_jobs = [j for j in list_resp.json()["jobs"] if j["type"] == "analyze"]
        assert len(analyze_jobs) == 1


# ---------------------------------------------------------------------------
# Worker timeout env var tests
# ---------------------------------------------------------------------------


class TestWorkerTimeouts:
    """Verify worker timeout env vars are respected."""

    def test_enqueue_job_uses_env_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enqueue_job passes job_timeout/result_ttl from env vars to q.enqueue."""
        import unittest.mock as mock

        import backend.app.worker as worker_module

        monkeypatch.setenv("RQ_JOB_TIMEOUT_S", "300")
        monkeypatch.setenv("RQ_RESULT_TTL_S", "7200")
        monkeypatch.delenv("BACKEND_DISABLE_JOBS", raising=False)

        mock_rq_job = mock.MagicMock()
        mock_rq_job.id = "rq-test-id"

        mock_queue = mock.MagicMock()
        mock_queue.enqueue.return_value = mock_rq_job

        with mock.patch.object(worker_module, "_redis_queue", return_value=mock_queue):
            result = worker_module.enqueue_job("job-123", "analyze")

        assert result == "rq-test-id"
        mock_queue.enqueue.assert_called_once()
        _, call_kwargs = mock_queue.enqueue.call_args
        assert call_kwargs["job_timeout"] == 300
        assert call_kwargs["result_ttl"] == 7200

    def test_enqueue_job_uses_default_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enqueue_job defaults: RQ_JOB_TIMEOUT_S=1800, RQ_RESULT_TTL_S=86400."""
        import unittest.mock as mock

        import backend.app.worker as worker_module

        monkeypatch.delenv("RQ_JOB_TIMEOUT_S", raising=False)
        monkeypatch.delenv("RQ_RESULT_TTL_S", raising=False)
        monkeypatch.delenv("BACKEND_DISABLE_JOBS", raising=False)

        mock_rq_job = mock.MagicMock()
        mock_rq_job.id = "rq-default-id"

        mock_queue = mock.MagicMock()
        mock_queue.enqueue.return_value = mock_rq_job

        with mock.patch.object(worker_module, "_redis_queue", return_value=mock_queue):
            result = worker_module.enqueue_job("job-456", "analyze")

        assert result == "rq-default-id"
        _, call_kwargs = mock_queue.enqueue.call_args
        assert call_kwargs["job_timeout"] == 1800
        assert call_kwargs["result_ttl"] == 86400

    def test_run_analyze_uses_extract_timeout_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subprocess.run timeout in run_analyze uses ANALYZE_EXTRACT_TIMEOUT_S."""
        import unittest.mock as mock

        import backend.app.storage as storage_module
        import backend.app.worker as worker_module

        monkeypatch.setenv("ANALYZE_EXTRACT_TIMEOUT_S", "42")

        captured_kwargs: dict = {}

        def _fake_subprocess_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            raise RuntimeError("short-circuit for test")

        # We only care that the timeout kwarg is passed — mock the whole DB + storage
        # chain so we reach subprocess.run.
        mock_job = mock.MagicMock()
        mock_job.folder_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mock_job.rq_job_id = None

        mock_folder = mock.MagicMock()
        mock_folder.clip_object_key = "test/clip.mp4"

        with (
            mock.patch.object(worker_module, "_get_job", return_value=mock_job),
            mock.patch.object(worker_module, "_get_folder", return_value=mock_folder),
            mock.patch.object(worker_module, "_update_job"),
            mock.patch.object(worker_module, "_update_folder_status"),
            mock.patch.object(worker_module, "_log_event"),
            mock.patch.object(storage_module, "get_object_bytes", return_value=b"\x00"),
            mock.patch("subprocess.run", side_effect=_fake_subprocess_run),
        ):
            worker_module.run_analyze("job-999")

        assert "timeout" in captured_kwargs, "subprocess.run must receive timeout kwarg"
        assert captured_kwargs["timeout"] == 42

    def test_run_analyze_uses_default_extract_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subprocess.run timeout defaults to ANALYZE_EXTRACT_TIMEOUT_S=900 when unset."""
        import unittest.mock as mock

        import backend.app.storage as storage_module
        import backend.app.worker as worker_module

        monkeypatch.delenv("ANALYZE_EXTRACT_TIMEOUT_S", raising=False)

        captured_kwargs: dict = {}

        def _fake_subprocess_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            raise RuntimeError("short-circuit for test")

        mock_job = mock.MagicMock()
        mock_job.folder_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        mock_job.rq_job_id = None

        mock_folder = mock.MagicMock()
        mock_folder.clip_object_key = "test/clip.mp4"

        with (
            mock.patch.object(worker_module, "_get_job", return_value=mock_job),
            mock.patch.object(worker_module, "_get_folder", return_value=mock_folder),
            mock.patch.object(worker_module, "_update_job"),
            mock.patch.object(worker_module, "_update_folder_status"),
            mock.patch.object(worker_module, "_log_event"),
            mock.patch.object(storage_module, "get_object_bytes", return_value=b"\x00"),
            mock.patch("subprocess.run", side_effect=_fake_subprocess_run),
        ):
            worker_module.run_analyze("job-000")

        assert captured_kwargs["timeout"] == 900
