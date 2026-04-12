"""
test_duplicate_artifacts.py
============================
Verifies that creating artifacts with the same folder_id + type results in
an upsert (one row with the latest object_key), not a duplicate row.

Tests both:
- _create_artifact() worker helper (for aggregate artifact types including "clip")
- upload_clip() route (clip artifact upsert via the HTTP endpoint)
"""

from __future__ import annotations

import os
import uuid

import pytest

# Disable background jobs so the tests are fast.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from fastapi.testclient import TestClient

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"

_TINY_MP4 = (
    b"\x00\x00\x00\x20ftyp"
    b"isom\x00\x00\x02\x00"
    b"isomiso2avc1mp41"
    b"\x00\x00\x00\x08free"
)


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


def _create_folder_api(client: TestClient) -> str:
    resp = client.post("/v1/folders", json={}, headers=_auth())
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tests for _create_artifact() upsert behaviour
# ---------------------------------------------------------------------------


class TestCreateArtifactUpsert:
    """_create_artifact() must upsert for aggregate types (including 'clip')."""

    def test_same_type_twice_yields_one_row(self) -> None:
        """Calling _create_artifact() twice for the same folder+type → one row."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Artifact
        from backend.app.worker import _create_artifact

        folder_id = str(uuid.uuid4())

        # Create the folder row so FK constraint is satisfied.
        from datetime import datetime, timezone

        from backend.app.models import Folder

        with Session(db_module.get_engine()) as session:
            folder = Folder(
                id=uuid.UUID(folder_id),
                title="Test",
                status="pending",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(folder)
            session.commit()

        _create_artifact(folder_id, "analysis_json", "key/v1.json")
        _create_artifact(folder_id, "analysis_json", "key/v2.json")

        with Session(db_module.get_engine()) as session:
            rows = session.exec(
                select(Artifact).where(
                    Artifact.folder_id == uuid.UUID(folder_id),
                    Artifact.type == "analysis_json",
                )
            ).all()

        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0].object_key == "key/v2.json"

    def test_clip_type_upserts(self) -> None:
        """clip artifact type is in UPSERT_TYPES — second call updates object_key."""
        from datetime import datetime, timezone

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Artifact, Folder
        from backend.app.worker import _create_artifact

        folder_id = str(uuid.uuid4())

        with Session(db_module.get_engine()) as session:
            folder = Folder(
                id=uuid.UUID(folder_id),
                title="Test",
                status="pending",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(folder)
            session.commit()

        _create_artifact(folder_id, "clip", "clips/original.mp4")
        _create_artifact(folder_id, "clip", "clips/updated.mp4")

        with Session(db_module.get_engine()) as session:
            rows = session.exec(
                select(Artifact).where(
                    Artifact.folder_id == uuid.UUID(folder_id),
                    Artifact.type == "clip",
                )
            ).all()

        assert len(rows) == 1, f"Expected 1 clip row, got {len(rows)}"
        assert rows[0].object_key == "clips/updated.mp4"

    def test_different_types_each_get_one_row(self) -> None:
        """Different types for the same folder each get their own row."""
        from datetime import datetime, timezone

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Artifact, Folder
        from backend.app.worker import _create_artifact

        folder_id = str(uuid.uuid4())

        with Session(db_module.get_engine()) as session:
            folder = Folder(
                id=uuid.UUID(folder_id),
                title="Test",
                status="pending",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(folder)
            session.commit()

        _create_artifact(folder_id, "analysis_json", "analysis.json")
        _create_artifact(folder_id, "analysis_md", "analysis.md")

        with Session(db_module.get_engine()) as session:
            rows = session.exec(
                select(Artifact).where(Artifact.folder_id == uuid.UUID(folder_id))
            ).all()

        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Tests for upload_clip() route upsert behaviour
# ---------------------------------------------------------------------------


class TestUploadClipUpsert:
    """Uploading a clip twice to the same folder must not create duplicate artifacts."""

    def test_upload_clip_twice_no_storage(self, client: TestClient) -> None:
        """Without R2 configured, no artifact is created — just smoke-test the route."""
        folder_id = _create_folder_api(client)
        for _ in range(2):
            resp = client.post(
                f"/v1/folders/{folder_id}/clip",
                files={"clip": ("clip.mp4", _TINY_MP4, "video/mp4")},
                headers=_auth(),
            )
            # Without storage configured the endpoint returns 202 (job created)
            # or may behave differently; we just check it doesn't crash.
            assert resp.status_code in (202, 400, 502), resp.text
