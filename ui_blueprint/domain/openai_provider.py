"""
ui_blueprint.domain.openai_provider
=====================================
OpenAI-backed domain-derivation provider.

Reads configuration from environment variables:
- OPENAI_API_KEY          (required to enable; if absent the module is imported
                           but the provider must not be instantiated)
- OPENAI_MODEL_DOMAIN     (optional; default: "gpt-4.1-mini")
- OPENAI_BASE_URL         (optional; default: "https://api.openai.com")
- OPENAI_TIMEOUT_SECONDS  (optional; default: 30)

The chat-completions URL is always built as::

    {OPENAI_BASE_URL}/v1/chat/completions

If OPENAI_BASE_URL already ends with ``/v1`` the suffix is not doubled.

Uses httpx (already a dependency) for HTTP calls — no additional packages
needed.

Security note
-------------
OPENAI_API_KEY is stored in a name-mangled private attribute; it is never
serialised, logged, or returned to clients.  Request/response bodies are
also never logged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ui_blueprint.domain.derivation import DomainDerivationProvider
from ui_blueprint.domain.ir import (
    CaptureStep,
    DerivedFrom,
    DomainProfile,
    ProfileExporter,
    ProfileValidator,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 30.0  # seconds

# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------


def _build_completions_url(base: str) -> str:
    """
    Build the chat-completions endpoint URL from *base*.

    Handles both bare origins and origins that already end with ``/v1``:

    >>> _build_completions_url("https://api.openai.com")
    'https://api.openai.com/v1/chat/completions'
    >>> _build_completions_url("https://api.openai.com/v1")
    'https://api.openai.com/v1/chat/completions'
    >>> _build_completions_url("https://proxy.example.com/openai/v1")
    'https://proxy.example.com/openai/v1/chat/completions'
    """
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a domain analyst for ui-blueprint. Given a media description, propose domain profile candidates.

Respond ONLY with valid JSON (no fences, no prose) with this structure:
{{"candidates": [{{"name": str, "capture_protocol": [{{"step_id": str, "title": str, "instructions": str, "required": bool}}], "validators": [{{"id": str, "type": str, "params": {{}}}}], "exporters": [{{"id": str, "type": str, "params": {{}}}}], "notes": str, "confidence": float}}]}}

Return 1–{max_candidates} candidates, highest confidence first. Each must have ≥2 capture steps, ≥1 validator, ≥1 exporter.
"""

_USER_PROMPT_TEMPLATE = """\
Media input:
- media_id: {media_id}
- media_type: {media_type}
- hint: {hint}

Propose up to {max_candidates} domain profile candidate(s).
"""

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

# Safe hint values surfaced to callers (never includes secrets or raw text).
_SAFE_HINTS = frozenset(
    {"timeout", "network_error", "http_error", "unauthorized", "rate_limited", "invalid_response"}
)


class OpenAIProviderError(Exception):
    """Raised when the OpenAI call fails; surfaced as a 502 in domain_routes."""

    def __init__(self, reason: str, hint: str = "invalid_response") -> None:
        super().__init__(reason)
        # Normalise to a known-safe hint so callers never leak secrets.
        self.hint: str = hint if hint in _SAFE_HINTS else "invalid_response"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIDomainDerivationProvider(DomainDerivationProvider):
    """
    AI domain-derivation provider backed by OpenAI Chat Completions.

    Raises :class:`OpenAIProviderError` on any failure so that
    ``domain_routes.py`` can return a structured 502 response — no fallback
    profile is silently substituted.

    Parameters
    ----------
    api_key:
        OpenAI API key.  Name-mangled; never logged or returned to callers.
    model:
        Chat model name.  Defaults to ``OPENAI_MODEL_DOMAIN`` env var or
        ``gpt-4.1-mini``.
    base_url:
        API base URL (origin, with or without trailing ``/v1``).  Defaults to
        ``OPENAI_BASE_URL`` env var or ``https://api.openai.com``.
    timeout:
        Request timeout in seconds.  Defaults to ``OPENAI_TIMEOUT_SECONDS``
        env var or 30.
    """

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.__api_key = api_key  # name-mangled — never returned or logged
        self._model = model or os.environ.get("OPENAI_MODEL_DOMAIN", _DEFAULT_MODEL)
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
        self._timeout = timeout or float(
            os.environ.get("OPENAI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT)
        )

    # ------------------------------------------------------------------
    # DomainDerivationProvider interface
    # ------------------------------------------------------------------

    def derive(
        self, media_input: dict[str, Any], max_candidates: int = 3
    ) -> list[DomainProfile]:
        """
        Call OpenAI to derive domain profile candidates.

        Raises
        ------
        OpenAIProviderError
            On network error, timeout, HTTP error, or unparseable response.
            The caller (``domain_routes.py``) converts this into a 502 response.
        """
        return self._call_openai(media_input, max_candidates)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_openai(
        self, media_input: dict[str, Any], max_candidates: int
    ) -> list[DomainProfile]:
        media_id: str = media_input.get("media_id", "unknown")
        media_type: str = media_input.get("media_type", "video")
        hint: str = media_input.get("hint", "")

        system_msg = _SYSTEM_PROMPT.format(max_candidates=max_candidates)
        user_msg = _USER_PROMPT_TEMPLATE.format(
            media_id=media_id,
            media_type=media_type,
            hint=hint or "(none provided)",
            max_candidates=max_candidates,
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.3,
            "max_tokens": 700,
        }

        url = _build_completions_url(self._base_url)

        try:
            with httpx.Client(timeout=self._timeout) as http:
                response = http.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.__api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise OpenAIProviderError("Request to OpenAI timed out", hint="timeout") from exc
        except httpx.RequestError as exc:
            raise OpenAIProviderError(
                "Network error contacting OpenAI", hint="network_error"
            ) from exc

        if response.status_code == 401:
            raise OpenAIProviderError(
                f"OpenAI returned HTTP {response.status_code}", hint="unauthorized"
            )
        if response.status_code == 429:
            raise OpenAIProviderError(
                f"OpenAI returned HTTP {response.status_code}", hint="rate_limited"
            )
        if response.status_code != 200:
            raise OpenAIProviderError(
                f"OpenAI returned HTTP {response.status_code}", hint="http_error"
            )

        try:
            content = response.json()
            text: str = content["choices"][0]["message"]["content"].strip()
            parsed: dict[str, Any] = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
            raise OpenAIProviderError(
                "Could not parse OpenAI response", hint="invalid_response"
            ) from exc

        candidates_raw: list[dict[str, Any]] = parsed.get("candidates", [])
        if not candidates_raw:
            raise OpenAIProviderError(
                "OpenAI returned no candidates", hint="invalid_response"
            )

        return [self._raw_to_profile(raw, media_input) for raw in candidates_raw[:max_candidates]]

    def _raw_to_profile(
        self, raw: dict[str, Any], media_input: dict[str, Any]
    ) -> DomainProfile:
        media_id: str = media_input.get("media_id", "unknown")
        confidence: float = float(raw.get("confidence", 0.75))

        capture_protocol = [
            CaptureStep(
                step_id=str(s.get("step_id", "")),
                title=str(s.get("title", "")),
                instructions=str(s.get("instructions", "")),
                required=bool(s.get("required", True)),
            )
            for s in raw.get("capture_protocol", [])
        ]
        validators = [
            ProfileValidator(
                id=str(v.get("id", "")),
                type=str(v.get("type", "generic")),
                params=dict(v.get("params", {})),
            )
            for v in raw.get("validators", [])
        ]
        exporters = [
            ProfileExporter(
                id=str(e.get("id", "")),
                type=str(e.get("type", "generic")),
                params=dict(e.get("params", {})),
            )
            for e in raw.get("exporters", [])
        ]
        notes = str(
            raw.get("notes", f"AI-derived (openai/{self._model}) — confidence {confidence:.2f}.")
        )

        return DomainProfile(
            name=str(raw.get("name", "AI-Derived Domain")),
            status="draft",
            derived_from=DerivedFrom(
                media_id=media_id,
                provider="openai",
                provider_version=self._model,
            ),
            capture_protocol=capture_protocol,
            validators=validators,
            exporters=exporters,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_provider_from_env() -> OpenAIDomainDerivationProvider | None:
    """
    Return an :class:`OpenAIDomainDerivationProvider` when ``OPENAI_API_KEY``
    is set, otherwise ``None``.

    This is the intended entry-point for ``domain_routes.py``.
    The API key is never returned to callers or logged.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    return OpenAIDomainDerivationProvider(api_key=api_key)

