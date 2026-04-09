"""
Backend API tests
=================
Uses FastAPI's TestClient (via httpx) to validate core API behaviour.
Heavy processing (extraction) is disabled via BACKEND_DISABLE_JOBS=1.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

# Disable background extraction so tests are fast.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402  (import after env setup)

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    # Reload the module-level API_KEY so the app picks it up.
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_MP4 = (
    # Minimal valid-ish MP4 stub (not a real playable video, but enough for
    # the upload endpoint which only saves the bytes without playing it).
    b"\x00\x00\x00\x20ftyp"
    b"isom\x00\x00\x02\x00"
    b"isomiso2avc1mp41"
    b"\x00\x00\x00\x08free"
)


def _upload(client: TestClient, token: str = TOKEN, meta: str = "") -> dict:
    """Helper — POST to /v1/sessions and return parsed JSON response."""
    response = client.post(
        "/v1/sessions",
        files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
        data={"meta": meta},
        headers={"Authorization": f"Bearer {token}"},
    )
    return response


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_upload_requires_auth(self, client: TestClient) -> None:
        """POST /v1/sessions without auth must return 401."""
        response = client.post(
            "/v1/sessions",
            files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
        )
        assert response.status_code == 401

    def test_upload_wrong_token_returns_403(self, client: TestClient) -> None:
        response = _upload(client, token="wrong-token")
        assert response.status_code == 403

    def test_status_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent")
        assert response.status_code == 401

    def test_blueprint_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent/blueprint")
        assert response.status_code == 401

    def test_preview_index_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent/preview/index")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestUpload:
    def test_upload_creates_session(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta=json.dumps({"device": "Pixel 8"}))
        assert response.status_code == 201
        body = response.json()
        assert "session_id" in body
        assert body["status"] == "queued"

    def test_upload_saves_files(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta=json.dumps({"fps": 30}))
        session_id = response.json()["session_id"]
        sdir = tmp_path / "sessions" / session_id
        assert (sdir / "clip.mp4").exists()
        assert (sdir / "meta.json").exists()
        assert (sdir / "status.json").exists()

    def test_upload_status_is_queued(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client)
        session_id = response.json()["session_id"]
        status_path = tmp_path / "sessions" / session_id / "status.json"
        with status_path.open() as fh:
            status = json.load(fh)
        assert status["status"] == "queued"

    def test_upload_invalid_meta_json_returns_422(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta="not-valid-json{{{")
        assert response.status_code == 422

    def test_upload_empty_meta_is_ok(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta="")
        assert response.status_code == 201


# ---------------------------------------------------------------------------
# Status endpoint tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_queued(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"

    def test_status_nonexistent_returns_404(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        # A valid UUID that doesn't exist should return 404.
        response = client.get(
            "/v1/sessions/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Blueprint / preview endpoints (pre-completion states)
# ---------------------------------------------------------------------------


class TestBlueprintAndPreview:
    def test_blueprint_not_ready_returns_404(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/blueprint",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 404

    def test_preview_index_empty_when_no_previews(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/preview/index",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["files"] == []

    def test_preview_file_traversal_rejected(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/preview/../../../etc/passwd",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        # FastAPI routes will either not match or return 400/404.
        assert response.status_code in (400, 404, 422)

    def test_invalid_session_id_returns_400(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        for bad_id in ("../etc", "not-a-uuid", "../../../../etc/passwd"):
            response = client.get(
                f"/v1/sessions/{bad_id}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert response.status_code in (400, 404, 422), (
                f"Expected 400/404/422 for session_id={bad_id!r}, got {response.status_code}"
            )


# ---------------------------------------------------------------------------
# Root health check
# ---------------------------------------------------------------------------


class TestRoot:
    def test_root_returns_200(self, client: TestClient) -> None:
        """GET / must return 200 with no auth (used by Render health checks)."""
        response = client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["service"] == "ui-blueprint-backend"


# ---------------------------------------------------------------------------
# /api/chat endpoint
# ---------------------------------------------------------------------------


class TestChat:
    """Tests for POST /api/chat — auth enforcement + stub/OpenAI behaviour."""

    def test_chat_requires_auth_when_api_key_set(self, client: TestClient) -> None:
        """401 when Authorization header is missing and API_KEY is configured."""
        # The autouse _set_api_key fixture already sets API_KEY=TOKEN.
        response = client.post("/api/chat", json={"message": "hello"})
        assert response.status_code == 401

    def test_chat_wrong_token_returns_403(self, client: TestClient) -> None:
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 403

    def test_chat_missing_message_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/api/chat",
            json={"context": {}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    def test_chat_stub_reply_when_no_openai_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OPENAI_API_KEY is absent a deterministic stub reply is returned."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        response = client.post(
            "/api/chat",
            json={"message": "What is ui-blueprint?"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"]
        assert "Stub" in body["reply"]
        assert "tools_available" in body
        assert "domains.derive" in body["tools_available"]

    def test_chat_schema_version_present(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All successful chat responses include top-level schema_version."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from ui_blueprint.domain.ir import SCHEMA_VERSION

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["schema_version"] == SCHEMA_VERSION

    def test_chat_openai_success(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OPENAI_API_KEY is set and OpenAI responds, reply is returned."""
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-test")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Here is how ui-blueprint works."}}]
        }

        with patch("backend.app.chat_routes.httpx.Client") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.post.return_value = mock_response

            response = client.post(
                "/api/chat",
                json={"message": "How does this work?"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["reply"] == "Here is how ui-blueprint works."
        assert "tools_available" in body

    def test_chat_openai_timeout_returns_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OpenAI times out, /api/chat returns 502 ai_provider_error."""
        from unittest.mock import MagicMock, patch

        import httpx

        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-test")

        with patch("backend.app.chat_routes.httpx.Client") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.post.side_effect = httpx.TimeoutException("timed out")

            response = client.post(
                "/api/chat",
                json={"message": "hello"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "ai_provider_error"
        assert body["error"]["details"]["hint"] == "timeout"

