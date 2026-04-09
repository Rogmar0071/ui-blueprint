"""
backend.app.domain_routes
=========================
FastAPI router implementing the AI-derived Domain Profile and Blueprint
Compiler API for steering contract v1.1.1, while successful payloads keep
schema_version v1.1.0 per the compatibility invariant.

Endpoints
---------
POST   /api/domains/derive                       derive draft domain profile candidates  [auth]
GET    /api/domains/{domain_profile_id}          fetch a domain profile                  [public]
PATCH  /api/domains/{domain_profile_id}          edit a draft profile (409 if not draft) [auth]
POST   /api/domains/{domain_profile_id}/confirm  confirm a draft profile (409 if not draft) [auth]
POST   /api/blueprints/compile                   compile blueprint (confirmed domain req.)    [auth]

All mutating endpoints require ``Authorization: Bearer <API_KEY>``.
GET is intentionally public so clients can inspect profiles without a key.

Error shape (all error responses)::

    {"error": {"code": "string", "message": "string", "details": {}}}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.app.auth import require_auth
from ui_blueprint.domain.compiler import BlueprintCompileError, compileBlueprintFromMedia
from ui_blueprint.domain.derivation import DomainDerivationProvider, StubDomainDerivationProvider
from ui_blueprint.domain.ir import (
    DOMAIN_STATUS_CONFIRMED,
    DOMAIN_STATUS_DRAFT,
    SCHEMA_VERSION,
    CaptureStep,
    DomainProfile,
    ProfileExporter,
    ProfileValidator,
)
from ui_blueprint.domain.openai_provider import OpenAIProviderError, build_provider_from_env
from ui_blueprint.domain.store import DomainProfileStore, InMemoryDomainProfileStore

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Module-level store (replaced per-app in tests via set_store)
# ---------------------------------------------------------------------------

_store: DomainProfileStore = InMemoryDomainProfileStore()


def get_store() -> DomainProfileStore:
    """Return the active DomainProfileStore (overridable in tests)."""
    return _store


def set_store(store: DomainProfileStore) -> None:
    """Replace the active store (for testing or multi-tenant setups)."""
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

# Lazily initialised on first call; tests can override via set_provider().
_provider: DomainDerivationProvider | None = None


def get_provider() -> DomainDerivationProvider:
    """
    Return the active derivation provider.

    Uses OpenAIDomainDerivationProvider when OPENAI_API_KEY is set;
    falls back to StubDomainDerivationProvider otherwise.  Cached after
    the first call; tests can call set_provider() to override.
    """
    global _provider
    if _provider is None:
        openai_p = build_provider_from_env()
        _provider = openai_p if openai_p is not None else StubDomainDerivationProvider()
    return _provider


def set_provider(provider: DomainDerivationProvider | None) -> None:
    """Replace the active provider (for testing). Pass None to force lazy re-init from env."""
    global _provider
    _provider = provider


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """Return a successful JSON response with top-level schema_version."""
    return JSONResponse(
        status_code=status_code,
        content={"schema_version": SCHEMA_VERSION, **content},
    )


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Return a structured error JSON response."""
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def _get_profile_or_error(
    domain_profile_id: str,
) -> tuple[DomainProfile | None, JSONResponse | None]:
    """Return (profile, None) on success or (None, error_response) on miss."""
    profile = get_store().get(domain_profile_id)
    if profile is None:
        return None, _error(
            404,
            "not_found",
            f"Domain profile {domain_profile_id!r} not found.",
        )
    return profile, None


# ---------------------------------------------------------------------------
# POST /api/domains/derive
# ---------------------------------------------------------------------------


@router.post("/domains/derive", status_code=200, dependencies=[Depends(require_auth)])
async def derive_domain_profiles(body: dict[str, Any]) -> JSONResponse:
    """
    Derive one or more draft DomainProfile candidates from media input.

    Request body::

        {
          "schema_version": "v1.1.0",   // optional
          "media": {
            "media_id": "vid_001",       // required
            "media_type": "video",       // required
            "uri": "...",                // optional
            "metadata": {}              // optional
          },
          "options": {
            "max_candidates": 3,         // optional, default 3
            "hint": "cabinet assembly"  // optional free-text hint
          }
        }

    Creates and persists draft profiles; no profile is auto-confirmed.
    """
    media: dict[str, Any] = body.get("media", {})
    options: dict[str, Any] = body.get("options", {})

    # Validate required media fields.
    missing = [f for f in ("media_id", "media_type") if not media.get(f)]
    if missing:
        return _error(
            400,
            "invalid_request",
            f"Required media fields missing: {', '.join(f'media.{field}' for field in missing)}.",
            {"missing_fields": missing},
        )

    media_id: str = media["media_id"]
    hint: str = options.get("hint", "")
    max_candidates: int = int(options.get("max_candidates", 3))

    media_input: dict[str, Any] = {
        "media_id": media_id,
        "media_type": media["media_type"],
        "hint": hint,
        "metadata": media.get("metadata", {}),
    }

    try:
        candidates = get_provider().derive(media_input, max_candidates=max_candidates)
    except OpenAIProviderError as exc:
        return _error(
            502,
            "ai_provider_error",
            "Domain derivation failed.",
            {"provider": "openai", "hint": exc.hint},
        )

    store = get_store()
    for profile in candidates:
        store.save(profile)

    warnings: list[str] = []
    if not hint:
        warnings.append(
            "No hint provided; results may be less accurate. "
            "Pass options.hint with a brief description of the media content."
        )

    return _ok(
        {
            "candidates": [
                {
                    "domain_profile_id": p.id,
                    "status": p.status,
                    "name": p.name,
                    "schema_version": p.schema_version,
                    "summary": p.notes,
                }
                for p in candidates
            ],
            "warnings": warnings,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/domains/{domain_profile_id}
# ---------------------------------------------------------------------------


@router.get("/domains/{domain_profile_id}", status_code=200)
def get_domain_profile(domain_profile_id: str) -> JSONResponse:
    """Return the full DomainProfile for the given id."""
    profile, err = _get_profile_or_error(domain_profile_id)
    if err:
        return err
    return _ok({"domain_profile": profile.to_dict()})  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# PATCH /api/domains/{domain_profile_id}
# ---------------------------------------------------------------------------


@router.patch("/domains/{domain_profile_id}", status_code=200, dependencies=[Depends(require_auth)])
async def patch_domain_profile(
    domain_profile_id: str, body: dict[str, Any]
) -> JSONResponse:
    """
    Edit a draft DomainProfile.  Rejected with 409 if not in draft status.

    Request body::

        {
          "schema_version": "v1.1.0",   // optional
          "patch": {
            "name": "...",              // optional
            "capture_protocol": [...],  // optional
            "validators": [...],        // optional
            "exporters": [...],         // optional
            "notes": "..."              // optional
          }
        }
    """
    profile, err = _get_profile_or_error(domain_profile_id)
    if err:
        return err

    if profile.status != DOMAIN_STATUS_DRAFT:  # type: ignore[union-attr]
        return _error(
            409,
            "state_conflict",
            (
                f"Domain profile {domain_profile_id!r} has status"
                f" {profile.status!r} and cannot be edited."  # type: ignore[union-attr]
                " Create a new draft to make changes."
            ),
        )

    patch: dict[str, Any] = body.get("patch", {})

    if "name" in patch:
        profile.name = str(patch["name"])  # type: ignore[union-attr]
    if "notes" in patch:
        profile.notes = str(patch["notes"])  # type: ignore[union-attr]
    if "capture_protocol" in patch:
        profile.capture_protocol = [  # type: ignore[union-attr]
            CaptureStep.from_dict(s) for s in patch["capture_protocol"]
        ]
    if "validators" in patch:
        profile.validators = [  # type: ignore[union-attr]
            ProfileValidator.from_dict(v) for v in patch["validators"]
        ]
    if "exporters" in patch:
        profile.exporters = [  # type: ignore[union-attr]
            ProfileExporter.from_dict(e) for e in patch["exporters"]
        ]

    profile.updated_at = _now_rfc3339()  # type: ignore[union-attr]
    get_store().save(profile)  # type: ignore[arg-type]

    return _ok({"domain_profile": profile.to_dict()})  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# POST /api/domains/{domain_profile_id}/confirm
# ---------------------------------------------------------------------------


@router.post(
    "/domains/{domain_profile_id}/confirm",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
async def confirm_domain_profile(
    domain_profile_id: str, body: dict[str, Any]
) -> JSONResponse:
    """
    Confirm a draft DomainProfile, making it immutable and ready for compile.

    Returns 409 if the profile is not in draft status (non-idempotent by
    contract — callers must derive a new draft to re-confirm).

    Request body::

        {
          "schema_version": "v1.1.0",   // optional
          "confirmed_by": "alice",       // optional
          "note": "LGTM"                // optional
        }
    """
    profile, err = _get_profile_or_error(domain_profile_id)
    if err:
        return err

    if profile.status != DOMAIN_STATUS_DRAFT:  # type: ignore[union-attr]
        return _error(
            409,
            "state_conflict",
            (
                f"Domain profile {domain_profile_id!r} has status"
                f" {profile.status!r} and cannot be confirmed."  # type: ignore[union-attr]
                " Only draft profiles may be confirmed."
            ),
        )

    confirmed_by: str = body.get("confirmed_by", "")
    note: str = body.get("note", "")

    profile.status = DOMAIN_STATUS_CONFIRMED  # type: ignore[union-attr]
    profile.updated_at = _now_rfc3339()  # type: ignore[union-attr]
    if confirmed_by or note:
        existing = profile.notes or ""  # type: ignore[union-attr]
        addendum = f"Confirmed by: {confirmed_by}. {note}".strip()
        profile.notes = (  # type: ignore[union-attr]
            f"{existing}\n{addendum}".strip() if existing else addendum
        )

    get_store().save(profile)  # type: ignore[arg-type]

    return _ok(
        {
            "domain_profile": {
                "id": profile.id,  # type: ignore[union-attr]
                "status": profile.status,  # type: ignore[union-attr]
                "schema_version": profile.schema_version,  # type: ignore[union-attr]
                "updated_at": profile.updated_at,  # type: ignore[union-attr]
            }
        }
    )


# ---------------------------------------------------------------------------
# POST /api/blueprints/compile
# ---------------------------------------------------------------------------


@router.post("/blueprints/compile", status_code=200, dependencies=[Depends(require_auth)])
async def compile_blueprint(body: dict[str, Any]) -> JSONResponse:
    """
    Compile a BlueprintIR from media + a confirmed DomainProfile.

    Request body::

        {
          "schema_version": "v1.1.0",   // optional
          "media": {
            "media_id": "vid_001",
            "media_type": "video",
            "uri": "...",               // optional
            "metadata": {}              // optional
          },
          "domain_profile_id": "<uuid>"  // required
        }

    Returns 400 if domain_profile_id is absent or profile is not confirmed.
    Returns 404 if the profile is not found.
    """
    domain_profile_id: str | None = body.get("domain_profile_id")
    if not domain_profile_id:
        return _error(
            400,
            "invalid_request",
            "domain_profile_id is required."
            " Derive a domain profile, confirm it, then compile.",
        )

    profile, err = _get_profile_or_error(domain_profile_id)
    if err:
        return err

    media: dict[str, Any] = body.get("media", {})

    try:
        blueprint = compileBlueprintFromMedia(
            media=media, confirmed_domain_profile=profile
        )
    except BlueprintCompileError:
        return _error(
            400,
            "domain_not_confirmed",
            "Compilation requires a confirmed domain profile. "
            "Derive a domain profile, confirm it, then compile.",
        )

    return _ok({"blueprint": blueprint.to_dict()})
