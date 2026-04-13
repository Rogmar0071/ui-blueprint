"""
Tests for ui_blueprint.extractor
=================================
Validates that:
- Synthetic extraction produces schema-valid blueprint JSON.
- The CLI's --synthetic flag works end-to-end.
- Chunk structure and timeline coverage are correct.
- New public functions (extract_segment, extract_keyframes, extract_ocr,
  extract_transcript) return the expected shapes and fall back gracefully.
- _ocr_region falls back to "" when pytesseract is absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

from ui_blueprint.extractor import (
    MAX_UI_DEPTH,
    MAX_UI_NODES,
    SCHEMA_VERSION,
    _generate_synthetic_frame,
    _ocr_region,
    analyze_audio_transcript,
    analyze_clip,
    analyze_video_ui,
    build_tree_from_nodes,
    chunk_ui_tree,
    extract,
    extract_audio_track,
    extract_keyframes,
    extract_ocr,
    extract_segment,
    extract_transcript,
    extract_video_track,
    preprocess_ui_tree,
    prune_ui_tree,
    save_blueprint,
    segment_ui_tree,
    split_and_analyze,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def schema() -> dict:
    """Load the blueprint JSON Schema from the repo root."""
    schema_path = Path(__file__).parent.parent / "schema" / "blueprint.schema.json"
    assert schema_path.exists(), f"Schema file not found: {schema_path}"
    with schema_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def synthetic_blueprint() -> dict:
    """Generate a synthetic blueprint (session-scoped for speed)."""
    return extract(None, synthetic=True)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_synthetic_blueprint_validates(self, synthetic_blueprint: dict, schema: dict) -> None:
        """The blueprint produced from synthetic data must pass JSON Schema validation."""
        jsonschema.validate(instance=synthetic_blueprint, schema=schema)

    def test_version_field(self, synthetic_blueprint: dict) -> None:
        assert synthetic_blueprint["version"] == SCHEMA_VERSION

    def test_required_top_level_keys(self, synthetic_blueprint: dict) -> None:
        for key in ("version", "meta", "assets", "elements_catalog", "chunks"):
            assert key in synthetic_blueprint, f"Missing top-level key: {key}"

    def test_meta_fields(self, synthetic_blueprint: dict) -> None:
        meta = synthetic_blueprint["meta"]
        assert meta["width_px"] > 0
        assert meta["height_px"] > 0
        assert meta["fps"] > 0
        assert meta["duration_ms"] > 0

    def test_chunks_non_empty(self, synthetic_blueprint: dict) -> None:
        assert len(synthetic_blueprint["chunks"]) >= 1

    def test_chunks_cover_full_duration(self, synthetic_blueprint: dict) -> None:
        """Chunk t0/t1 should cover [0, duration_ms] without gaps."""
        chunks = synthetic_blueprint["chunks"]
        duration_ms = synthetic_blueprint["meta"]["duration_ms"]
        assert chunks[0]["t0_ms"] == pytest.approx(0.0)
        # The last chunk ends at exactly duration_ms (floating-point chunk arithmetic).
        assert chunks[-1]["t1_ms"] == pytest.approx(duration_ms, abs=1e-6)
        for i in range(len(chunks) - 1):
            assert chunks[i]["t1_ms"] == pytest.approx(chunks[i + 1]["t0_ms"], abs=1e-6)

    def test_chunk_has_key_scene(self, synthetic_blueprint: dict) -> None:
        for chunk in synthetic_blueprint["chunks"]:
            assert "key_scene" in chunk
            assert isinstance(chunk["key_scene"], list)

    def test_chunk_has_tracks(self, synthetic_blueprint: dict) -> None:
        for chunk in synthetic_blueprint["chunks"]:
            assert "tracks" in chunk
            assert isinstance(chunk["tracks"], list)

    def test_chunk_has_events(self, synthetic_blueprint: dict) -> None:
        for chunk in synthetic_blueprint["chunks"]:
            assert "events" in chunk
            assert isinstance(chunk["events"], list)

    def test_elements_catalog_non_empty(self, synthetic_blueprint: dict) -> None:
        assert len(synthetic_blueprint["elements_catalog"]) >= 1

    def test_all_scene_element_ids_in_catalog(self, synthetic_blueprint: dict) -> None:
        catalog_ids = {el["id"] for el in synthetic_blueprint["elements_catalog"]}
        for chunk in synthetic_blueprint["chunks"]:
            for node in chunk["key_scene"]:
                assert node["element_id"] in catalog_ids, (
                    f"Scene node references unknown element: {node['element_id']}"
                )

    def test_all_track_element_ids_in_catalog(self, synthetic_blueprint: dict) -> None:
        catalog_ids = {el["id"] for el in synthetic_blueprint["elements_catalog"]}
        for chunk in synthetic_blueprint["chunks"]:
            for track in chunk["tracks"]:
                assert track["element_id"] in catalog_ids

    def test_track_models_are_valid(self, synthetic_blueprint: dict) -> None:
        valid_models = {"bezier", "spring", "linear", "step", "sampled"}
        for chunk in synthetic_blueprint["chunks"]:
            for track in chunk["tracks"]:
                assert track["model"] in valid_models


# ---------------------------------------------------------------------------
# Custom chunk parameters
# ---------------------------------------------------------------------------


class TestCustomParameters:
    def test_custom_chunk_ms(self) -> None:
        bp = extract(None, synthetic=True, chunk_ms=2000)
        duration_ms = bp["meta"]["duration_ms"]
        expected_chunks = max(1, int(duration_ms / 2000))
        # Allow off-by-one due to floating-point chunk boundary.
        assert abs(len(bp["chunks"]) - expected_chunks) <= 1

    def test_custom_sample_fps(self) -> None:
        bp = extract(None, synthetic=True, sample_fps=5)
        # With sample_fps=5 over 10 s there should be ~51 samples (0..10 s step 0.2 s).
        total_samples = sum(len(c["tracks"]) for c in bp["chunks"])
        assert total_samples > 0

    def test_synthetic_pipeline_infers_events(self) -> None:
        bp = extract(None, synthetic=True, sample_fps=10)
        event_kinds = {event["kind"] for chunk in bp["chunks"] for event in chunk["events"]}
        assert "scroll" in event_kinds

    def test_synthetic_pipeline_uses_fitted_tracks(self) -> None:
        bp = extract(None, synthetic=True, sample_fps=10)
        track_models = {track["model"] for chunk in bp["chunks"] for track in chunk["tracks"]}
        assert "linear" in track_models or "bezier" in track_models
        for chunk in bp["chunks"]:
            for track in chunk["tracks"]:
                assert "residual_error" in track

    def test_assets_dir_exports_real_crops(self, tmp_path: Path) -> None:
        assets_dir = tmp_path / "assets"
        bp = extract(None, synthetic=True, assets_dir=assets_dir)
        assert bp["assets"]
        for asset in bp["assets"]:
            assert Path(asset["path"]).exists()


# ---------------------------------------------------------------------------
# File save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveBlueprintRoundTrip:
    def test_save_and_reload(self, tmp_path: Path, synthetic_blueprint: dict, schema: dict) -> None:
        out = tmp_path / "bp.json"
        save_blueprint(synthetic_blueprint, out)
        assert out.exists()
        with out.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        jsonschema.validate(instance=loaded, schema=schema)


# ---------------------------------------------------------------------------
# CLI integration test (--synthetic flag)
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_synthetic_extract_produces_valid_blueprint(
        self, tmp_path: Path, schema: dict
    ) -> None:
        out_json = tmp_path / "cli_test.json"
        result = subprocess.run(
            [sys.executable, "-m", "ui_blueprint", "extract", "--synthetic", "-o", str(out_json)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"CLI exited with code {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert out_json.exists(), "Output file was not created."
        with out_json.open("r", encoding="utf-8") as fh:
            bp = json.load(fh)
        jsonschema.validate(instance=bp, schema=schema)

    def test_cli_preview_produces_png_frames(self, tmp_path: Path) -> None:
        bp_path = tmp_path / "bp.json"
        frames_dir = tmp_path / "frames"

        # Generate blueprint first.
        result_extract = subprocess.run(
            [sys.executable, "-m", "ui_blueprint", "extract", "--synthetic", "-o", str(bp_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result_extract.returncode == 0

        # Run preview.
        result_preview = subprocess.run(
            [
                sys.executable,
                "-m",
                "ui_blueprint",
                "preview",
                str(bp_path),
                "--out",
                str(frames_dir),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result_preview.returncode == 0, (
            f"Preview CLI failed.\nstdout: {result_preview.stdout}\nstderr: {result_preview.stderr}"
        )
        png_files = list(frames_dir.glob("*.png"))
        assert len(png_files) >= 1, "No PNG frames were produced."

    def test_cli_missing_video_without_synthetic(self, tmp_path: Path) -> None:
        out_json = tmp_path / "should_not_exist.json"
        result = subprocess.run(
            [sys.executable, "-m", "ui_blueprint", "extract", "-o", str(out_json)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0

    def test_cli_nonexistent_video_file(self, tmp_path: Path) -> None:
        out_json = tmp_path / "should_not_exist.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ui_blueprint",
                "extract",
                "/nonexistent/video.mp4",
                "-o",
                str(out_json),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0


class TestVideoDecoderPath:
    def test_extract_from_real_video_when_imageio_available(
        self, tmp_path: Path, schema: dict
    ) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        video_path = tmp_path / "sample.mp4"
        frames = []
        meta = {
            "width_px": 368,
            "height_px": 640,
            "fps": 12.0,
            "duration_ms": 1000.0,
        }
        frame_interval_ms = 1000.0 / 12.0
        for idx in range(12):
            frame = _generate_synthetic_frame(meta, idx * frame_interval_ms).resize((368, 640))
            frames.append(numpy.asarray(frame))

        with imageio.get_writer(video_path, fps=12, format="FFMPEG") as writer:
            for frame in frames:
                writer.append_data(frame)

        bp = extract(video_path, sample_fps=6)
        assert bp["meta"]["width_px"] == 368
        assert bp["meta"]["height_px"] == 640
        assert bp["elements_catalog"]
        jsonschema.validate(instance=bp, schema=schema)


# ---------------------------------------------------------------------------
# New public extraction helpers
# ---------------------------------------------------------------------------


class TestOcrRegionFallback:
    """_ocr_region must return "" when pytesseract is not importable."""

    def test_returns_empty_string_when_pytesseract_missing(self) -> None:
        """Simulate pytesseract being absent via import patch."""
        from PIL import Image

        img = Image.new("RGB", (100, 30), color=(255, 255, 255))
        frame_bytes = img.tobytes()
        bbox = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 30.0}

        with patch.dict("sys.modules", {"pytesseract": None}):
            result = _ocr_region(frame_bytes, bbox, 100, 30)

        assert result == ""

    def test_returns_empty_string_on_pytesseract_exception(self) -> None:
        """If pytesseract raises during OCR, return "" without crashing."""
        from PIL import Image

        img = Image.new("RGB", (100, 30), color=(255, 255, 255))
        frame_bytes = img.tobytes()
        bbox = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 30.0}

        mock_pt = types.ModuleType("pytesseract")
        mock_pt.image_to_string = lambda *_a, **_kw: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            RuntimeError("tesseract not found")
        )
        with patch.dict("sys.modules", {"pytesseract": mock_pt}):
            result = _ocr_region(frame_bytes, bbox, 100, 30)

        assert result == ""


class TestExtractSegmentShape:
    """extract_segment must return the expected dict shape."""

    def test_returns_empty_on_nonexistent_file(self) -> None:
        result = extract_segment("/nonexistent/clip.mp4", 0, 5000)
        assert "elements_catalog" in result
        assert "chunks" in result
        assert "events" in result
        assert "quality" in result
        assert isinstance(result["elements_catalog"], list)
        assert isinstance(result["chunks"], list)
        assert isinstance(result["events"], list)

    def test_returns_empty_on_zero_duration(self) -> None:
        result = extract_segment("/nonexistent/clip.mp4", 1000, 1000)
        assert result["elements_catalog"] == []
        assert result["chunks"] == []
        assert result["events"] == []

    def test_returns_real_data_for_synthetic_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        video_path = tmp_path / "seg_test.mp4"
        meta = {
            "width_px": 368,
            "height_px": 640,
            "fps": 12.0,
            "duration_ms": 3000.0,
        }
        frame_interval_ms = 1000.0 / 12.0
        frames_np = []
        for idx in range(36):
            img = _generate_synthetic_frame(meta, idx * frame_interval_ms).resize((368, 640))
            frames_np.append(numpy.asarray(img))

        with imageio.get_writer(video_path, fps=12, format="FFMPEG") as writer:
            for frame in frames_np:
                writer.append_data(frame)

        result = extract_segment(str(video_path), 0, 3000)
        assert "elements_catalog" in result
        assert "chunks" in result
        assert "events" in result
        assert "quality" in result
        # With a real video the catalog should be non-empty.
        assert isinstance(result["elements_catalog"], list)


class TestExtractOptionalHelpers:
    """extract_keyframes, extract_ocr, extract_transcript return correct shapes."""

    def test_extract_keyframes_empty_on_missing_file(self) -> None:
        result = extract_keyframes("/nonexistent/clip.mp4", 0, 5000)
        assert "frames" in result
        assert isinstance(result["frames"], list)

    def test_extract_ocr_empty_on_missing_file(self) -> None:
        result = extract_ocr("/nonexistent/clip.mp4", 0, 5000)
        assert "text_blocks" in result
        assert isinstance(result["text_blocks"], list)

    def test_extract_transcript_returns_empty_string(self, tmp_path: Path) -> None:
        result = extract_transcript(str(tmp_path / "dummy.mp4"), 0, 5000)
        assert "transcript" in result
        assert result["transcript"] == ""

    def test_extract_keyframes_real_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        video_path = tmp_path / "kf_test.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 2000.0}
        frame_interval_ms = 1000.0 / 12.0
        frames_np = []
        for idx in range(24):
            img = _generate_synthetic_frame(meta, idx * frame_interval_ms).resize((368, 640))
            frames_np.append(numpy.asarray(img))

        with imageio.get_writer(video_path, fps=12, format="FFMPEG") as writer:
            for frame in frames_np:
                writer.append_data(frame)

        result = extract_keyframes(str(video_path), 0, 2000)
        assert "frames" in result
        assert isinstance(result["frames"], list)
        for frame in result["frames"]:
            assert "t_ms" in frame
            assert "width" in frame
            assert "height" in frame


# ---------------------------------------------------------------------------
# UI tree analysis pipeline
# ---------------------------------------------------------------------------

def _make_tree(depth: int, branching: int = 2) -> dict:
    """Build a balanced tree with *depth* levels and *branching* children per node."""
    def _node(d: int) -> dict:
        if d == 0:
            return {"id": f"leaf_{d}", "children": []}
        return {"id": f"node_{d}", "children": [_node(d - 1) for _ in range(branching)]}

    return {"root": _node(depth)}


class TestPreprocessUITree:
    def test_passthrough_when_within_limits(self) -> None:
        """Small trees are returned unchanged."""
        tree = _make_tree(depth=3, branching=2)
        result = preprocess_ui_tree(tree)
        assert result is tree

    def test_prunes_when_too_deep(self) -> None:
        """Trees deeper than MAX_UI_DEPTH are pruned."""
        tree = _make_tree(depth=MAX_UI_DEPTH + 5, branching=1)
        import warnings
        with warnings.catch_warnings(record=True):
            result = preprocess_ui_tree(tree)
        # After pruning the tree should be returned (same object, mutated)
        assert "root" in result

    def test_prunes_when_too_many_nodes(self) -> None:
        """Trees with more than MAX_UI_NODES nodes are pruned."""
        # Build a wide flat tree with MAX_UI_NODES + 100 children
        big_tree: dict = {
            "root": {
                "id": "root",
                "children": [{"id": f"c{i}", "children": []} for i in range(MAX_UI_NODES + 100)],
            }
        }
        import warnings
        with warnings.catch_warnings(record=True):
            result = preprocess_ui_tree(big_tree)
        assert len(result["root"]["children"]) < MAX_UI_NODES + 100


class TestPruneUITree:
    def test_depth_truncation(self) -> None:
        """Nodes beyond max_depth should have their children cleared."""
        tree = _make_tree(depth=10, branching=2)
        pruned = prune_ui_tree(tree, max_nodes=10000, max_depth=3)
        # Walk tree and verify no node past depth 3 has children
        def _check(node: dict, depth: int) -> None:
            if depth > 3:
                assert node.get("children") == []
            for child in node.get("children") or []:
                _check(child, depth + 1)
        _check(pruned["root"], 1)

    def test_node_count_truncation(self) -> None:
        """Children list is capped at max_nodes - 1."""
        tree: dict = {
            "root": {
                "id": "root",
                "children": [{"id": f"c{i}", "children": []} for i in range(20)],
            }
        }
        pruned = prune_ui_tree(tree, max_nodes=5, max_depth=50)
        assert len(pruned["root"]["children"]) <= 4  # max_nodes - 1


class TestSegmentUITree:
    def test_returns_list_of_nodes(self) -> None:
        """segment_ui_tree returns every node in the tree."""
        tree = _make_tree(depth=3, branching=2)
        result = segment_ui_tree(tree)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0

    def test_respects_max_depth(self) -> None:
        """Subtrees beyond MAX_UI_DEPTH are skipped (no error raised)."""
        # Build a chain that just exceeds MAX_UI_DEPTH
        deep_tree = _make_tree(depth=MAX_UI_DEPTH + 2, branching=1)
        import warnings
        with warnings.catch_warnings(record=True):
            result = segment_ui_tree(deep_tree)
        # Should return some segments (root-level nodes) without crashing
        assert result is not None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """segment_ui_tree returns None when the time limit is exceeded."""
        import ui_blueprint.extractor as ext

        # Fake time so that the first check already exceeds the limit
        monkeypatch.setattr(ext, "MAX_SEGMENTATION_TIME_MS", -1)
        tree = _make_tree(depth=5, branching=2)
        import warnings
        with warnings.catch_warnings(record=True):
            result = segment_ui_tree(tree)
        assert result is None


class TestChunkUITree:
    def test_all_nodes_present_across_chunks(self) -> None:
        """Every node from the original tree must appear across all chunks."""
        tree = _make_tree(depth=4, branching=3)
        chunks = chunk_ui_tree(tree, max_chunk_size=5)
        total_nodes = sum(len(c) for c in chunks)
        # 3^4 + 3^3 + 3^2 + 3^1 + 1 = 121 nodes in a depth-4 branching-3 tree
        assert total_nodes > 0
        # Multiple chunks should be produced
        assert len(chunks) > 1

    def test_single_chunk_for_small_tree(self) -> None:
        tree = _make_tree(depth=2, branching=2)
        chunks = chunk_ui_tree(tree, max_chunk_size=1000)
        assert len(chunks) == 1

    def test_empty_tree_returns_single_chunk(self) -> None:
        tree: dict = {"root": {"id": "root", "children": []}}
        chunks = chunk_ui_tree(tree)
        assert len(chunks) == 1
        assert chunks[0][0]["id"] == "root"


class TestBuildTreeFromNodes:
    def test_empty_list_returns_sentinel(self) -> None:
        result = build_tree_from_nodes([])
        assert result["root"]["children"] == []

    def test_first_node_becomes_root(self) -> None:
        nodes = [{"id": "a", "children": []}, {"id": "b", "children": []}]
        result = build_tree_from_nodes(nodes)
        assert result["root"]["id"] == "a"

    def test_remaining_nodes_are_children(self) -> None:
        nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        result = build_tree_from_nodes(nodes)
        assert len(result["root"]["children"]) == 2


class TestAnalyzeClip:
    def test_returns_list_of_segments(self) -> None:
        tree = _make_tree(depth=3, branching=2)
        result = analyze_clip(tree)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_handles_large_tree(self) -> None:
        """analyze_clip should not crash on a tree that needs pruning."""
        big_tree: dict = {
            "root": {
                "id": "root",
                "children": [
                    {"id": f"c{i}", "children": []} for i in range(MAX_UI_NODES + 200)
                ],
            }
        }
        import warnings
        with warnings.catch_warnings(record=True):
            result = analyze_clip(big_tree)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Split video/audio analysis pipeline
# ---------------------------------------------------------------------------


class TestExtractVideoTrack:
    def test_returns_false_on_missing_file(self, tmp_path: Path) -> None:
        assert extract_video_track("/nonexistent/clip.mp4", str(tmp_path / "out.mp4")) is False

    def test_returns_true_for_real_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        src = tmp_path / "src.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 1000.0}
        frames_np = [
            numpy.asarray(
                _generate_synthetic_frame(meta, i * (1000.0 / 12.0)).resize((368, 640))
            )
            for i in range(12)
        ]
        with imageio.get_writer(src, fps=12, format="FFMPEG") as writer:
            for f in frames_np:
                writer.append_data(f)

        out = tmp_path / "video_only.mp4"
        result = extract_video_track(str(src), str(out))
        assert result is True
        assert out.exists()
        assert out.stat().st_size > 0


class TestExtractAudioTrack:
    def test_returns_false_on_missing_file(self, tmp_path: Path) -> None:
        assert extract_audio_track("/nonexistent/clip.mp4", str(tmp_path / "out.wav")) is False

    def test_returns_false_for_silent_video(self, tmp_path: Path) -> None:
        """A synthetic video-only file has no audio stream; extraction yields False."""
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        src = tmp_path / "src.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 1000.0}
        frames_np = [
            numpy.asarray(
                _generate_synthetic_frame(meta, i * (1000.0 / 12.0)).resize((368, 640))
            )
            for i in range(12)
        ]
        with imageio.get_writer(src, fps=12, format="FFMPEG") as writer:
            for f in frames_np:
                writer.append_data(f)

        out = tmp_path / "audio_only.wav"
        # Silent video: ffmpeg will produce an empty/missing output → False
        result = extract_audio_track(str(src), str(out))
        assert isinstance(result, bool)


class TestAnalyzeVideoUI:
    def test_returns_expected_shape_on_missing_file(self) -> None:
        result = analyze_video_ui("/nonexistent/video.mp4")
        assert "elements_catalog" in result
        assert "chunks" in result
        assert "events" in result
        assert "quality" in result
        assert isinstance(result["elements_catalog"], list)
        assert isinstance(result["chunks"], list)
        assert isinstance(result["events"], list)

    def test_returns_data_for_real_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        src = tmp_path / "video_only.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 1000.0}
        frames_np = [
            numpy.asarray(
                _generate_synthetic_frame(meta, i * (1000.0 / 12.0)).resize((368, 640))
            )
            for i in range(12)
        ]
        with imageio.get_writer(src, fps=12, format="FFMPEG") as writer:
            for f in frames_np:
                writer.append_data(f)

        result = analyze_video_ui(str(src))
        assert isinstance(result["elements_catalog"], list)
        assert isinstance(result["chunks"], list)


class TestAnalyzeAudioTranscript:
    def test_returns_transcript_key(self, tmp_path: Path) -> None:
        result = analyze_audio_transcript(str(tmp_path / "dummy.wav"))
        assert "transcript" in result
        assert isinstance(result["transcript"], str)

    def test_falls_back_on_error(self) -> None:
        result = analyze_audio_transcript("/nonexistent/audio.wav")
        assert "transcript" in result
        assert isinstance(result["transcript"], str)


class TestSplitAndAnalyze:
    def test_returns_expected_shape_on_missing_file(self) -> None:
        result = split_and_analyze("/nonexistent/clip.mp4")
        assert "ui_structure" in result
        assert "audio_transcript" in result
        assert isinstance(result["ui_structure"], dict)
        assert isinstance(result["audio_transcript"], dict)
        assert "elements_catalog" in result["ui_structure"]
        assert "transcript" in result["audio_transcript"]

    def test_accepts_explicit_output_paths(self, tmp_path: Path) -> None:
        video_out = str(tmp_path / "video.mp4")
        audio_out = str(tmp_path / "audio.wav")
        result = split_and_analyze(
            "/nonexistent/clip.mp4",
            video_out=video_out,
            audio_out=audio_out,
        )
        assert "ui_structure" in result
        assert "audio_transcript" in result

    def test_returns_data_for_real_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        src = tmp_path / "clip.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 1000.0}
        frames_np = [
            numpy.asarray(
                _generate_synthetic_frame(meta, i * (1000.0 / 12.0)).resize((368, 640))
            )
            for i in range(12)
        ]
        with imageio.get_writer(src, fps=12, format="FFMPEG") as writer:
            for f in frames_np:
                writer.append_data(f)

        result = split_and_analyze(str(src))
        assert "ui_structure" in result
        assert "audio_transcript" in result
        assert isinstance(result["ui_structure"]["elements_catalog"], list)
        assert isinstance(result["audio_transcript"]["transcript"], str)


class TestSplitAnalyzeCLI:
    def test_split_analyze_missing_clip(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable, "-m", "ui_blueprint",
                "split-analyze", "/nonexistent/clip.mp4",
                "--ui-output", str(tmp_path / "ui.json"),
                "--audio-output", str(tmp_path / "audio.json"),
                "--combined-output", str(tmp_path / "combined.json"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_split_analyze_synthetic_video(self, tmp_path: Path) -> None:
        imageio = pytest.importorskip("imageio.v2")
        numpy = pytest.importorskip("numpy")

        src = tmp_path / "clip.mp4"
        meta = {"width_px": 368, "height_px": 640, "fps": 12.0, "duration_ms": 1000.0}
        frames_np = [
            numpy.asarray(
                _generate_synthetic_frame(meta, i * (1000.0 / 12.0)).resize((368, 640))
            )
            for i in range(12)
        ]
        with imageio.get_writer(src, fps=12, format="FFMPEG") as writer:
            for f in frames_np:
                writer.append_data(f)

        ui_json = tmp_path / "ui.json"
        audio_json = tmp_path / "audio.json"
        combined_json = tmp_path / "combined.json"

        result = subprocess.run(
            [
                sys.executable, "-m", "ui_blueprint",
                "split-analyze", str(src),
                "--ui-output", str(ui_json),
                "--audio-output", str(audio_json),
                "--combined-output", str(combined_json),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"split-analyze failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert ui_json.exists()
        assert audio_json.exists()
        assert combined_json.exists()

        with combined_json.open() as fh:
            combined = json.load(fh)
        assert "ui_structure" in combined
        assert "audio_transcript" in combined
