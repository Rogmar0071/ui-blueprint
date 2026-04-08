"""
ui_blueprint.domain.compiler
=============================
Blueprint compile pipeline.

Enforces the invariant: compilation is forbidden unless a confirmed
DomainProfile is supplied.  Vision extraction uses a stub that returns
deterministic sample entities/relations derived from the domain.
"""

from __future__ import annotations

from typing import Any

from ui_blueprint.domain.ir import (
    DOMAIN_STATUS_CONFIRMED,
    SCHEMA_VERSION,
    BlueprintConstraint,
    BlueprintEntity,
    BlueprintIR,
    BlueprintRelation,
    BlueprintSource,
    Completeness,
    DomainProfile,
    ProvenanceRecord,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BlueprintCompileError(ValueError):
    """Raised when the compile pipeline cannot proceed."""


# ---------------------------------------------------------------------------
# Stub vision extraction
# ---------------------------------------------------------------------------

# Per-domain entity/relation templates used by the stub extractor.
_DOMAIN_STUBS: dict[str, dict[str, Any]] = {
    "Mechanical Assembly": {
        "entities": [
            {
                "type": "primary_structure",
                "attributes": {"description": "Main structural body"},
                "confidence": 0.92,
            },
            {
                "type": "joint",
                "attributes": {"joint_type": "hinge", "description": "Rotational connector"},
                "confidence": 0.85,
            },
            {
                "type": "fastener",
                "attributes": {"fastener_type": "screw", "count_estimate": 4},
                "confidence": 0.78,
            },
            {
                "type": "surface",
                "attributes": {"description": "Planar face", "orientation": "frontal"},
                "confidence": 0.88,
            },
        ],
        "relation_type": "attached_to",
        "constraint_type": "alignment",
        "completeness_score": 0.82,
        "missing_info": [
            "Exact dimensions (include a reference object for scale)",
            "Joint sub-type (mortise/tenon vs bolt-on)",
        ],
    },
    "Warehouse Pallet Inspection": {
        "entities": [
            {"type": "pallet", "attributes": {"description": "Pallet base"}, "confidence": 0.95},
            {
                "type": "case",
                "attributes": {"layer": 1, "column": 1, "row": 1},
                "confidence": 0.88,
            },
            {
                "type": "case",
                "attributes": {"layer": 1, "column": 2, "row": 1},
                "confidence": 0.87,
            },
            {
                "type": "label",
                "attributes": {"ocr_text": "SKU-UNKNOWN", "ocr_confidence": 0.61},
                "confidence": 0.72,
            },
        ],
        "relation_type": "stacked_on",
        "constraint_type": "grid_alignment",
        "completeness_score": 0.74,
        "missing_info": [
            "Clear barcode read (retake label close-up)",
            "Back-right column occluded — needs a sweep",
        ],
    },
    "Retail Shelf Audit": {
        "entities": [
            {
                "type": "shelf",
                "attributes": {"level": 1, "description": "Bottom shelf"},
                "confidence": 0.93,
            },
            {
                "type": "product_facing",
                "attributes": {"shelf_level": 1, "position": 1},
                "confidence": 0.80,
            },
            {
                "type": "product_facing",
                "attributes": {"shelf_level": 1, "position": 2},
                "confidence": 0.80,
            },
            {
                "type": "price_label",
                "attributes": {"readable": False},
                "confidence": 0.55,
            },
        ],
        "relation_type": "placed_on",
        "constraint_type": "planogram_compliance",
        "completeness_score": 0.70,
        "missing_info": ["Price label text unreadable — retake close-up"],
    },
}

_DEFAULT_STUB: dict[str, Any] = {
    "entities": [
        {"type": "object", "attributes": {"description": "Detected object"}, "confidence": 0.70},
    ],
    "relation_type": "related_to",
    "constraint_type": "generic",
    "completeness_score": 0.55,
    "missing_info": [
        "Insufficient media detail — follow capture protocol for more complete extraction",
    ],
}


# Return type alias to keep the stub extract signature within line-length.
_ExtractResult = tuple[
    list[BlueprintEntity],
    list[BlueprintRelation],
    list[BlueprintConstraint],
    list[ProvenanceRecord],
    float,
    list[str],
]


def _stub_extract(media: dict[str, Any], domain: DomainProfile) -> _ExtractResult:
    """Return deterministic stub extraction results for the given domain."""
    stub = _DOMAIN_STUBS.get(domain.name, _DEFAULT_STUB)

    entities = [
        BlueprintEntity(
            type=e["type"],
            attributes=dict(e.get("attributes", {})),
            confidence=float(e.get("confidence", 0.8)),
        )
        for e in stub["entities"]
    ]

    relations: list[BlueprintRelation] = []
    for i in range(len(entities) - 1):
        avg_conf = round((entities[i].confidence + entities[i + 1].confidence) / 2, 3)
        relations.append(
            BlueprintRelation(
                type=stub["relation_type"],
                source_entity_id=entities[i].id,
                target_entity_id=entities[i + 1].id,
                confidence=avg_conf,
            )
        )

    constraint_entity_ids = [e.id for e in entities]
    constraints = [
        BlueprintConstraint(
            type=stub["constraint_type"],
            entities=constraint_entity_ids,
            params={"domain": domain.name},
            confidence=stub["completeness_score"],
        )
    ]

    provenance = [
        ProvenanceRecord(
            kind="stub_extractor",
            details={
                "provider": "StubVisionExtractor",
                "domain": domain.name,
                "media_id": media.get("media_id", "unknown"),
                "note": "Replace with real vision extraction pipeline",
            },
        )
    ]

    score: float = stub["completeness_score"]
    missing: list[str] = list(stub["missing_info"])
    return entities, relations, constraints, provenance, score, missing


# ---------------------------------------------------------------------------
# Public compile entry point
# ---------------------------------------------------------------------------


def compileBlueprintFromMedia(
    media: dict[str, Any],
    confirmed_domain_profile: DomainProfile | None,
) -> BlueprintIR:
    """
    Compile a BlueprintIR from *media* using a confirmed DomainProfile.

    Parameters
    ----------
    media : dict
        Description of the source media.  Recognised keys:
        - media_id    : str  -- identifier for the video/image.
        - media_type  : str  -- "video" | "image" | "other".
        - uri         : str  -- optional URI/path to media.
        - metadata    : dict -- optional extra metadata.
    confirmed_domain_profile : DomainProfile or None
        The domain profile that guides extraction and validation.
        MUST be non-None and have status == "confirmed".

    Returns
    -------
    BlueprintIR
        Compiled artifact with domain_profile_id set to the confirmed
        profile's id.

    Raises
    ------
    BlueprintCompileError
        If confirmed_domain_profile is None or not confirmed.
    """
    if confirmed_domain_profile is None:
        raise BlueprintCompileError(
            "Compilation requires a confirmed domain profile. "
            "Derive a domain profile, confirm it, then compile."
        )
    if confirmed_domain_profile.status != DOMAIN_STATUS_CONFIRMED:
        raise BlueprintCompileError(
            f"Domain profile '{confirmed_domain_profile.id}' has status "
            f"'{confirmed_domain_profile.status}'; only 'confirmed' profiles "
            "may be used at compile time."
        )

    entities, relations, constraints, provenance, score, missing = _stub_extract(
        media, confirmed_domain_profile
    )

    source = BlueprintSource(
        media_id=media.get("media_id", "unknown"),
        start_time_ms=media.get("start_time_ms"),
        end_time_ms=media.get("end_time_ms"),
    )

    return BlueprintIR(
        domain_profile_id=confirmed_domain_profile.id,
        schema_version=SCHEMA_VERSION,
        schema_version_used=confirmed_domain_profile.schema_version,
        source=source,
        completeness=Completeness(score=score, missing_info=missing),
        entities=entities,
        relations=relations,
        constraints=constraints,
        provenance=provenance,
    )
