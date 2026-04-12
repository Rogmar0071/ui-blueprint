"""
ui_blueprint.intent_pack
========================
Generates an IntentPack from extractor segment data by calling gpt-4.1-mini.

An IntentPack is a structured, agent-consumable JSON artifact that describes:
- The app domain inferred from the recording
- Individual screens identified across segments
- User flows connecting those screens
- Code hints (component names, props, routes) an agent can use to generate code

Public API
----------
generate_intent_pack(segments: list[dict], api_key: str, model: str = "gpt-4.1-mini") -> dict
    Takes a list of segment analysis dicts (from extract_segment) and returns an IntentPack dict.
    Returns a minimal fallback dict on any error (never raises).

INTENT_PACK_SCHEMA_VERSION = "1"
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ui_blueprint.domain.openai_provider import _build_completions_url

logger = logging.getLogger(__name__)

INTENT_PACK_SCHEMA_VERSION = "1"

_DEFAULT_MODEL = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 45.0

_SYSTEM_PROMPT = """\
You are an expert mobile/web UI analyst. You receive structured extraction data from a
screen recording analysis pipeline (elements detected, UI events, screen chunks) and produce
an IntentPack.

An IntentPack identifies:
1. The app domain (e.g. "e-commerce checkout", "social feed", "settings")
2. Individual screens visible in the recording
3. User flows connecting screens
4. Code hints: inferred component names, props, and route names an agent can use to generate code

Respond ONLY with valid JSON matching this exact schema (no markdown fences, no prose):
{
  "intent_version": "1",
  "app_domain": "<short domain label>",
  "screens": [
    {
      "screen_id": "<slug>",
      "label": "<human label>",
      "elements": ["<element_type>", ...],
      "entry_events": ["<event_kind>:<element>", ...],
      "exit_events": ["<event_kind>:<element>", ...]
    }
  ],
  "flows": [
    {
      "flow_id": "<slug>",
      "steps": ["<screen_id>", ...],
      "trigger": "<event description>",
      "outcome": "<what happens at end>"
    }
  ],
  "code_hints": [
    {
      "type": "component|route|hook|service",
      "name": "<PascalCase or camelCase name>",
      "props": ["<prop_name>", ...],
      "inferred_from": "<screen_id or element reference>"
    }
  ]
}

Be concise. Limit to 5 screens, 3 flows, 10 code_hints maximum. If data is insufficient, return
best-effort with confidence reflected in fewer items.
"""

_USER_PROMPT_TEMPLATE = """\
Segment analysis data ({n_segments} segments):

{segment_summary}

Produce an IntentPack for this recording.
"""


def _build_segment_summary(segments: list[dict[str, Any]]) -> str:
    """
    Convert raw segment dicts into a compact text summary for the prompt.
    Keeps only the fields useful for intent inference to minimize tokens.
    """
    lines = []
    for i, seg in enumerate(segments):
        analysis = seg.get("analysis", seg)  # support both wrapped and unwrapped
        elements = analysis.get("elements_catalog", [])
        events = analysis.get("events", [])
        chunks = analysis.get("chunks", [])

        # Compact element type list (deduplicated)
        element_types = list(
            {e.get("type", "unknown") for e in elements if isinstance(e, dict)}
        )[:8]

        # Compact event list
        event_kinds = list({e.get("kind", "unknown") for e in events if isinstance(e, dict)})[:6]

        # Key scene flag from chunks
        key_scenes = sum(1 for c in chunks if isinstance(c, dict) and c.get("key_scene"))

        t0 = seg.get("t0_ms", analysis.get("t0_ms", "?"))
        t1 = seg.get("t1_ms", analysis.get("t1_ms", "?"))

        lines.append(
            f"Segment {i+1} [{t0}ms–{t1}ms]: "
            f"elements=[{', '.join(element_types) or 'none'}] "
            f"events=[{', '.join(event_kinds) or 'none'}] "
            f"key_scenes={key_scenes}"
        )
    return "\n".join(lines) if lines else "No segment data available."


def _empty_intent_pack(reason: str = "insufficient_data") -> dict[str, Any]:
    return {
        "intent_version": INTENT_PACK_SCHEMA_VERSION,
        "app_domain": "unknown",
        "screens": [],
        "flows": [],
        "code_hints": [],
        "_meta": {"status": "empty", "reason": reason},
    }


def generate_intent_pack(
    segments: list[dict[str, Any]],
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    Generate an IntentPack from segment analysis data.

    Parameters
    ----------
    segments:
        List of segment dicts. Each should have keys like t0_ms, t1_ms, and
        an 'analysis' sub-dict (or the analysis keys at the top level).
    api_key:
        OpenAI API key.
    model:
        Model name. Defaults to OPENAI_MODEL_CHAT env var or gpt-4.1-mini.
    base_url:
        OpenAI base URL. Defaults to OPENAI_BASE_URL env var.
    timeout:
        Request timeout. Defaults to OPENAI_TIMEOUT_SECONDS env var or 45s.

    Returns
    -------
    dict
        IntentPack dict. Never raises — returns _empty_intent_pack() on error.
    """
    if not segments:
        return _empty_intent_pack("no_segments")

    resolved_model = model or os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL)
    resolved_base_url = base_url or os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    resolved_timeout = timeout or float(
        os.environ.get("OPENAI_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT))
    )

    segment_summary = _build_segment_summary(segments)
    user_msg = _USER_PROMPT_TEMPLATE.format(
        n_segments=len(segments),
        segment_summary=segment_summary,
    )

    payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
    }

    url = _build_completions_url(resolved_base_url)

    try:
        with httpx.Client(timeout=resolved_timeout) as http:
            response = http.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(text)

        # Validate minimum structure
        if not isinstance(parsed, dict) or "intent_version" not in parsed:
            raise ValueError("Response missing intent_version")

        parsed["intent_version"] = INTENT_PACK_SCHEMA_VERSION
        parsed.setdefault("app_domain", "unknown")
        parsed.setdefault("screens", [])
        parsed.setdefault("flows", [])
        parsed.setdefault("code_hints", [])
        return parsed

    except Exception as exc:
        logger.warning("IntentPack generation failed: %s", exc)
        return _empty_intent_pack(str(exc)[:200])
