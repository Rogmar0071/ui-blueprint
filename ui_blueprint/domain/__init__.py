"""
ui_blueprint.domain
===================
AI-derived Domain Profiles and Blueprint Compiler.

Exports:
    SCHEMA_VERSION               -- canonical schema version string ("v1.1.0").
    DomainProfile                -- persisted domain profile.
    CaptureStep                  -- one step in the capture protocol.
    ProfileValidator             -- a validation rule.
    ProfileExporter              -- an export target.
    DerivedFrom                  -- AI derivation provenance.
    BlueprintIR                  -- compiled Blueprint Intermediate Representation.
    BlueprintSource              -- source/provenance metadata for a blueprint.
    Completeness                 -- completeness summary.
    BlueprintEntity              -- a detected real-world component.
    BlueprintRelation            -- a relationship between entities.
    BlueprintConstraint          -- a structural constraint.
    ProvenanceRecord             -- evidence record for a blueprint.
    DomainDerivationProvider     -- abstract AI derivation interface.
    StubDomainDerivationProvider -- stub (no external keys needed).
    compileBlueprintFromMedia    -- compile pipeline; requires confirmed domain.
    BlueprintCompileError        -- raised when compile pre-conditions are unmet.
    DomainProfileStore           -- abstract storage interface.
    InMemoryDomainProfileStore   -- in-memory storage implementation.
"""

from ui_blueprint.domain.compiler import BlueprintCompileError, compileBlueprintFromMedia
from ui_blueprint.domain.derivation import (
    DomainDerivationProvider,
    StubDomainDerivationProvider,
)
from ui_blueprint.domain.ir import (
    SCHEMA_VERSION,
    BlueprintConstraint,
    BlueprintEntity,
    BlueprintIR,
    BlueprintRelation,
    BlueprintSource,
    CaptureStep,
    Completeness,
    DerivedFrom,
    DomainProfile,
    ProfileExporter,
    ProfileValidator,
    ProvenanceRecord,
)
from ui_blueprint.domain.store import DomainProfileStore, InMemoryDomainProfileStore

__all__ = [
    "SCHEMA_VERSION",
    "DomainProfile",
    "CaptureStep",
    "ProfileValidator",
    "ProfileExporter",
    "DerivedFrom",
    "BlueprintIR",
    "BlueprintSource",
    "Completeness",
    "BlueprintEntity",
    "BlueprintRelation",
    "BlueprintConstraint",
    "ProvenanceRecord",
    "DomainDerivationProvider",
    "StubDomainDerivationProvider",
    "compileBlueprintFromMedia",
    "BlueprintCompileError",
    "DomainProfileStore",
    "InMemoryDomainProfileStore",
]
