"""
ui_blueprint.domain.derivation
===============================
AI domain-derivation provider abstraction and stub implementation.

DomainDerivationProvider is the abstract interface.
StubDomainDerivationProvider returns deterministic sample profiles from
keyword analysis — no external API keys or network required.

Replace StubDomainDerivationProvider with a real LLM/vision implementation
when you are ready to go beyond the stub.
"""

from __future__ import annotations

import abc
from typing import Any

from ui_blueprint.domain.ir import (
    CaptureStep,
    DerivedFrom,
    DomainProfile,
    ProfileExporter,
    ProfileValidator,
)

# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class DomainDerivationProvider(abc.ABC):
    """
    Abstract AI domain-derivation provider.

    Implement derive() to connect a real AI service (LLM, vision model, …)
    that analyses media and proposes candidate DomainProfile objects.

    The user must confirm exactly one candidate before compilation.
    """

    @abc.abstractmethod
    def derive(
        self, media_input: dict[str, Any], max_candidates: int = 3
    ) -> list[DomainProfile]:
        """
        Derive domain profile candidates from media_input.

        Parameters
        ----------
        media_input : dict
            Recognised keys:
            - media_id     : str  -- identifier for the source video/image.
            - media_type   : str  -- "video" | "image" | "other".
            - hint         : str  -- optional free-text hint from the user.
            - metadata     : dict -- optional extra metadata.
        max_candidates : int
            Maximum number of candidate profiles to return (default 3).

        Returns
        -------
        list[DomainProfile]
            Ranked list of draft DomainProfile objects (highest confidence
            first).  An empty list means no candidates could be derived.
        """


# ---------------------------------------------------------------------------
# Stub domain templates
# ---------------------------------------------------------------------------

_STUB_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "Mechanical Assembly",
        "keywords": ["drawer", "cabinet", "hinge", "rail", "screw", "bolt", "assembly", "part"],
        "capture_protocol": [
            ("front-view", "Front view", "Film the full object from the front (landscape).", True),
            ("left-side", "Left side", "Move around to capture the left side.", True),
            ("right-side", "Right side", "Move around to capture the right side.", True),
            (
            "joints-close-up", "Joints & fasteners",
            "Close-up pass over all joints and fasteners.", True),
            (
            "labels-close-up", "Labels & markings",
            "Close-up of any labels, part numbers, or serial marks.", False),
        ],
        "validators": [
            ("min-one-structural", "min_entity_count", {"min": 1, "entity_type": "structural"}),
            ("joint-types-resolved", "joint_type_confirmed", {}),
            ("dual-orthogonal-view", "dimension_estimable", {"required_views": 2}),
        ],
        "exporters": [
            ("assembly-instructions", "assembly_instructions_text", {}),
            ("parts-list-csv", "parts_list_csv", {}),
            ("parametric-sketch", "parametric_sketch_json", {}),
            ("agent-plan", "agent_reconstruction_plan", {}),
        ],
        "confidence": 0.91,
    },
    {
        "name": "Warehouse Pallet Inspection",
        "keywords": ["pallet", "case", "carton", "warehouse", "shelf", "barcode", "sku", "label"],
        "capture_protocol": [
            ("pallet-front", "Pallet front", "Film the full pallet from the front at 2-3 metres.",
                True),
            ("stack-top", "Top of stack", "Sweep across the top of the stack.", True),
            ("barcode-close-up", "Barcode close-up",
                "Close-up pass over visible barcodes and placards.", True),
            ("corner-view", "Corner view", "Film at least one corner view to capture stack depth.",
                False),
        ],
        "validators": [
            ("pallet-base-detected", "entity_required", {"entity_type": "pallet"}),
            ("label-ocr", "ocr_confidence", {"min": 0.7}),
            ("stack-count-consistent", "stack_count_consistent", {}),
        ],
        "exporters": [
            ("inventory-manifest", "inventory_manifest_json", {}),
            ("pick-plan", "pick_plan_text", {}),
            ("wms-import", "wms_import_csv", {}),
        ],
        "confidence": 0.88,
    },
    {
        "name": "Retail Shelf Audit",
        "keywords": ["shelf", "product", "retail", "store", "facing", "sku", "planogram"],
        "capture_protocol": [
            ("full-bay", "Full shelf bay", "Stand back 1-2 m and film the full bay left-to-right.",
                True),
            ("shelf-level-pan", "Shelf level pan",
                "Capture each shelf level with a slow horizontal pan.", True),
            ("price-labels", "Price labels", "Close-up pass over price labels and product panels.",
                False),
        ],
        "validators": [
            ("shelf-detected", "entity_required", {"entity_type": "shelf"}),
            ("facing-count-derivable", "facing_count_per_level", {}),
        ],
        "exporters": [
            ("planogram-compliance", "planogram_compliance_json", {}),
            ("oos-report", "out_of_stock_report_csv", {}),
        ],
        "confidence": 0.82,
    },
    {
        "name": "General Object",
        "keywords": [],  # fallback — always matches
        "capture_protocol": [
            ("front-view", "Front view", "Film the object from the front.", True),
            ("sides-back", "Sides and back", "Film the sides and back.", True),
            ("features-close-up", "Distinctive features",
                "Capture close-ups of distinctive features.", False),
        ],
        "validators": [
            ("one-entity", "min_entity_count", {"min": 1}),
        ],
        "exporters": [
            ("generic-blueprint", "generic_blueprint_json", {}),
            ("human-description", "human_readable_description", {}),
        ],
        "confidence": 0.60,
    },
]


def _score(template: dict[str, Any], hint: str) -> float:
    hint_lower = hint.lower()
    keywords: list[str] = template["keywords"]
    if not keywords:
        return template["confidence"]
    matches = sum(1 for kw in keywords if kw in hint_lower)
    return min(1.0, template["confidence"] * (0.5 + matches / len(keywords)))


def _build_profile(template: dict[str, Any], media_input: dict[str, Any],
    score: float) -> DomainProfile:
    media_id: str = media_input.get("media_id", "unknown")
    capture_protocol = [
        CaptureStep(step_id=sid, title=title, instructions=instr, required=req)
        for sid, title, instr, req in template["capture_protocol"]
    ]
    validators = [
        ProfileValidator(id=vid, type=vtype, params=dict(vparams))
        for vid, vtype, vparams in template["validators"]
    ]
    exporters = [
        ProfileExporter(id=eid, type=etype, params=dict(eparams))
        for eid, etype, eparams in template["exporters"]
    ]
    return DomainProfile(
        name=template["name"],
        status="draft",
        derived_from=DerivedFrom(media_id=media_id, provider="stub", provider_version="1.0"),
        capture_protocol=capture_protocol,
        validators=validators,
        exporters=exporters,
        notes=f"AI-derived (stub) — confidence {score:.2f}. Review and confirm before compiling.",
    )


# ---------------------------------------------------------------------------
# Stub implementation
# ---------------------------------------------------------------------------


class StubDomainDerivationProvider(DomainDerivationProvider):
    """
    Stub AI domain-derivation provider.

    Returns deterministic DomainProfile candidates derived from keyword
    analysis of the optional hint string in media_input.  No external API
    keys or network access are required.

    Replace with a real implementation that calls an LLM or vision API.
    """

    def derive(
        self, media_input: dict[str, Any], max_candidates: int = 3
    ) -> list[DomainProfile]:
        hint: str = media_input.get("hint", "")
        scored = sorted(
            [(_score(t, hint), t) for t in _STUB_TEMPLATES],
            key=lambda x: x[0],
            reverse=True,
        )
        return [
            _build_profile(t, media_input, s)
            for s, t in scored[:max(1, max_candidates)]
        ]
