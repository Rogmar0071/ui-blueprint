"""
Analyze Pipeline v1 tests
=========================
Tests for the resumable, step-based analyze pipeline introduced in Pipeline v1.

Covers:
  - Cursor and stage advancement per step
  - Resume from checkpoint after simulated worker restart
  - Re-enqueue loop continues until the pipeline is done
  - Streaming download (get_object_to_file) is used, not get_object_bytes
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
    """After the prepare step, the checkpoint must show stage=frames and cursor=0."""

    def test_prepare_sets_frames_stage_and_zero_cursor(self, tmp_path):
        """run_analyze_step with no checkpoint runs the prepare stage and sets
        analyze_stage='frames', analyze_cursor_frame_index=0."""
        folder_id, job_id = _make_folder_and_job()

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch(
                "backend.app.worker._probe_video_info",
                return_value=(10.0, 30.0),  # 10 s, 30 fps native
            ),
            patch("backend.app.worker.enqueue_job"),  # swallow re-enqueue
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job is not None
        assert job.analyze_stage == "frames"
        assert job.analyze_cursor_frame_index == 0
        assert job.analyze_total_frames == 10  # ceil(10s * 1fps)
        assert job.analyze_clip_object_key == "folders/test/clip.mp4"
        assert job.progress == 20
        assert job.status == "running"

    def test_frames_step_advances_cursor(self, tmp_path):
        """A frames step must advance the cursor by the number of extracted frames."""
        folder_id, job_id = _make_folder_and_job()
        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="frames",
            analyze_cursor_frame_index=0,
            analyze_total_frames=10,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        def fake_extract(clip_path, start_time_s, frames_per_step, desired_fps,
                         out_dir, ffmpeg_exe, start_number):
            # Simulate producing 5 frame files.
            paths = []
            for i in range(frames_per_step):
                p = os.path.join(out_dir, f"frame_{start_number + i:05d}.jpg")
                with open(p, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xe0" + bytes(i))
                paths.append(p)
            return paths

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker._extract_frames_chunk", side_effect=fake_extract),
            patch("backend.app.worker.enqueue_job"),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        job = _get_job(job_id)
        assert job.analyze_cursor_frame_index == 5
        # Progress is between 20 and 80.
        assert 20 <= job.progress <= 80


# ---------------------------------------------------------------------------
# Test: pipeline resumes from checkpoint
# ---------------------------------------------------------------------------


class TestAnalyzePipelineResumesFromCheckpoint:
    """When a checkpoint exists (simulating a worker restart), the pipeline
    continues from where it left off."""

    def test_resumes_from_mid_frames_checkpoint(self, tmp_path):
        """If analyze_stage='frames' and cursor=5, the next step starts
        extracting from frame index 5, not from 0."""
        folder_id, job_id = _make_folder_and_job()
        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="frames",
            analyze_cursor_frame_index=5,
            analyze_total_frames=10,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        captured_start_number = {}

        def fake_extract(clip_path, start_time_s, frames_per_step, desired_fps,
                         out_dir, ffmpeg_exe, start_number):
            captured_start_number["value"] = start_number
            paths = []
            for i in range(frames_per_step):
                p = os.path.join(out_dir, f"frame_{start_number + i:05d}.jpg")
                with open(p, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xe0")
                paths.append(p)
            return paths

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker._extract_frames_chunk", side_effect=fake_extract),
            patch("backend.app.worker.enqueue_job"),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        # Frame extraction must start from cursor position 5, not 0.
        assert captured_start_number["value"] == 5

        job = _get_job(job_id)
        # Cursor must advance from 5.
        assert job.analyze_cursor_frame_index > 5

    def test_skips_prepare_when_stage_is_frames(self):
        """If analyze_stage is already 'frames', prepare must not run again."""
        folder_id, job_id = _make_folder_and_job()
        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="frames",
            analyze_cursor_frame_index=0,
            analyze_total_frames=3,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        prepare_calls = []

        def spy_prepare(job_id_, folder_id_):
            prepare_calls.append(job_id_)

        def fake_extract(clip_path, start_time_s, frames_per_step, desired_fps,
                         out_dir, ffmpeg_exe, start_number):
            # Produce fewer frames than total so we know it ran frames stage.
            p = os.path.join(out_dir, f"frame_{start_number:05d}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0")
            return [p]

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker._analyze_prepare", side_effect=spy_prepare),
            patch("backend.app.worker._extract_frames_chunk", side_effect=fake_extract),
            patch("backend.app.worker.enqueue_job"),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        assert prepare_calls == [], "prepare should NOT run when stage=frames"


# ---------------------------------------------------------------------------
# Test: pipeline re-enqueues until done
# ---------------------------------------------------------------------------


class TestAnalyzePipelineReenqueueUntilDone:
    """The pipeline must re-enqueue after each step and stop re-enqueueing only
    when the job transitions to succeeded."""

    def test_reenqueue_called_each_step_until_summarize_complete(self, tmp_path):
        """Drive the pipeline from prepare through all frames steps and verify
        enqueue_job is called the expected number of times."""
        folder_id, job_id = _make_folder_and_job()

        frames_per_step = 3
        total_frames = 6  # two frames steps needed
        enqueue_calls: list[tuple] = []

        def recording_enqueue(jid, jtype):
            enqueue_calls.append((jid, jtype))
            # In test mode we drive steps manually; swallow the real enqueue.

        def fake_extract(clip_path, start_time_s, frames_per_step, desired_fps,
                         out_dir, ffmpeg_exe, start_number):
            paths = []
            for i in range(frames_per_step):
                p = os.path.join(out_dir, f"frame_{start_number + i:05d}.jpg")
                with open(p, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xe0")
                paths.append(p)
            return paths

        def fake_summarize_extract(job_id_, folder_id_, job_):
            # Simulate a fast summarize: write analysis.json and mark succeeded.
            from backend.app.worker import _update_folder_status, _update_job

            _update_job(job_id_, status="succeeded", progress=100)
            _update_folder_status(folder_id_, "done")

        patcher_get = patch(
            "backend.app.storage.get_object_to_file", side_effect=_fake_get_to_file
        )
        patcher_upload = patch(
            "backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes
        )
        patcher_extract = patch(
            "backend.app.worker._extract_frames_chunk", side_effect=fake_extract
        )
        patcher_probe = patch(
            "backend.app.worker._probe_video_info",
            return_value=(float(total_frames), 1.0),
        )
        patcher_summarize = patch(
            "backend.app.worker._analyze_summarize", side_effect=fake_summarize_extract
        )
        patcher_enqueue = patch(
            "backend.app.worker.enqueue_job", side_effect=recording_enqueue
        )

        monkeypatch_fps = patch.dict(
            os.environ,
            {"ANALYZE_FRAMES_PER_STEP": str(frames_per_step), "ANALYZE_FRAME_FPS": "1"},
        )

        with (
            patcher_get,
            patcher_upload,
            patcher_extract,
            patcher_probe,
            patcher_summarize,
            patcher_enqueue,
            monkeypatch_fps,
        ):
            from backend.app.worker import run_analyze_step

            # Step 1: prepare
            run_analyze_step(job_id)
            # Step 2: frames batch 1 (cursor 0→3)
            run_analyze_step(job_id)
            # Step 3: frames batch 2 (cursor 3→6 → triggers summarize)
            run_analyze_step(job_id)
            # Step 4: summarize
            run_analyze_step(job_id)

        # enqueue_job must have been called at least 3 times (prepare + 2 frame batches).
        # It is NOT called again after summarize succeeds.
        assert len(enqueue_calls) >= 3, (
            f"Expected ≥3 enqueue calls, got {len(enqueue_calls)}: {enqueue_calls}"
        )
        for _, jtype in enqueue_calls:
            assert jtype == "analyze"

        job = _get_job(job_id)
        assert job.status == "succeeded"
        assert job.progress == 100


# ---------------------------------------------------------------------------
# Test: streaming download – get_object_to_file used, not get_object_bytes
# ---------------------------------------------------------------------------


class TestAnalyzeStreamingDownload:
    """The pipeline must use get_object_to_file (streaming) for the clip,
    never get_object_bytes (which would buffer the full clip in RAM)."""

    def test_streaming_download_does_not_buffer_entire_clip(self):
        """Verify get_object_to_file is called for clip download during prepare
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

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=spy_get_to_file),
            patch("backend.app.storage.get_object_bytes", side_effect=spy_get_bytes),
            patch("backend.app.worker._probe_video_info", return_value=(5.0, 30.0)),
            patch("backend.app.worker.enqueue_job"),
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

    def test_frames_stage_uses_streaming_download(self):
        """The frames stage also uses get_object_to_file for the clip."""
        folder_id, job_id = _make_folder_and_job()
        _set_job_checkpoint(
            job_id,
            status="running",
            analyze_stage="frames",
            analyze_cursor_frame_index=0,
            analyze_total_frames=5,
            analyze_clip_object_key="folders/test/clip.mp4",
        )

        streaming_calls: list[str] = []
        buffering_calls: list[str] = []

        def spy_to_file(object_key, local_path):
            streaming_calls.append(object_key)
            with open(local_path, "wb") as fh:
                fh.write(b"\x00" * 16)
            return True

        def spy_bytes(object_key):
            buffering_calls.append(object_key)
            return b"\x00" * 16

        def fake_extract(clip_path, start_time_s, frames_per_step, desired_fps,
                         out_dir, ffmpeg_exe, start_number):
            p = os.path.join(out_dir, f"frame_{start_number:05d}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0")
            return [p]

        with (
            patch("backend.app.storage.get_object_to_file", side_effect=spy_to_file),
            patch("backend.app.storage.get_object_bytes", side_effect=spy_bytes),
            patch("backend.app.storage.upload_bytes", side_effect=_noop_upload_bytes),
            patch("backend.app.worker._extract_frames_chunk", side_effect=fake_extract),
            patch("backend.app.worker.enqueue_job"),
        ):
            from backend.app.worker import run_analyze_step

            run_analyze_step(job_id)

        assert any(".mp4" in k for k in streaming_calls), (
            "Expected get_object_to_file to be called for clip in frames stage"
        )
        mp4_bytes = [k for k in buffering_calls if ".mp4" in k]
        assert mp4_bytes == [], (
            f"get_object_bytes called for MP4 in frames stage: {mp4_bytes}"
        )
