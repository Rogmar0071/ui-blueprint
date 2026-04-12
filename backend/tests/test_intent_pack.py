"""Tests for ui_blueprint.intent_pack module."""

import json
from unittest.mock import MagicMock, patch

from ui_blueprint.intent_pack import (
    INTENT_PACK_SCHEMA_VERSION,
    _build_segment_summary,
    _empty_intent_pack,
    generate_intent_pack,
)

# ---------------------------------------------------------------------------
# _build_segment_summary
# ---------------------------------------------------------------------------

def test_build_segment_summary_empty():
    result = _build_segment_summary([])
    assert "No segment" in result


def test_build_segment_summary_basic():
    segments = [
        {
            "t0_ms": 0,
            "t1_ms": 3000,
            "analysis": {
                "elements_catalog": [{"type": "button"}, {"type": "toolbar"}],
                "events": [{"kind": "tap"}],
                "chunks": [{"key_scene": True}],
            },
        }
    ]
    result = _build_segment_summary(segments)
    assert "button" in result
    assert "toolbar" in result
    assert "tap" in result
    assert "key_scenes=1" in result


# ---------------------------------------------------------------------------
# _empty_intent_pack
# ---------------------------------------------------------------------------

def test_empty_intent_pack_structure():
    pack = _empty_intent_pack("test_reason")
    assert pack["intent_version"] == INTENT_PACK_SCHEMA_VERSION
    assert pack["app_domain"] == "unknown"
    assert pack["screens"] == []
    assert pack["flows"] == []
    assert pack["code_hints"] == []
    assert pack["_meta"]["reason"] == "test_reason"


# ---------------------------------------------------------------------------
# generate_intent_pack
# ---------------------------------------------------------------------------

def test_generate_intent_pack_no_segments():
    result = generate_intent_pack([], api_key="test")
    assert result["_meta"]["reason"] == "no_segments"


def _make_openai_response(content: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps(content)}}]
    }
    return mock_response


def test_generate_intent_pack_success():
    expected = {
        "intent_version": "1",
        "app_domain": "e-commerce",
        "screens": [
            {
                "screen_id": "s1",
                "label": "Cart",
                "elements": ["button"],
                "entry_events": [],
                "exit_events": [],
            }
        ],
        "flows": [],
        "code_hints": [
            {
                "type": "component",
                "name": "CartItem",
                "props": ["title"],
                "inferred_from": "s1",
            }
        ],
    }
    segments = [
        {
            "t0_ms": 0,
            "t1_ms": 2000,
            "analysis": {
                "elements_catalog": [{"type": "button"}],
                "events": [],
                "chunks": [],
            },
        }
    ]

    with patch("ui_blueprint.intent_pack.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = _make_openai_response(expected)

        result = generate_intent_pack(segments, api_key="sk-test")

    assert result["app_domain"] == "e-commerce"
    assert len(result["screens"]) == 1
    assert result["code_hints"][0]["name"] == "CartItem"


def test_generate_intent_pack_openai_error_returns_empty():
    segments = [
        {
            "t0_ms": 0,
            "t1_ms": 2000,
            "analysis": {"elements_catalog": [], "events": [], "chunks": []},
        }
    ]

    with patch("ui_blueprint.intent_pack.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = Exception("connection refused")

        result = generate_intent_pack(segments, api_key="sk-test")

    assert result["app_domain"] == "unknown"
    assert "connection refused" in result["_meta"]["reason"]


def test_generate_intent_pack_invalid_json_returns_empty():
    segments = [{"t0_ms": 0, "t1_ms": 2000, "analysis": {}}]
    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.raise_for_status = MagicMock()
    bad_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json {"}}]
    }

    with patch("ui_blueprint.intent_pack.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = bad_response

        result = generate_intent_pack(segments, api_key="sk-test")

    assert result["app_domain"] == "unknown"
