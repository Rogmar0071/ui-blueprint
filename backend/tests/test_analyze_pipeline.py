"""
Analyze Pipeline v1 tests (segment-based)
==========================================
Tests for the resumable, segment-based Pipeline v1 analyze pipeline.

Covers:
  - Segment manifest stage: sets baseline_segments + cursor=0
  - Baseline_segments step: advances segment cursor
  - Resume from checkpoint after simulated worker restart
  - Re-enqueue loop continues until the pipeline is done
  - Streaming download (get_object_to_file) is used, not get_object_bytes
  - Per-user options: defaults, persistence, conditional optional stage
  - Segment manifest bounded by MAX_SEGMENTS
  - Baseline artifacts created for all segments
  - Job marks succeeded once baseline + aggregate done (no optional needed)
  - Optional analyze_optional job auto-enqueued when options.enabled=true
  - Per-segment optional artifacts produced by analyze_optional
  - Resume of analyze_optional from mid-segment cursor
  - Idempotency: rerun optional job produces stable artifact keys
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest

# Must be set before importing the app so modules pick them up at import time.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")


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

    db_module.reset_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_folder_and_job(clip_object_key: str = "folders/test/clip.mp4"):
    """Insert a Folder + an analyze Job row, return (folder_id, job_id) as strings."""
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Folder, Job

    folder_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with Session(get_engine()) as session:
        folder = Folder(id=folder_id, clip_object_key=clip_object_key)
        session.add(folder)
        job = Job(id=job_id, folder_id=folder_id, type="analyze")
        session.add(job)
        session.commit()

    return str(folder_id), str(job_id)


def _get_job(job_id: str):
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    with Session(get_engine()) as session:
        return session.get(Job, uuid.UUID(job_id))


def _set_job_checkpoint(job_id: str, **kwargs):
    """Directly set checkpoint fields on a Job row."""
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    with Session(get_engine()) as session:
        job = session.get(Job, uuid.UUID(job_id))
        for k, v in kwargs.items():
            setattr(job, k, v)
        session.add(job)
        session.commit()


def _fake_get_to_file(object_key: str, local_path: str) -> bool:
    """Write a tiny stub file to local_path, simulating a streamed download."""
    with open(local_path, "wb") as fh:
        fh.write(b"\x00" * 16)
    return True


def _noop_upload_bytes(folder_id, filename, data, content_type="application/octet-stream"):
    return f"folders/{folder_id}/{filename}"


# ---------------------------------------------------------------------------
# Test: pipeline advances cursor and stage
# ---------------------------------------------------------------------------


class TestAnalyzePipelineAdvancesCursorAndStage:
    """After the manifest step, the checkpoint must show stage=baseline_segments
    and analyze_cursor_segment_index=0."""

    def test_manifest_sets_baseline_stage_and_zero_cursor(self, tmp_path):
        """run_analyze_step with no checkpoint runs the manifest stage and sets
        analyze_stage='baseline_segments', analyze_cursor_segment_index=0."""
        folder_id, job_id = _make_folder_and_job()

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch(
                "backend.app.worker._probe_video_info",
                return_value=(30.0, 30.0),  # 30 s → 3 segments at 10 s each
            ),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_SEGMENT_SIZE_S": "10"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job is not None
        assert job.analyze_stage == "baseline_segments"
        assert job.analyze_cursor_segment_index == 0
        assert job.analyze_clip_object_key == "folders/test/clip.mp4"
        assert job.progress == 20
        assert job.status == "running"

        # The manifest must have been uploaded.
        manifest_key = f"folders/{folder_id}/segments/manifest.json"
        assert manifest_key in uploaded, (
            f"manifest.json not uploaded; keys={list(uploaded.keys())}"
        )
        import json as _json

        manifest = _json.loads(uploaded[manifest_key])
        assert manifest["schema_version"] == "v1"
        assert len(manifest["segments"]) == 3

    def test_baseline_segments_step_advances_cursor(self, tmp_path):
        """A baseline_segments step must advance analyze_cursor_segment_index."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()

        # Build a small manifest with 5 segments.
        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(5)
        ]
        manifest = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": 50000,
            "segment_size_s": 10,
            "segments": segments,
        }
        manifest_bytes = _json.dumps(manifest).encode()

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        uploaded: dict = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        with (
            patch(
                "backend.app.storage.get_object_bytes",
                return_value=manifest_bytes,
            ),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job.analyze_cursor_segment_index is not None
        assert job.analyze_cursor_segment_index > 0  # cursor advanced
        assert 20 <= job.progress <= 80


# ---------------------------------------------------------------------------
# Test: pipeline resumes from checkpoint
# ---------------------------------------------------------------------------


class TestAnalyzePipelineResumesFromCheckpoint:
    """When a checkpoint exists (simulating a worker restart), the pipeline
    continues from where it left off."""

    def test_resumes_from_mid_baseline_checkpoint(self, tmp_path):
        """If analyze_stage='baseline_segments' and cursor=3 (of 6), the next step
        starts from segment index 3, not 0."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()

        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(6)
        ]
        manifest = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": 60000,
            "segment_size_s": 10,
            "segments": segments,
        }
        manifest_bytes = _json.dumps(manifest).encode()

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=3,  # 3 of 6 processed
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        uploaded_keys: list[str] = []

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded_keys.append(key)
            return key

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        # Cursor must advance from 3.
        assert job.analyze_cursor_segment_index > 3

        # Only segments 3+ should have been uploaded.
        baseline_uploaded = [k for k in uploaded_keys if "baseline.json" in k]
        for key in baseline_uploaded:
            # segment indices in uploaded keys should be 3, 4, or 5.
            assert any(f"seg_{i:04d}_" in key for i in range(3, 6)), (
                f"Unexpected segment uploaded: {key}"
            )

    def test_skips_manifest_when_stage_is_baseline_segments(self):
        """If analyze_stage is already 'baseline_segments', the manifest stage must
        not run again."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()

        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(3)
        ]
        manifest = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": 30000,
            "segment_size_s": 10,
            "segments": segments,
        }
        manifest_bytes = _json.dumps(manifest).encode()

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        manifest_stage_calls: list[str] = []

        def spy_manifest(job_id_, folder_id_, job_):
            manifest_stage_calls.append(job_id_)

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker._analyze_manifest", side_effect=spy_manifest),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        assert manifest_stage_calls == [], (
            "manifest stage should NOT run when stage=baseline_segments"
        )


# ---------------------------------------------------------------------------
# Test: pipeline re-enqueues until done
# ---------------------------------------------------------------------------


class TestAnalyzePipelineReenqueueUntilDone:
    """The pipeline must re-enqueue after each step and stop re-enqueueing only
    when the job transitions to succeeded."""

    def test_reenqueue_called_each_step_until_aggregate_complete(self, tmp_path):
        """Drive the pipeline from manifest through baseline_segments and verify
        enqueue_job is called the expected number of times and job ends succeeded."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()

        enqueue_calls: list[tuple] = []
        uploaded: dict[str, bytes] = {}

        def recording_enqueue(jid, jtype):
            enqueue_calls.append((jid, jtype))

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        # 2 segments × 10 s each = 20 s clip.
        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(2)
        ]
        manifest_obj = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": 20000,
            "segment_size_s": 10,
            "segments": segments,
        }
        manifest_bytes = _json.dumps(manifest_obj).encode()

        def fake_get_bytes(object_key):
            # Return manifest when asked; return None otherwise.
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
            patch(
                "backend.app.worker._probe_video_info",
                return_value=(20.0, 30.0),
            ),
            patch("backend.app.worker.enqueue_job", side_effect=recording_enqueue),
            patch.dict(
                os.environ,
                {"ANALYZE_SEGMENT_SIZE_S": "10", "ANALYZE_STEP_MAX_SECONDS": "60"},
            ),
        ):
            from backend.app.worker import run_analyze_step

            # Step 1: manifest → stage=baseline_segments
            run_analyze_step(job_id)
            # Step 2: baseline_segments (all 2 segments in one step) → stage=aggregate
            run_analyze_step(job_id)
            # Step 3: aggregate → succeeded
            run_analyze_step(job_id)

        # enqueue_job must have been called for manifest + baseline_segments steps
        # (not after aggregate since the job is done).
        assert len(enqueue_calls) >= 2, (
            f"Expected ≥2 enqueue calls, got {len(enqueue_calls)}: {enqueue_calls}"
        )
        # All enqueues must be for the 'analyze' job type.
        for _, jtype in enqueue_calls:
            assert jtype == "analyze", f"Unexpected job type in enqueue: {jtype!r}"

        job = _get_job(job_id)
        assert job.status == "succeeded"
        assert job.progress == 100

        # analysis.json must have been uploaded.
        analysis_key = f"folders/{folder_id}/analysis/analysis.json"
        assert analysis_key in uploaded, (
            f"analysis.json not uploaded; keys={list(uploaded.keys())}"
        )


# ---------------------------------------------------------------------------
# Test: streaming download – get_object_to_file used, not get_object_bytes
# ---------------------------------------------------------------------------


class TestAnalyzeStreamingDownload:
    """The pipeline must use get_object_to_file (streaming) for the clip,
    never get_object_bytes (which would buffer the full clip in RAM)."""

    def test_streaming_download_does_not_buffer_entire_clip(self):
        """Verify get_object_to_file is called for clip download during manifest
        and that get_object_bytes is NOT called for any clip MP4 download."""
        folder_id, job_id = _make_folder_and_job(
            clip_object_key="folders/test/clip.mp4"
        )

        get_to_file_calls: list[str] = []
        get_bytes_calls: list[str] = []

        def spy_get_to_file(object_key, local_path):
            get_to_file_calls.append(object_key)
            with open(local_path, "wb") as fh:
                fh.write(b"\x00" * 16)
            return True

        def spy_get_bytes(object_key):
            get_bytes_calls.append(object_key)
            return b"\x00" * 16

        def noop_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            return f"folders/{folder_id_}/{filename}"

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=spy_get_to_file),
            patch("backend.app.storage.get_object_bytes", side_effect=spy_get_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=noop_upload),
            patch("backend.app.worker._probe_video_info", return_value=(10.0, 30.0)),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_SEGMENT_SIZE_S": "10"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        # get_object_to_file must have been called for the clip.
        clip_key = "folders/test/clip.mp4"
        assert any(clip_key in k for k in get_to_file_calls), (
            f"Expected get_object_to_file to be called for clip; calls={get_to_file_calls}"
        )

        # get_object_bytes must NOT have been called for the clip MP4.
        mp4_bytes_calls = [k for k in get_bytes_calls if k.endswith(".mp4")]
        assert mp4_bytes_calls == [], (
            f"get_object_bytes was called for clip MP4 (memory-buffering!): {mp4_bytes_calls}"
        )

    def test_baseline_segments_stage_does_not_buffer_clip(self):
        """The baseline_segments stage reads manifest bytes (JSON) but must NOT
        call get_object_bytes for any .mp4 file."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()
        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(3)
        ]
        manifest = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": 30000,
            "segment_size_s": 10,
            "segments": segments,
        }
        manifest_bytes = _json.dumps(manifest).encode()

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        buffering_calls: list[str] = []

        def spy_get_bytes(object_key):
            buffering_calls.append(object_key)
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        with (
            patch("backend.app.storage.get_object_bytes", side_effect=spy_get_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        mp4_calls = [k for k in buffering_calls if k.endswith(".mp4")]
        assert mp4_calls == [], (
            f"get_object_bytes called for .mp4 in baseline_segments stage: {mp4_calls}"
        )


# ---------------------------------------------------------------------------
# Test: per-user options – API, persistence, and conditional execution
# ---------------------------------------------------------------------------


class TestAnalyzeOptions:
    """Tests covering the optional additional analysis feature:
      - Default path unchanged when options are omitted
      - Options are persisted in the DB and survive worker restarts
      - Optional analyses only run when enabled
      - API accepts and returns all 5 toggles
    """

    # -- helpers -------------------------------------------------------------

    def _make_folder(self):
        """Create a folder with a clip_object_key and return folder_id (str)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import Folder

        folder_id = uuid.uuid4()
        with Session(get_engine()) as session:
            folder = Folder(id=folder_id, clip_object_key="folders/test/clip.mp4")
            session.add(folder)
            session.commit()
        return str(folder_id)

    def _make_manifest_bytes(self, folder_id: str, n_segments: int = 2) -> bytes:
        import json as _json

        segments = [
            {
                "segment_id": f"seg_{i:04d}_{i*10000}_{(i+1)*10000}",
                "index": i,
                "t0_ms": i * 10000,
                "t1_ms": (i + 1) * 10000,
            }
            for i in range(n_segments)
        ]
        manifest = {
            "schema_version": "v1",
            "folder_id": folder_id,
            "clip_object_key": "folders/test/clip.mp4",
            "duration_ms": n_segments * 10000,
            "segment_size_s": 10,
            "segments": segments,
        }
        return _json.dumps(manifest).encode()

    # -- test 1 --------------------------------------------------------------

    def test_default_analyze_path_unchanged_when_options_omitted(self):
        """When no options are supplied, pipeline runs manifest → baseline_segments →
        aggregate and the job reaches succeeded with progress=100, without running
        any optional analyses."""

        folder_id, job_id = _make_folder_and_job()
        manifest_bytes = self._make_manifest_bytes(folder_id, n_segments=2)
        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        def fake_get_bytes(object_key):
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        enqueue_calls: list[tuple] = []

        def recording_enqueue(jid, jtype):
            enqueue_calls.append((jid, jtype))

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
            patch("backend.app.worker._probe_video_info", return_value=(20.0, 30.0)),
            patch("backend.app.worker.enqueue_job", side_effect=recording_enqueue),
            patch.dict(
                os.environ,
                {"ANALYZE_SEGMENT_SIZE_S": "10", "ANALYZE_STEP_MAX_SECONDS": "60"},
            ),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)   # manifest → baseline_segments
            run_analyze_step(job_id)   # baseline_segments → aggregate
            run_analyze_step(job_id)   # aggregate → succeeded

        job = _get_job(job_id)
        assert job.status == "succeeded"
        assert job.progress == 100
        # analyze_options must remain None (not set by pipeline when absent).
        assert job.analyze_options is None

        # analyze_optional must NOT have been enqueued.
        optional_enqueues = [t for (_, t) in enqueue_calls if t == "analyze_optional"]
        assert optional_enqueues == [], (
            "analyze_optional should not be enqueued when options omitted"
        )

    # -- test 2 --------------------------------------------------------------

    def test_options_are_persisted_and_survive_retries(self, tmp_path):
        """Options stored at enqueue time must still be readable after a
        simulated worker restart (i.e., fresh DB read)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import Folder, Job

        folder_id = uuid.uuid4()
        job_id = uuid.uuid4()
        opts = {
            "additional_analysis": {
                "enabled": True,
                "keyframes": True,
                "ocr": False,
                "transcript": False,
                "events": True,
                "segment_summaries": False,
            }
        }

        with Session(get_engine()) as session:
            folder = Folder(id=folder_id, clip_object_key="folders/test/clip.mp4")
            session.add(folder)
            job = Job(
                id=job_id,
                folder_id=folder_id,
                type="analyze",
                analyze_options=opts,
            )
            session.add(job)
            session.commit()

        # Simulate worker restart: fresh read from DB.
        from backend.app.worker import _get_analyze_options

        loaded_job = _get_job(str(job_id))
        options_after_restart = _get_analyze_options(loaded_job)

        assert options_after_restart["additional_analysis"]["enabled"] is True
        assert options_after_restart["additional_analysis"]["keyframes"] is True
        assert options_after_restart["additional_analysis"]["ocr"] is False
        assert options_after_restart["additional_analysis"]["events"] is True
        assert options_after_restart["additional_analysis"]["segment_summaries"] is False

    # -- test 3 --------------------------------------------------------------

    def test_optional_analyze_not_enqueued_when_disabled(self):
        """When analyze completes with additional_analysis.enabled=false, the
        analyze_optional job must NOT be auto-enqueued."""

        folder_id, job_id = _make_folder_and_job()
        manifest_bytes = self._make_manifest_bytes(folder_id, n_segments=1)

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="aggregate",
            analyze_clip_object_key="folders/test/clip.mp4",
            analyze_options={
                "additional_analysis": {
                    "enabled": False,
                    "keyframes": True,  # toggled on but master switch is off
                }
            },
        )

        enqueue_calls: list[tuple] = []

        def recording_enqueue(jid, jtype):
            enqueue_calls.append((jid, jtype))

        def fake_get_bytes(object_key):
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        with (
            patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker.enqueue_job", side_effect=recording_enqueue),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job.status == "succeeded"

        optional_enqueues = [t for (_, t) in enqueue_calls if t == "analyze_optional"]
        assert optional_enqueues == [], (
            "analyze_optional must not be enqueued when enabled=false"
        )

    # -- test 4 --------------------------------------------------------------

    def test_pipeline_with_options_persisted_via_api(self):
        """POST /v1/folders/{id}/jobs with options must persist them on the Job
        row so the worker can read them."""
        from fastapi.testclient import TestClient

        import backend.app.main as m
        from backend.app.main import app

        token = "test-secret-key"
        m.API_KEY = token
        client = TestClient(app, raise_server_exceptions=True)

        folder_resp = client.post(
            "/v1/folders",
            json={"title": "Test folder"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert folder_resp.status_code == 201
        folder_id = folder_resp.json()["id"]

        # Enqueue an analyze job WITH options (all 5 toggles).
        job_resp = client.post(
            f"/v1/folders/{folder_id}/jobs",
            json={
                "type": "analyze",
                "options": {
                    "additional_analysis": {
                        "enabled": True,
                        "keyframes": True,
                        "ocr": False,
                        "transcript": True,
                        "events": True,
                        "segment_summaries": False,
                    }
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert job_resp.status_code == 202, job_resp.text
        body = job_resp.json()
        assert "job" in body
        returned_options = body["job"]["options"]
        assert returned_options is not None, "options not returned in response"
        aa = returned_options["additional_analysis"]
        assert aa["enabled"] is True
        assert aa["keyframes"] is True
        assert aa["transcript"] is True
        assert aa["events"] is True
        assert aa["segment_summaries"] is False

        # Verify options are persisted in DB.
        job_id = body["job"]["id"]
        loaded = _get_job(job_id)
        assert loaded.analyze_options is not None
        assert loaded.analyze_options["additional_analysis"]["enabled"] is True
        assert loaded.analyze_options["additional_analysis"]["events"] is True

    def test_unknown_options_key_returns_400(self):
        """POSTing an unknown key inside options must return 400."""
        from fastapi.testclient import TestClient

        import backend.app.main as m
        from backend.app.main import app

        token = "test-secret-key"
        m.API_KEY = token
        client = TestClient(app, raise_server_exceptions=True)

        folder_resp = client.post(
            "/v1/folders",
            json={"title": "Test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        folder_id = folder_resp.json()["id"]

        resp = client.post(
            f"/v1/folders/{folder_id}/jobs",
            json={
                "type": "analyze",
                "options": {
                    "unknown_option": True,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# New tests: segment manifest, baseline artifacts, job success, optional job
# ---------------------------------------------------------------------------


def _make_manifest_bytes_for_folder(
    folder_id: str, n_segments: int, segment_size_s: int = 10
) -> bytes:
    """Helper: generate a manifest JSON bytes for tests."""
    import json as _json

    segments = [
        {
            "segment_id": (
                f"seg_{i:04d}_{i * segment_size_s * 1000}_{(i+1) * segment_size_s * 1000}"
            ),
            "index": i,
            "t0_ms": i * segment_size_s * 1000,
            "t1_ms": (i + 1) * segment_size_s * 1000,
        }
        for i in range(n_segments)
    ]
    manifest = {
        "schema_version": "v1",
        "folder_id": folder_id,
        "clip_object_key": "folders/test/clip.mp4",
        "duration_ms": n_segments * segment_size_s * 1000,
        "segment_size_s": segment_size_s,
        "segments": segments,
    }
    return _json.dumps(manifest).encode()


class TestSegmentManifest:
    """Tests for the segment manifest generation stage."""

    def test_manifest_bounded_by_max_segments(self, tmp_path):
        """A clip with more time than MAX_SEGMENTS × SEGMENT_SIZE_S must be
        capped at MAX_SEGMENTS segments."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()
        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        # 1000 s clip, 10 s segments → would produce 100 segments; cap to 5.
        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker._probe_video_info", return_value=(1000.0, 30.0)),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(
                os.environ,
                {"ANALYZE_SEGMENT_SIZE_S": "10", "ANALYZE_MAX_SEGMENTS": "5"},
            ),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        manifest_key = f"folders/{folder_id}/segments/manifest.json"
        assert manifest_key in uploaded
        manifest = _json.loads(uploaded[manifest_key])
        assert len(manifest["segments"]) == 5, (
            f"Expected 5 segments (MAX_SEGMENTS cap), got {len(manifest['segments'])}"
        )

    def test_manifest_segment_ids_are_deterministic(self, tmp_path):
        """Segment IDs must follow the scheme seg_{index:04d}_{t0_ms}_{t1_ms}."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()
        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker._probe_video_info", return_value=(20.0, 30.0)),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_SEGMENT_SIZE_S": "10"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        manifest = _json.loads(uploaded[f"folders/{folder_id}/segments/manifest.json"])
        assert manifest["segments"][0]["segment_id"] == "seg_0000_0_10000"
        assert manifest["segments"][1]["segment_id"] == "seg_0001_10000_20000"

    def test_baseline_artifacts_created_for_all_segments(self):
        """All segments in the manifest must get a baseline.json artifact."""

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import Artifact

        folder_id, job_id = _make_folder_and_job()
        n_segments = 4
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        enqueue_calls: list = []

        def recording_enqueue(jid, jtype):
            enqueue_calls.append((jid, jtype))
            # We drive manually; do not actually re-enqueue.

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job", side_effect=recording_enqueue),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            # All 4 segments should fit in one step under 60 s budget.
            run_analyze_step(job_id)

        # All 4 baseline.json files must have been uploaded.
        baseline_keys = [k for k in uploaded if "baseline.json" in k]
        assert len(baseline_keys) == n_segments, (
            f"Expected {n_segments} baseline uploads, got {len(baseline_keys)}: {baseline_keys}"
        )

        # Check DB artifacts.
        with Session(get_engine()) as session:
            artifacts = session.exec(
                select(Artifact)
                .where(Artifact.folder_id == uuid.UUID(folder_id))
                .where(Artifact.type == "baseline_segment_json")
            ).all()
        assert len(artifacts) == n_segments, (
            f"Expected {n_segments} baseline_segment_json artifacts, got {len(artifacts)}"
        )


class TestJobSucceedsAfterAggregate:
    """Job must reach succeeded=100 after aggregate even when optional analyses
    are disabled."""

    def test_job_marks_succeeded_once_aggregate_complete(self):
        """When the aggregate stage finishes, the job must be status='succeeded'
        with progress=100 regardless of optional toggles."""



        folder_id, job_id = _make_folder_and_job()
        n_segments = 2
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="aggregate",
            analyze_cursor_segment_index=n_segments,
            analyze_clip_object_key="folders/test/clip.mp4",
            analyze_options=None,  # optional disabled
        )

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        enqueue_calls: list = []

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch(
                "backend.app.worker.enqueue_job",
                side_effect=lambda jid, jtype: enqueue_calls.append((jid, jtype)),
            ),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job.status == "succeeded"
        assert job.progress == 100

        # analysis.json must have been created.
        analysis_keys = [k for k in uploaded if "analysis.json" in k]
        assert analysis_keys, f"analysis.json not uploaded; got {list(uploaded.keys())}"

        # analyze_optional must NOT be enqueued.
        optional_enqueues = [t for (_, t) in enqueue_calls if t == "analyze_optional"]
        assert optional_enqueues == []


class TestOptionalAnalyzeJob:
    """Tests for the analyze_optional job type."""

    def _make_optional_job(self, folder_id: str, options: dict):
        """Insert an analyze_optional Job row and return job_id (str)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import Job

        job_id = uuid.uuid4()
        with Session(get_engine()) as session:
            job = Job(
                id=job_id,
                folder_id=uuid.UUID(folder_id),
                type="analyze_optional",
                analyze_options=options,
            )
            session.add(job)
            session.commit()
        return str(job_id)

    def test_optional_job_auto_enqueued_when_enabled(self):
        """When aggregate finishes with enabled=true, an analyze_optional job must
        be created in the DB and enqueued."""

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import Job

        folder_id, job_id = _make_folder_and_job()
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments=2)
        opts = {
            "additional_analysis": {
                "enabled": True,
                "keyframes": True,
                "ocr": False,
                "transcript": False,
                "events": False,
                "segment_summaries": False,
            }
        }

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="aggregate",
            analyze_clip_object_key="folders/test/clip.mp4",
            analyze_options=opts,
        )

        enqueued: list[tuple] = []

        def recording_enqueue(jid, jtype):
            enqueued.append((jid, jtype))

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker.enqueue_job", side_effect=recording_enqueue),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        # The analyze job must have succeeded.
        job = _get_job(job_id)
        assert job.status == "succeeded"

        # An analyze_optional job must have been enqueued.
        optional_types = [t for (_, t) in enqueued if t == "analyze_optional"]
        assert optional_types, (
            "analyze_optional job was not enqueued after aggregate with enabled=true"
        )

        # Verify the analyze_optional job row was created in the DB.
        with Session(get_engine()) as session:
            opt_job = session.exec(
                select(Job)
                .where(Job.folder_id == uuid.UUID(folder_id))
                .where(Job.type == "analyze_optional")
            ).first()
        assert opt_job is not None, "analyze_optional job row not found in DB"
        assert opt_job.analyze_options is not None
        assert opt_job.analyze_options["additional_analysis"]["enabled"] is True

    def test_per_segment_optional_artifacts_produced(self):
        """run_analyze_optional_step must produce per-segment artifacts for each
        enabled toggle."""

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import Artifact

        folder_id, _ = _make_folder_and_job()
        n_segments = 3
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)
        opts = {
            "additional_analysis": {
                "enabled": True,
                "keyframes": True,
                "ocr": False,
                "transcript": True,
                "events": False,
                "segment_summaries": False,
            }
        }
        job_id = self._make_optional_job(folder_id, opts)

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        def fake_get_bytes(object_key):
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        with (
            patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_optional_step

            run_analyze_optional_step(job_id)

        job = _get_job(job_id)
        assert job.status == "succeeded"
        assert job.progress == 100

        # keyframes.json and transcript.json for each segment must be uploaded.
        kf_keys = [k for k in uploaded if k.endswith("keyframes.json")]
        tr_keys = [k for k in uploaded if k.endswith("transcript.json")]
        ocr_keys = [k for k in uploaded if k.endswith("ocr.json")]

        assert len(kf_keys) == n_segments, (
            f"Expected {n_segments} keyframes uploads, got {len(kf_keys)}"
        )
        assert len(tr_keys) == n_segments, (
            f"Expected {n_segments} transcript uploads, got {len(tr_keys)}"
        )
        assert ocr_keys == [], f"ocr must be disabled; got {ocr_keys}"

        # DB artifacts.
        with Session(get_engine()) as session:
            kf_artifacts = session.exec(
                select(Artifact)
                .where(Artifact.folder_id == uuid.UUID(folder_id))
                .where(Artifact.type == "keyframes_segment_json")
            ).all()
        assert len(kf_artifacts) == n_segments

    def test_optional_job_resumes_from_cursor_after_crash(self):
        """Simulate a crash mid-way through optional analyses; verify that restarting
        continues from the saved cursor index and does not reprocess earlier segments."""

        folder_id, _ = _make_folder_and_job()
        n_segments = 4
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)
        opts = {
            "additional_analysis": {
                "enabled": True,
                "keyframes": True,
                "ocr": False,
                "transcript": False,
                "events": False,
                "segment_summaries": False,
            }
        }
        job_id = self._make_optional_job(folder_id, opts)

        # Pre-set cursor as if 2 of 4 segments were already processed.
        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="segments",
            analyze_cursor_segment_index=2,
            analyze_options=opts,
        )

        uploaded_keys: list[str] = []

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded_keys.append(key)
            return key

        def fake_get_bytes(object_key):
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        with (
            patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_optional_step

            run_analyze_optional_step(job_id)

        job = _get_job(job_id)
        assert job.status == "succeeded"

        # Only segments 2 and 3 should have been uploaded (not 0 or 1).
        kf_keys = [k for k in uploaded_keys if k.endswith("keyframes.json")]
        assert len(kf_keys) == 2, (
            f"Expected 2 keyframes uploads (segments 2+3), got {kf_keys}"
        )
        for k in kf_keys:
            assert any(f"seg_{i:04d}_" in k for i in range(2, n_segments)), (
                f"Unexpected segment key uploaded: {k}"
            )

    def test_optional_artifact_keys_are_idempotent(self):
        """Running analyze_optional twice on the same segments must produce the
        same deterministic artifact keys (no duplicates; safe overwrites)."""

        folder_id, _ = _make_folder_and_job()
        n_segments = 2
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)
        opts = {
            "additional_analysis": {
                "enabled": True,
                "keyframes": True,
                "ocr": False,
                "transcript": False,
                "events": False,
                "segment_summaries": False,
            }
        }

        def fake_get_bytes(object_key):
            if "manifest.json" in object_key:
                return manifest_bytes
            return None

        def _run_once() -> set[str]:
            jid = self._make_optional_job(folder_id, opts)
            collected: list[str] = []

            def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
                key = f"folders/{folder_id_}/{filename}"
                collected.append(key)
                return key

            with (
                patch("backend.app.storage.get_object_bytes", side_effect=fake_get_bytes),
                patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
                patch("backend.app.worker.enqueue_job"),
                patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
            ):
                from backend.app.worker import run_analyze_optional_step

                run_analyze_optional_step(jid)

            return set(k for k in collected if k.endswith("keyframes.json"))

        run1_keys = _run_once()
        run2_keys = _run_once()

        assert run1_keys == run2_keys, (
            f"Artifact keys differ between runs – not idempotent!\n"
            f"Run 1: {sorted(run1_keys)}\nRun 2: {sorted(run2_keys)}"
        )
        assert len(run1_keys) == n_segments


# ---------------------------------------------------------------------------
# Test: baseline_segments produces real analysis data (non-stub)
# ---------------------------------------------------------------------------


class TestBaselineSegmentsNonStub:
    """When extract_segment is available and returns data, the uploaded
    baseline.json must contain an 'analysis' key with real data rather than
    falling back to the stub 'notes' key."""

    def test_baseline_json_contains_analysis_key_when_extractor_available(self):
        """Mock extract_segment to return a populated result and verify the
        uploaded baseline.json contains 'analysis' and not just 'notes'."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()
        n_segments = 2
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        fake_analysis = {
            "elements_catalog": [{"id": "el_0000", "type": "button"}],
            "chunks": [{"t0_ms": 0, "t1_ms": 10000, "events": []}],
            "events": [],
            "quality": {"detection_confidence": 0.8},
        }

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch(
                "ui_blueprint.extractor.extract_segment",
                return_value=fake_analysis,
            ),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        baseline_keys = [k for k in uploaded if k.endswith("baseline.json")]
        assert len(baseline_keys) == n_segments, (
            f"Expected {n_segments} baseline uploads, got {baseline_keys}"
        )

        for key in baseline_keys:
            data = _json.loads(uploaded[key])
            assert "analysis" in data, (
                f"baseline.json at {key} is still a stub – expected 'analysis' key. Got: {data}"
            )
            assert "notes" not in data, (
                f"baseline.json at {key} still contains stub 'notes' key. Got: {data}"
            )
            assert data["analysis"]["elements_catalog"] == fake_analysis["elements_catalog"]

    def test_baseline_json_falls_back_to_notes_when_extractor_raises(self):
        """If extract_segment raises, the baseline.json must contain the 'notes'
        fallback key and the pipeline must NOT stall (cursor still advances)."""
        import json as _json

        folder_id, job_id = _make_folder_and_job()
        n_segments = 2
        manifest_bytes = _make_manifest_bytes_for_folder(folder_id, n_segments)

        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="baseline_segments",
            analyze_cursor_segment_index=0,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        uploaded: dict[str, bytes] = {}

        def fake_upload(folder_id_, filename, data, content_type="application/octet-stream"):
            key = f"folders/{folder_id_}/{filename}"
            uploaded[key] = data
            return key

        with (
            patch("backend.app.storage.get_object_bytes", return_value=manifest_bytes),
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=fake_upload),
            patch("backend.app.worker.enqueue_job"),
            patch(
                "ui_blueprint.extractor.extract_segment",
                side_effect=RuntimeError("ffmpeg unavailable"),
            ),
            patch.dict(os.environ, {"ANALYZE_STEP_MAX_SECONDS": "60"}),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        # Even with extractor failure, all segments must be uploaded.
        baseline_keys = [k for k in uploaded if k.endswith("baseline.json")]
        assert len(baseline_keys) == n_segments, (
            f"Pipeline stalled: expected {n_segments} baselines, got {baseline_keys}"
        )

        # Cursor must have advanced.
        job = _get_job(job_id)
        assert job.analyze_cursor_segment_index is not None
        assert job.analyze_cursor_segment_index >= n_segments

        # Fallback notes key must be present.
        for key in baseline_keys:
            data = _json.loads(uploaded[key])
            assert "notes" in data, (
                f"Expected fallback 'notes' key in {key}. Got: {data}"
            )
