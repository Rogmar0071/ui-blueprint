"""
Tests for audio_path parameter in extract_transcript and extract_segment.
"""

from __future__ import annotations

from ui_blueprint.extractor import extract_segment, extract_transcript


class TestExtractTranscriptAudioPath:
    def test_returns_dict_with_transcript_key(self) -> None:
        result = extract_transcript("nonexistent.mp4", 0, 1000)
        assert isinstance(result, dict)
        assert "transcript" in result

    def test_audio_path_none_does_not_raise(self) -> None:
        result = extract_transcript("nonexistent.mp4", 0, 1000, audio_path=None)
        assert isinstance(result, dict)
        assert "transcript" in result

    def test_audio_path_string_does_not_raise(self) -> None:
        result = extract_transcript("nonexistent.mp4", 0, 1000, audio_path="/nonexistent/audio.m4a")
        assert isinstance(result, dict)
        assert "transcript" in result


class TestExtractSegmentAudioPath:
    def test_audio_path_none_does_not_raise(self) -> None:
        result = extract_segment("nonexistent.mp4", 0, 1000, audio_path=None)
        assert isinstance(result, dict)

    def test_audio_path_string_does_not_raise(self) -> None:
        result = extract_segment("nonexistent.mp4", 0, 1000, audio_path="/nonexistent/audio.m4a")
        assert isinstance(result, dict)
