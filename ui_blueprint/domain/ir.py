"""
ui_blueprint.domain.ir
======================
Data models for AI-derived Domain Profiles and the Blueprint Intermediate
Representation (IR). Field shapes follow the steering contract v1.1.1,
while serialized objects continue to use schema_version v1.1.0.

All classes use plain Python dataclasses with to_dict / from_dict helpers
so no new runtime dependencies are required.

Every object carries a ``schema_version`` field so persisted data can be
migrated when the contract evolves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid4() -> str:
    return str(uuid.uuid4())


# Canonical schema version for all objects produced by this module.
SCHEMA_VERSION = "v1.1.0"

# ---------------------------------------------------------------------------
# Domain Profile sub-models
# ---------------------------------------------------------------------------


@dataclass
class DerivedFrom:
    """Provenance of the AI derivation that produced this profile."""

    schema_version: str = SCHEMA_VERSION
    media_id: str = ""
    provider: str = "stub"
    provider_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "media_id": self.media_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DerivedFrom":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            media_id=data.get("media_id", ""),
            provider=data.get("provider", "stub"),
            provider_version=data.get("provider_version", "1.0"),
        )


@dataclass
class CaptureStep:
    """A single step in a domain's capture protocol."""

    schema_version: str = SCHEMA_VERSION
    step_id: str = field(default_factory=_uuid4)
    title: str = ""
    instructions: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "step_id": self.step_id,
            "title": self.title,
            "instructions": self.instructions,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaptureStep":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            step_id=data.get("step_id", _uuid4()),
            title=data.get("title", ""),
            instructions=data.get("instructions", ""),
            required=bool(data.get("required", True)),
        )


@dataclass
class ProfileValidator:
    """A validation rule associated with a domain profile."""

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    type: str = "generic"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileValidator":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            type=data.get("type", "generic"),
            params=dict(data.get("params", {})),
        )


@dataclass
class ProfileExporter:
    """An export target associated with a domain profile."""

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    type: str = "generic"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileExporter":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            type=data.get("type", "generic"),
            params=dict(data.get("params", {})),
        )


# ---------------------------------------------------------------------------
# Domain Profile
# ---------------------------------------------------------------------------

DOMAIN_STATUS_DRAFT = "draft"
DOMAIN_STATUS_CONFIRMED = "confirmed"
DOMAIN_STATUS_ARCHIVED = "archived"
_VALID_DOMAIN_STATUSES = {DOMAIN_STATUS_DRAFT, DOMAIN_STATUS_CONFIRMED, DOMAIN_STATUS_ARCHIVED}


@dataclass
class DomainProfile:
    """
    An AI-derived domain profile that guides the compile pipeline.

    Domain profiles are NOT hard-coded or bundled as built-in constants.
    They are produced by a DomainDerivationProvider from media input, then
    reviewed and confirmed by the user before compilation.

    Status lifecycle: draft -> confirmed -> archived.
    Once confirmed a profile is immutable; edits require a new draft.
    """

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    name: str = ""
    status: str = DOMAIN_STATUS_DRAFT
    created_at: str = field(default_factory=_now_rfc3339)
    updated_at: str = field(default_factory=_now_rfc3339)
    derived_from: DerivedFrom = field(default_factory=DerivedFrom)
    capture_protocol: list[CaptureStep] = field(default_factory=list)
    validators: list[ProfileValidator] = field(default_factory=list)
    exporters: list[ProfileExporter] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "derived_from": self.derived_from.to_dict(),
            "capture_protocol": [s.to_dict() for s in self.capture_protocol],
            "validators": [v.to_dict() for v in self.validators],
            "exporters": [e.to_dict() for e in self.exporters],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainProfile":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            name=data.get("name", ""),
            status=data.get("status", DOMAIN_STATUS_DRAFT),
            created_at=data.get("created_at", _now_rfc3339()),
            updated_at=data.get("updated_at", _now_rfc3339()),
            derived_from=DerivedFrom.from_dict(data.get("derived_from", {})),
            capture_protocol=[
                CaptureStep.from_dict(s) for s in data.get("capture_protocol", [])
            ],
            validators=[
                ProfileValidator.from_dict(v) for v in data.get("validators", [])
            ],
            exporters=[
                ProfileExporter.from_dict(e) for e in data.get("exporters", [])
            ],
            notes=data.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# Blueprint IR sub-models
# ---------------------------------------------------------------------------


@dataclass
class BlueprintSource:
    """Provenance / source media reference for a BlueprintIR."""

    schema_version: str = SCHEMA_VERSION
    media_id: str = ""
    start_time_ms: int | None = None
    end_time_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "media_id": self.media_id,
        }
        if self.start_time_ms is not None:
            d["start_time_ms"] = self.start_time_ms
        if self.end_time_ms is not None:
            d["end_time_ms"] = self.end_time_ms
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintSource":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            media_id=data.get("media_id", ""),
            start_time_ms=data.get("start_time_ms"),
            end_time_ms=data.get("end_time_ms"),
        )


@dataclass
class Completeness:
    """Completeness summary for a BlueprintIR."""

    schema_version: str = SCHEMA_VERSION
    score: float = 0.0
    missing_info: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "score": self.score,
            "missing_info": list(self.missing_info),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Completeness":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            score=float(data.get("score", 0.0)),
            missing_info=list(data.get("missing_info", [])),
        )


@dataclass
class BlueprintEntity:
    """A detected real-world component extracted from media."""

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    type: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "attributes": dict(self.attributes),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintEntity":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            type=data.get("type", "unknown"),
            attributes=dict(data.get("attributes", {})),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class BlueprintRelation:
    """A directional relationship between two BlueprintEntity nodes."""

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    type: str = "related_to"
    source_entity_id: str = ""
    target_entity_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "attributes": dict(self.attributes),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintRelation":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            type=data.get("type", "related_to"),
            source_entity_id=data.get("source_entity_id", ""),
            target_entity_id=data.get("target_entity_id", ""),
            attributes=dict(data.get("attributes", {})),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class BlueprintConstraint:
    """A structural or geometric constraint derived from the domain schema."""

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    type: str = "generic"
    entities: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "entities": list(self.entities),
            "params": dict(self.params),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintConstraint":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            type=data.get("type", "generic"),
            entities=list(data.get("entities", [])),
            params=dict(data.get("params", {})),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class ProvenanceRecord:
    """A single provenance entry describing evidence that produced IR content."""

    schema_version: str = SCHEMA_VERSION
    kind: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvenanceRecord":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            kind=data.get("kind", "unknown"),
            details=dict(data.get("details", {})),
        )


# ---------------------------------------------------------------------------
# Blueprint IR (top-level artifact)
# ---------------------------------------------------------------------------


@dataclass
class BlueprintIR:
    """
    Blueprint Intermediate Representation — the compiled output artifact.

    A BlueprintIR is produced by compileBlueprintFromMedia() from a
    confirmed DomainProfile.  It is usable by humans, systems, and
    agents to reconstruct a real-world artifact or object.
    """

    schema_version: str = SCHEMA_VERSION
    id: str = field(default_factory=_uuid4)
    domain_profile_id: str = ""
    schema_version_used: str | None = None
    source: BlueprintSource = field(default_factory=BlueprintSource)
    completeness: Completeness = field(default_factory=Completeness)
    entities: list[BlueprintEntity] = field(default_factory=list)
    relations: list[BlueprintRelation] = field(default_factory=list)
    constraints: list[BlueprintConstraint] = field(default_factory=list)
    provenance: list[ProvenanceRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "domain_profile_id": self.domain_profile_id,
            "schema_version_used": self.schema_version_used,
            "source": self.source.to_dict(),
            "completeness": self.completeness.to_dict(),
            "entities": [e.to_dict() for e in self.entities],
            "relations": [r.to_dict() for r in self.relations],
            "constraints": [c.to_dict() for c in self.constraints],
            "provenance": [p.to_dict() for p in self.provenance],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintIR":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            id=data.get("id", _uuid4()),
            domain_profile_id=data.get("domain_profile_id", ""),
            schema_version_used=data.get("schema_version_used"),
            source=BlueprintSource.from_dict(data.get("source", {})),
            completeness=Completeness.from_dict(data.get("completeness", {})),
            entities=[BlueprintEntity.from_dict(e) for e in data.get("entities", [])],
            relations=[BlueprintRelation.from_dict(r) for r in data.get("relations", [])],
            constraints=[
                BlueprintConstraint.from_dict(c) for c in data.get("constraints", [])
            ],
            provenance=[ProvenanceRecord.from_dict(p) for p in data.get("provenance", [])],
        )
