"""
tests.test_domain
=================
Unit tests for the AI-derived Domain Profile and Blueprint Compiler.

Covers steering contract v1.1.1 required test cases:
1. compileBlueprintFromMedia rejects None domain
2. compileBlueprintFromMedia rejects non-confirmed domain (draft/archived)
3. BlueprintIR relations/constraints reference only existing entity ids
4. DomainProfileStore roundtrip preserves field shapes
5. Serialisation round-trip for DomainProfile and BlueprintIR
6. StubDomainDerivationProvider returns ranked draft profiles
7. schema_version present on all serialised sub-objects
"""

from __future__ import annotations

import pytest

from ui_blueprint.domain import (
    SCHEMA_VERSION,
    BlueprintCompileError,
    DomainProfile,
    InMemoryDomainProfileStore,
    StubDomainDerivationProvider,
    compileBlueprintFromMedia,
)
from ui_blueprint.domain.ir import (
    DOMAIN_STATUS_CONFIRMED,
    DOMAIN_STATUS_DRAFT,
    BlueprintIR,
    CaptureStep,
    DerivedFrom,
    ProfileExporter,
    ProfileValidator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_provider() -> StubDomainDerivationProvider:
    return StubDomainDerivationProvider()


@pytest.fixture()
def draft_profile() -> DomainProfile:
    provider = StubDomainDerivationProvider()
    candidates = provider.derive(
        {"media_id": "vid_test", "hint": "cabinet drawer assembly"},
        max_candidates=1,
    )
    assert candidates, "Stub must return at least one candidate"
    return candidates[0]


@pytest.fixture()
def confirmed_profile(draft_profile: DomainProfile) -> DomainProfile:
    draft_profile.status = DOMAIN_STATUS_CONFIRMED
    return draft_profile


@pytest.fixture()
def sample_media() -> dict:
    return {"media_id": "vid_001", "media_type": "video"}


# ---------------------------------------------------------------------------
# 1. Compile rejects None / missing domain
# ---------------------------------------------------------------------------


class TestCompileRejectsMissingDomain:
    def test_compile_none_domain_raises(self, sample_media: dict) -> None:
        with pytest.raises(BlueprintCompileError, match="confirmed domain profile"):
            compileBlueprintFromMedia(media=sample_media, confirmed_domain_profile=None)

    def test_compile_error_is_value_error(self, sample_media: dict) -> None:
        with pytest.raises(ValueError):
            compileBlueprintFromMedia(media=sample_media, confirmed_domain_profile=None)


# ---------------------------------------------------------------------------
# 2. Compile rejects unconfirmed (draft/archived) domain
# ---------------------------------------------------------------------------


class TestCompileRejectsUnconfirmedDomain:
    def test_compile_draft_domain_raises(
        self, sample_media: dict, draft_profile: DomainProfile
    ) -> None:
        assert draft_profile.status == DOMAIN_STATUS_DRAFT
        with pytest.raises(BlueprintCompileError, match="confirmed"):
            compileBlueprintFromMedia(
                media=sample_media, confirmed_domain_profile=draft_profile
            )

    def test_compile_archived_domain_raises(
        self, sample_media: dict, draft_profile: DomainProfile
    ) -> None:
        draft_profile.status = "archived"
        with pytest.raises(BlueprintCompileError, match="confirmed"):
            compileBlueprintFromMedia(
                media=sample_media, confirmed_domain_profile=draft_profile
            )

    def test_compile_confirmed_domain_succeeds(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        blueprint = compileBlueprintFromMedia(
            media=sample_media, confirmed_domain_profile=confirmed_profile
        )
        assert isinstance(blueprint, BlueprintIR)
        assert blueprint.domain_profile_id == confirmed_profile.id


# ---------------------------------------------------------------------------
# 3. BlueprintIR structure and invariants
# ---------------------------------------------------------------------------


class TestBlueprintIRStructure:
    def test_blueprint_contains_required_fields(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        assert bp.id
        assert bp.domain_profile_id == confirmed_profile.id
        assert bp.schema_version
        assert bp.schema_version_used == confirmed_profile.schema_version
        assert bp.source.media_id == sample_media["media_id"]
        assert 0.0 <= bp.completeness.score <= 1.0

    def test_blueprint_has_entities(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        assert len(bp.entities) >= 1
        for entity in bp.entities:
            assert entity.id
            assert entity.type
            assert 0.0 <= entity.confidence <= 1.0

    def test_blueprint_relations_reference_valid_entity_ids(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        entity_ids = {e.id for e in bp.entities}
        for rel in bp.relations:
            assert rel.source_entity_id in entity_ids
            assert rel.target_entity_id in entity_ids

    def test_blueprint_constraints_reference_valid_entity_ids(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        entity_ids = {e.id for e in bp.entities}
        for con in bp.constraints:
            for eid in con.entities:
                assert eid in entity_ids

    def test_blueprint_provenance_present(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        assert len(bp.provenance) >= 1
        for rec in bp.provenance:
            assert rec.kind

    def test_blueprint_roundtrip(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        d = bp.to_dict()
        bp2 = BlueprintIR.from_dict(d)
        assert bp2.id == bp.id
        assert bp2.domain_profile_id == bp.domain_profile_id
        assert bp2.schema_version_used == bp.schema_version_used
        assert len(bp2.entities) == len(bp.entities)
        assert len(bp2.relations) == len(bp.relations)


# ---------------------------------------------------------------------------
# 4. schema_version present on all serialised sub-objects
# ---------------------------------------------------------------------------


class TestSchemaVersionInSubObjects:
    def test_domain_profile_schema_version(self, draft_profile: DomainProfile) -> None:
        d = draft_profile.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION
        assert d["derived_from"]["schema_version"] == SCHEMA_VERSION
        for step in d["capture_protocol"]:
            assert step["schema_version"] == SCHEMA_VERSION
        for val in d["validators"]:
            assert val["schema_version"] == SCHEMA_VERSION
        for exp in d["exporters"]:
            assert exp["schema_version"] == SCHEMA_VERSION

    def test_blueprint_ir_schema_version(
        self, sample_media: dict, confirmed_profile: DomainProfile
    ) -> None:
        bp = compileBlueprintFromMedia(sample_media, confirmed_profile)
        d = bp.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION
        assert d["schema_version_used"] == SCHEMA_VERSION
        assert d["source"]["schema_version"] == SCHEMA_VERSION
        assert d["completeness"]["schema_version"] == SCHEMA_VERSION
        for ent in d["entities"]:
            assert ent["schema_version"] == SCHEMA_VERSION
        for rel in d["relations"]:
            assert rel["schema_version"] == SCHEMA_VERSION
        for con in d["constraints"]:
            assert con["schema_version"] == SCHEMA_VERSION
        for prov in d["provenance"]:
            assert prov["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 5. StubDomainDerivationProvider
# ---------------------------------------------------------------------------


class TestStubProvider:
    def test_returns_list_of_domain_profiles(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        results = stub_provider.derive({"media_id": "x"})
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_all_candidates_are_draft(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        for profile in stub_provider.derive({"media_id": "x"}):
            assert profile.status == DOMAIN_STATUS_DRAFT

    def test_hint_influences_first_candidate(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        mechanical = stub_provider.derive(
            {"media_id": "x", "hint": "drawer hinge assembly"}, max_candidates=3
        )
        warehouse = stub_provider.derive(
            {"media_id": "x", "hint": "pallet barcode warehouse"}, max_candidates=3
        )
        assert mechanical[0].name != warehouse[0].name

    def test_max_candidates_respected(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        results = stub_provider.derive({"media_id": "x"}, max_candidates=2)
        assert len(results) <= 2

    def test_derived_from_set_to_stub(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        profile = stub_provider.derive({"media_id": "vid_42"})[0]
        assert profile.derived_from.provider == "stub"
        assert profile.derived_from.media_id == "vid_42"

    def test_capture_protocol_non_empty(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        profile = stub_provider.derive({"media_id": "x"})[0]
        assert len(profile.capture_protocol) >= 1
        step = profile.capture_protocol[0]
        assert step.step_id
        assert step.title
        assert step.instructions

    def test_validators_non_empty(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        profile = stub_provider.derive({"media_id": "x"})[0]
        assert len(profile.validators) >= 1

    def test_exporters_non_empty(
        self, stub_provider: StubDomainDerivationProvider
    ) -> None:
        profile = stub_provider.derive({"media_id": "x"})[0]
        assert len(profile.exporters) >= 1


# ---------------------------------------------------------------------------
# 6. DomainProfile model
# ---------------------------------------------------------------------------


class TestDomainProfileModel:
    def test_default_status_is_draft(self) -> None:
        p = DomainProfile(name="Test")
        assert p.status == DOMAIN_STATUS_DRAFT

    def test_schema_version_default(self) -> None:
        p = DomainProfile(name="Test")
        assert p.schema_version == SCHEMA_VERSION

    def test_roundtrip(self) -> None:
        p = DomainProfile(
            name="Test Domain",
            status=DOMAIN_STATUS_DRAFT,
            derived_from=DerivedFrom(
                media_id="m1", provider="stub", provider_version="1.0"
            ),
            capture_protocol=[
                CaptureStep(
                    step_id="s1",
                    title="Step 1",
                    instructions="Do it",
                    required=True,
                )
            ],
            validators=[
                ProfileValidator(id="v1", type="min_entity_count", params={"min": 1})
            ],
            exporters=[
                ProfileExporter(id="e1", type="generic_blueprint_json", params={})
            ],
            notes="test notes",
        )
        d = p.to_dict()
        p2 = DomainProfile.from_dict(d)
        assert p2.id == p.id
        assert p2.name == p.name
        assert p2.status == p.status
        assert p2.schema_version == SCHEMA_VERSION
        assert p2.derived_from.media_id == "m1"
        assert p2.capture_protocol[0].step_id == "s1"
        assert p2.validators[0].type == "min_entity_count"
        assert p2.exporters[0].type == "generic_blueprint_json"
        assert p2.notes == "test notes"


# ---------------------------------------------------------------------------
# 7. InMemoryDomainProfileStore
# ---------------------------------------------------------------------------


class TestInMemoryStore:
    def test_save_and_get(self) -> None:
        store = InMemoryDomainProfileStore()
        p = DomainProfile(name="Stored Profile")
        store.save(p)
        fetched = store.get(p.id)
        assert fetched is not None
        assert fetched.name == "Stored Profile"

    def test_get_missing_returns_none(self) -> None:
        store = InMemoryDomainProfileStore()
        assert store.get("nonexistent-uuid") is None

    def test_list_all(self) -> None:
        store = InMemoryDomainProfileStore()
        p1 = DomainProfile(name="A")
        p2 = DomainProfile(name="B")
        store.save(p1)
        store.save(p2)
        all_profiles = store.list_all()
        ids = {p.id for p in all_profiles}
        assert p1.id in ids
        assert p2.id in ids

    def test_save_overwrites(self) -> None:
        store = InMemoryDomainProfileStore()
        p = DomainProfile(name="Original")
        store.save(p)
        p.name = "Updated"
        store.save(p)
        fetched = store.get(p.id)
        assert fetched is not None
        assert fetched.name == "Updated"
